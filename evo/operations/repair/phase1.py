from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evo.llm import LazyLLMClient
from evo.repair_model import EvoModelConfigError, opencode_settings
from evo.traces.detail import build_trace_detail_view

from .agent import (
    ModelCallError,
    ModelCallTimeout,
    _assess,
    _bounded_json,
    _experiment_grounding_issues,
    _known_urls,
    _next_turn,
    _review_experiment,
    _review_stop,
)
from .contracts import build_supported_plan, select_category, validate_analysis
from .demo import capture_live_probes, demo_readiness, run_demo, seal_demo
from .experiment import (
    append_journal,
    cleanup_experiment_workdir,
    content_ref,
    create_experiment,
    materialize_inputs,
    save_experiment_spec,
    write_json,
)
from .opencode import OpenCodeSession
from .validation import inside_repair_scope, repair_scope
from .web import read_web_pages, search_web


DEFAULT_BUDGET = {
    'turns': 20,
    'web_searches': 6,
    'page_reads': 12,
    'opencode_calls': 10,
    'experiments': 3,
    'demo_runs': 8,
    'seconds': 1800,
}
DEFAULT_MODEL_TIMEOUT_SECONDS = 120
MAX_CONSECUTIVE_MODEL_FAILURES = 3
_TRACE_ERROR_MARKERS = (
    'connection refused', 'failed to establish', 'error', 'exception', 'timeout', 'timed out',
    'unavailable', '503',
)


def build_repair_plan(run_id: str, analysis_value: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    try:
        analysis = validate_analysis(analysis_value)
    except (TypeError, ValueError):
        return _plan_failure('blocked', 'unverified_root_cause')
    category_id, category = select_category(analysis['categories'])
    if not inside_repair_scope(category['code_span'], policy.get('allowed_roots'), policy.get('blocked_roots')):
        return _plan_failure('blocked', 'target_outside_repair_scope')
    result = run_phase1(str(run_id), {
        'category_id': category_id,
        'source_hash': analysis['source_hash'],
        'category': category,
    }, policy)
    if result.get('status') != 'supported':
        status = str(result.get('status') or 'failed')
        return _plan_failure(status if status in {'blocked', 'exhausted', 'failed'} else 'failed',
                             str(result.get('reason') or 'phase1_failed'))
    try:
        plan = build_supported_plan(category_id, result)
    except (TypeError, ValueError):
        return _plan_failure('failed', 'phase1_invalid_result')
    if not inside_repair_scope(
        plan['method']['code_scope'], policy.get('allowed_roots'), policy.get('blocked_roots'),
    ):
        return _plan_failure('blocked', 'repair_method_outside_scope')
    return plan


def _plan_failure(status: str, reason: str) -> dict[str, str]:
    return {'id': 'repair.plan', 'status': status, 'reason': reason}


def run_phase1(run_id: str, target: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    work_root: Path | None = None
    artifact_root: Path | None = None
    try:
        category_id = str(target.get('category_id') or '').strip()
        source_hash = str(target.get('source_hash') or '').strip()
        budget = _limits(policy.get('phase1_budget'))
        input_hash = _input_hash(target, policy)
        raw_guidance = policy.get('user_guidance') or []
        if not isinstance(raw_guidance, (list, tuple)):
            raise ValueError('phase1_user_guidance_invalid')
        trace_evidence = _load_trace_evidence(target)
        if not trace_evidence:
            return _terminal('blocked', 'trace_evidence_unavailable')
        source_value = policy.get('candidate_source_dir') or os.getenv('LAZYMIND_EVO_CHAT_SOURCE') or '/app/algorithm'
        source_dir = Path(str(source_value)).resolve()
        try:
            work_root, artifact_root = create_experiment(
                run_id, category_id, input_hash, source_dir, source_hash, policy,
            )
        except ValueError as exc:
            return _terminal('blocked', str(exc))
        try:
            config = opencode_settings(_llm_config(policy).get('evo_llm'))
        except EvoModelConfigError as exc:
            return _terminal('blocked', exc.reason)
        session = OpenCodeSession(
            category_id=category_id,
            input_hash=input_hash,
            workdir=work_root,
            artifact_root=artifact_root,
            config=config,
            timeout_s=min(budget['seconds'], int(policy.get('opencode_timeout_s') or 900)),
        )
        client = LazyLLMClient(llm_config=_llm_config(policy), model='evo_llm')
        model_timeout = _positive_seconds(
            policy.get('phase1_llm_timeout_s'), DEFAULT_MODEL_TIMEOUT_SECONDS,
        )
        state: dict[str, Any] = {
            'target': dict(target),
            'user_guidance': [str(item).strip() for item in raw_guidance if str(item).strip()],
            'repair_scope': repair_scope(policy.get('allowed_roots'), policy.get('blocked_roots')),
            'demo_allowed_origins': _demo_allowed_origins(policy),
            'trace_evidence': trace_evidence,
            'code_findings': [],
            'web_searches': [],
            'web_pages': [],
            'read_urls': [],
            'experiment': None,
            'runs': [],
            'assessment': None,
            'demo_attempt': 0,
            'failures': [],
        }
        counters = {key: 0 for key in DEFAULT_BUDGET if key != 'seconds'}
        consecutive_model_failures = 0
        deadline = time.monotonic() + budget['seconds']
        journal_ref = append_journal(artifact_root, 'phase1.started', {
            'category_id': category_id, 'input_hash': input_hash,
        })
        for turn_no in range(1, budget['turns'] + 1):
            counters['turns'] = turn_no
            if time.monotonic() >= deadline:
                return _terminal('exhausted', 'phase1_time_budget_exhausted')
            try:
                turn = _next_turn(client, state, counters, budget, model_timeout, deadline)
            except (ModelCallTimeout, ModelCallError) as exc:
                consecutive_model_failures += 1
                reason = (
                    'phase1_model_timeout'
                    if isinstance(exc, ModelCallTimeout)
                    else f'phase1_model_error:{exc}'
                )
                journal_ref = append_journal(artifact_root, 'model.failed', {
                    'stage': 'agent_decision', 'turn': turn_no, 'reason': reason,
                })
                state['failures'].append({'action': 'agent_decision', 'reason': reason})
                if (consecutive_model_failures < MAX_CONSECUTIVE_MODEL_FAILURES
                        and time.monotonic() < deadline):
                    continue
                return _terminal('failed', reason)
            consecutive_model_failures = 0
            journal_ref = append_journal(artifact_root, 'agent.turn', {
                'turn': turn_no, 'action': turn.action, 'reason': turn.reason,
            })
            if turn.action == 'search_web':
                counters['web_searches'] += 1
                result = search_web(turn.question, artifact_root)
                state['web_searches'].append(result)
                journal_ref = append_journal(artifact_root, 'web.search', {
                    'status': result.get('status'), 'query': result.get('query'),
                    'result_count': len(result.get('results') or ()),
                })
                continue
            if turn.action == 'read_web':
                already_read = set(state['read_urls'])
                fresh_urls = [url for url in turn.urls if url not in already_read]
                if not fresh_urls:
                    state['failures'].append({'action': 'read_web', 'reason': 'pages_already_read'})
                    continue
                if counters['page_reads'] + len(fresh_urls) > budget['page_reads']:
                    state['failures'].append({'action': 'read_web', 'reason': 'page_read_budget_exhausted'})
                    continue
                allowed_urls = _known_urls(state)
                if any(url not in allowed_urls for url in fresh_urls):
                    state['failures'].append({'action': 'read_web', 'reason': 'url_not_from_search_or_user'})
                    continue
                counters['page_reads'] += len(fresh_urls)
                result = read_web_pages(
                    turn.question, fresh_urls, work_root, artifact_root, seen_urls=already_read,
                )
                state['read_urls'].extend(fresh_urls)
                state['web_pages'].append({'content_trust': 'external_untrusted', **result})
                journal_ref = append_journal(artifact_root, 'web.read', {
                    'page_count': len(result.get('pages') or ()),
                    'statuses': [item.get('status') for item in result.get('pages') or ()],
                })
                continue
            if turn.action == 'investigate':
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    return _terminal('exhausted', 'phase1_time_budget_exhausted')
                counters['opencode_calls'] += 1
                _write_opencode_context(work_root, state)
                result = session.run('investigate', turn.instruction, remaining_seconds)
                if result['invalid_changes']:
                    return _terminal('failed', 'phase1_workspace_tainted')
                if result['status'] == 'completed':
                    state['code_findings'].extend(result['report'].get('findings') or ())
                else:
                    state['failures'].append({'action': 'investigate', 'reason': result['reason']})
                journal_ref = append_journal(artifact_root, 'opencode.investigate', {
                    'status': result['status'], 'reason': result['reason'],
                    'session_id': result['session_id'],
                    'artifacts': result['artifacts'],
                })
                continue
            if turn.action == 'experiment':
                candidate_spec = {
                    'repair_method': turn.repair_method.strip(),
                    'success_metric': turn.success_metric.strip(),
                    'repair_steps': [item.model_dump() for item in turn.repair_steps],
                    'demo_method': turn.demo_method.strip(),
                    'inputs': [item.model_dump() for item in turn.inputs],
                    'expected': {
                        'must_observe': turn.must_observe,
                        'must_not_observe': turn.must_not_observe,
                    },
                    'code_scope': [item.model_dump() for item in turn.code_scope],
                    'repair_requirements': turn.repair_requirements,
                    'demo_requirements': turn.demo_requirements,
                    'live_urls': [str(item).strip() for item in turn.live_urls if str(item).strip()],
                }
                try:
                    _validate_live_urls(candidate_spec['live_urls'], state['demo_allowed_origins'])
                    if candidate_spec['success_metric'] not in target['category']['metric_averages']:
                        raise ValueError('success_metric_not_in_analysis')
                except ValueError as exc:
                    state['failures'].append({'action': 'experiment', 'reason': str(exc)})
                    continue
                draft_path = artifact_root / 'reviews' / f'turn-{turn_no:02d}.json'
                write_json(draft_path, candidate_spec)
                draft_ref = content_ref(draft_path, artifact_root)
                remaining_seconds = min(model_timeout, deadline - time.monotonic())
                if remaining_seconds <= 0:
                    return _terminal('exhausted', 'phase1_time_budget_exhausted')
                try:
                    review = _review_experiment(client, state, candidate_spec, remaining_seconds)
                except ModelCallTimeout:
                    reason = 'phase1_model_timeout'
                except ModelCallError as exc:
                    reason = f'phase1_model_error:{exc}'
                else:
                    causal_order_valid = review.causal_order_valid(len(turn.repair_steps))
                    grounding_issues = _experiment_grounding_issues(state, candidate_spec, review)
                    guidance_valid = (
                        review.guidance_valid(state['user_guidance'])
                        and not any(item.startswith('guidance_') for item in grounding_issues)
                    )
                    review_accepted = all((
                        causal_order_valid, review.demo_distinguishes_method,
                        review.success_metric_matches_root_cause,
                        review.repair_steps_unambiguous, review.repair_steps_minimal,
                        guidance_valid, not grounding_issues,
                    ))
                    verdict = 'accept' if review_accepted else 'revise'
                    journal_ref = append_journal(artifact_root, 'experiment.reviewed', {
                        'verdict': verdict,
                        'checks': {
                            'causal_order_valid': causal_order_valid,
                            'demo_distinguishes_method': review.demo_distinguishes_method,
                            'success_metric_matches_root_cause': review.success_metric_matches_root_cause,
                            'repair_steps_unambiguous': review.repair_steps_unambiguous,
                            'repair_steps_minimal': review.repair_steps_minimal,
                            'guidance_valid': guidance_valid,
                            'evidence_grounded': not grounding_issues,
                        },
                        'causal_checks': [item.model_dump() for item in review.causal_checks],
                        'guidance_checks': [item.model_dump() for item in review.guidance_checks],
                        'issues': [*review.issues, *grounding_issues],
                        'reason': review.reason,
                        'candidate_ref': draft_ref,
                    })
                    if not review_accepted:
                        state['failures'].append({
                            'action': 'experiment_review',
                            'reason': review.reason,
                            'issues': [*review.issues, *grounding_issues],
                        })
                        continue
                    reason = ''
                if reason:
                    journal_ref = append_journal(artifact_root, 'model.failed', {
                        'stage': 'experiment_review', 'turn': turn_no, 'reason': reason,
                        'candidate_ref': draft_ref,
                    })
                    state['failures'].append({'action': 'experiment_review', 'reason': reason})
                    continue
                counters['experiments'] += 1
                live_probe = capture_live_probes(
                    candidate_spec['live_urls'], state['demo_allowed_origins'], artifact_root,
                ) if candidate_spec['live_urls'] else {'results': [], 'ref': None}
                if live_probe['results']:
                    journal_ref = append_journal(artifact_root, 'live.probed', {
                        'results': live_probe['results'], 'probe_ref': live_probe['ref'],
                    })
                materialized = []
                for item in turn.inputs:
                    payload = dict(item.payload)
                    if live_probe['results']:
                        payload['_repair_live_probes'] = live_probe['results']
                    materialized.append({'name': item.name, 'payload': payload})
                inputs = materialize_inputs(
                    work_root, artifact_root, materialized,
                )
                spec = {
                    **candidate_spec,
                    'inputs': [{'name': item['name'], 'ref': item['ref']} for item in inputs],
                    'live_probe_ref': live_probe['ref'],
                }
                spec_ref = save_experiment_spec(artifact_root, spec)
                state['experiment'] = {'spec': spec, 'spec_ref': spec_ref, 'inputs': inputs}
                result = _write_and_run_demo(
                    client, session, 'write_demo',
                    'Implement only demo_method and demo_requirements from the frozen Experiment Spec in '
                    'opencode/context.json; repair_method and code_scope describe the later formal patch.',
                    work_root, artifact_root, state, counters, budget, source_hash, deadline, model_timeout,
                )
                journal_ref = result['journal_ref'] or journal_ref
                if result.get('fatal'):
                    reason = str(result['fatal'])
                    return _terminal('exhausted' if reason == 'phase1_time_budget_exhausted' else 'failed', reason)
                if result.get('supported'):
                    return _supported(state, artifact_root, journal_ref)
                continue
            if turn.action == 'revise_demo':
                if not state.get('experiment'):
                    state['failures'].append({'action': 'revise_demo', 'reason': 'experiment_missing'})
                    continue
                result = _write_and_run_demo(
                    client, session, 'revise_demo', turn.instruction,
                    work_root, artifact_root, state, counters, budget, source_hash, deadline, model_timeout,
                )
                journal_ref = result['journal_ref'] or journal_ref
                if result.get('fatal'):
                    reason = str(result['fatal'])
                    return _terminal('exhausted' if reason == 'phase1_time_budget_exhausted' else 'failed', reason)
                if result.get('supported'):
                    return _supported(state, artifact_root, journal_ref)
                continue
            if turn.action == 'stop':
                remaining_seconds = min(model_timeout, deadline - time.monotonic())
                if remaining_seconds <= 0:
                    return _terminal('exhausted', 'phase1_time_budget_exhausted')
                try:
                    review = _review_stop(client, state, turn, remaining_seconds)
                except ModelCallTimeout:
                    reason = 'phase1_model_timeout'
                except ModelCallError as exc:
                    reason = f'phase1_model_error:{exc}'
                else:
                    accepted = (
                        review.requirements_resolved
                        and not review.in_scope_alternative_exists
                        and review.terminal_justified
                    )
                    journal_ref = append_journal(artifact_root, 'stop.reviewed', {
                        'verdict': 'accept' if accepted else 'continue',
                        'requested_status': turn.stop_status,
                        'checks': {
                            'requirements_resolved': review.requirements_resolved,
                            'in_scope_alternative_exists': review.in_scope_alternative_exists,
                            'terminal_justified': review.terminal_justified,
                        },
                        'issues': review.issues, 'reason': review.reason,
                    })
                    if accepted:
                        return _terminal(turn.stop_status, turn.reason)
                    state['failures'].append({
                        'action': 'stop_review', 'reason': review.reason, 'issues': review.issues,
                    })
                    continue
                journal_ref = append_journal(artifact_root, 'model.failed', {
                    'stage': 'stop_review', 'turn': turn_no, 'reason': reason,
                })
                state['failures'].append({'action': 'stop_review', 'reason': reason})
                continue
        return _terminal('exhausted', 'phase1_turn_budget_exhausted')
    except Exception as exc:
        if artifact_root is not None:
            append_journal(artifact_root, 'phase1.failed', {'error_type': type(exc).__name__, 'reason': str(exc)})
        return _terminal('failed', f'phase1_error:{type(exc).__name__}')
    finally:
        cleanup_experiment_workdir(work_root)


def _write_and_run_demo(client: LazyLLMClient, session: OpenCodeSession, task: str, instruction: str, work_root: Path,
                        artifact_root: Path, state: dict[str, Any], counters: dict[str, int],
                        budget: Mapping[str, int], source_hash: str, deadline: float,
                        model_timeout: float) -> dict[str, Any]:
    if counters['opencode_calls'] >= budget['opencode_calls']:
        state['failures'].append({'action': task, 'reason': 'opencode_budget_exhausted'})
        return {'supported': False, 'journal_ref': None}
    experiment = state['experiment']
    _write_opencode_context(work_root, state)
    counters['opencode_calls'] += 1
    remaining_seconds = deadline - time.monotonic()
    if remaining_seconds <= 0:
        return {'supported': False, 'journal_ref': None, 'fatal': 'phase1_time_budget_exhausted'}
    opencode_result = session.run(task, instruction, remaining_seconds)
    journal_ref = append_journal(artifact_root, f'opencode.{task}', {
        'status': opencode_result['status'], 'reason': opencode_result['reason'],
        'session_id': opencode_result['session_id'],
        'artifacts': opencode_result['artifacts'],
    })
    if opencode_result['status'] != 'completed':
        state['failures'].append({'action': task, 'reason': opencode_result['reason']})
        fatal = 'phase1_workspace_tainted' if opencode_result['invalid_changes'] else ''
        return {'supported': False, 'journal_ref': journal_ref, 'fatal': fatal}
    try:
        sealed = seal_demo(work_root, artifact_root, source_hash)
    except ValueError as exc:
        state['failures'].append({'action': task, 'reason': str(exc)})
        return {'supported': False, 'journal_ref': journal_ref}
    remaining = budget['demo_runs'] - counters['demo_runs']
    readiness = demo_readiness(
        work_root, experiment['spec'], experiment['inputs'], sealed,
        opencode_result['report'], remaining,
    )
    if readiness['status'] != 'ready':
        state['failures'].append({'action': task, 'reason': readiness['reason']})
        return {'supported': False, 'journal_ref': journal_ref}
    counters['demo_runs'] += len(experiment['inputs'])
    state['demo_attempt'] += 1
    remaining_seconds = min(120.0, deadline - time.monotonic())
    if remaining_seconds <= 0:
        return {'supported': False, 'journal_ref': journal_ref, 'fatal': 'phase1_time_budget_exhausted'}
    runs = run_demo(
        work_root, artifact_root, experiment['inputs'], attempt=state['demo_attempt'],
        timeout_seconds=remaining_seconds, output_limit=256 * 1024,
        expected_source_hash=source_hash, expected_demo_hash=sealed['sha256'],
    )
    state['runs'] = list(runs)
    state['sealed_demo'] = sealed
    mechanical = [record for record in runs if record['status'] != 'completed']
    if mechanical:
        state['assessment'] = {
            'verdict': 'inconclusive', 'matched': [],
            'unmet': list(experiment['spec']['expected'].get('must_observe') or ()),
            'unexpected': [f"{item['input_name']}:{item['status']}" for item in mechanical],
            'reason': 'Demo did not complete its mechanical execution contract.',
        }
    else:
        for assessment_attempt in range(1, MAX_CONSECUTIVE_MODEL_FAILURES + 1):
            remaining_seconds = min(model_timeout, deadline - time.monotonic())
            if remaining_seconds <= 0:
                return {
                    'supported': False, 'journal_ref': journal_ref,
                    'fatal': 'phase1_time_budget_exhausted',
                }
            try:
                state['assessment'] = _assess(
                    client, experiment['spec'], runs, remaining_seconds,
                ).model_dump()
                break
            except ModelCallTimeout:
                reason = 'phase1_model_timeout'
            except ModelCallError as exc:
                reason = f'phase1_model_error:{exc}'
            journal_ref = append_journal(artifact_root, 'model.failed', {
                'stage': 'demo_assessment', 'attempt': assessment_attempt, 'reason': reason,
            })
            state['failures'].append({'action': 'demo_assessment', 'reason': reason})
            if assessment_attempt == MAX_CONSECUTIVE_MODEL_FAILURES:
                return {'supported': False, 'journal_ref': journal_ref, 'fatal': reason}
    journal_ref = append_journal(artifact_root, 'demo.assessed', {
        'verdict': state['assessment']['verdict'],
        'reason': state['assessment']['reason'],
        'runs': [{key: value for key, value in item.items() if key != '_output'} for item in runs],
    })
    return {'supported': state['assessment']['verdict'] == 'supports', 'journal_ref': journal_ref}


def _load_trace_evidence(target: Mapping[str, Any]) -> list[dict[str, Any]]:
    category = target.get('category') if isinstance(target.get('category'), Mapping) else {}
    result = []
    for case_id, trace_id in list((category.get('cases') or {}).items())[:2]:
        detail = build_trace_detail_view(str(trace_id))
        if detail.get('trace_status') != 'success':
            continue
        nodes = []
        trace = detail.get('trace') if isinstance(detail.get('trace'), Mapping) else {}
        root = trace.get('root') if isinstance(trace.get('root'), Mapping) else None
        stack = [root] if root else []
        while stack and len(nodes) < 40:
            node = stack.pop()
            raw_node = {
                'name': node.get('name'), 'type': node.get('type'), 'status': node.get('status'),
            }
            node_detail = _bounded_json({
                'input': node.get('input'), 'output': node.get('output'), 'metadata': node.get('metadata'),
            }, 1600)
            if str(node.get('status') or '').casefold() not in {'ok', 'success', 'completed'} or any(
                marker in node_detail.casefold() for marker in _TRACE_ERROR_MARKERS
            ):
                raw_node['error_evidence'] = node_detail
            nodes.append(raw_node)
            stack.extend(reversed([child for child in node.get('children') or () if isinstance(child, Mapping)]))
        result.append({
            'case_id': str(case_id), 'trace_id': str(trace_id), 'query': detail.get('query'),
            'summary': detail.get('summary'), 'nodes': nodes,
        })
    return result


def _write_opencode_context(work_root: Path, state: Mapping[str, Any]) -> None:
    value = {
        'root_cause': state.get('target'),
        'user_guidance': state.get('user_guidance'),
        'trace_evidence': state.get('trace_evidence'),
        'code_findings': state.get('code_findings'),
        'web_pages': state.get('web_pages'),
        'experiment': state.get('experiment'),
        'latest_runs': state.get('runs'),
        'latest_assessment': state.get('assessment'),
    }
    path = work_root / 'opencode' / 'context.json'
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n',
                    encoding='utf-8')


def _supported(state: Mapping[str, Any], artifact_root: Path, journal_ref: Mapping[str, str]) -> dict[str, Any]:
    experiment = state['experiment']
    assessment = state['assessment']
    result_path = artifact_root / 'outputs' / 'result.json'
    clean_runs = [{key: value for key, value in item.items() if key != '_output'} for item in state.get('runs') or ()]
    result_path.write_text(json.dumps({'runs': clean_runs, 'assessment': assessment}, ensure_ascii=False,
                                      indent=2, sort_keys=True) + '\n', encoding='utf-8')
    result_ref = content_ref(result_path, artifact_root)
    return {
        'status': 'supported',
        'reason': '',
        'method': {
            'summary': experiment['spec']['repair_method'],
            'success_metric': experiment['spec']['success_metric'],
            'steps': experiment['spec']['repair_steps'],
            'code_scope': experiment['spec']['code_scope'],
            'requirements': experiment['spec']['repair_requirements'],
        },
        'demo_validation': {
            'verdict': 'supports',
            'reason': assessment['reason'],
            'spec_ref': experiment['spec_ref'],
            'demo_ref': state['sealed_demo']['demo_ref'],
            'result_ref': result_ref,
            'journal_ref': dict(journal_ref),
        },
    }


def _terminal(status: str, reason: str) -> dict[str, Any]:
    return {'status': status, 'reason': str(reason or status)}


def _limits(value: object) -> dict[str, int]:
    raw = value if isinstance(value, Mapping) else {}
    result = {}
    for key, default in DEFAULT_BUDGET.items():
        candidate = raw.get(key, default)
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
            raise ValueError(f'phase1_budget_invalid:{key}')
        result[key] = min(candidate, 1800 if key == 'seconds' else 100)
    return result


def _positive_seconds(value: object, default: float) -> float:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or candidate <= 0:
        raise ValueError('phase1_llm_timeout_invalid')
    return min(float(candidate), 300.0)


def _input_hash(target: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    relevant_policy = {
        'allowed_roots': policy.get('allowed_roots'),
        'blocked_roots': policy.get('blocked_roots'),
        'user_guidance': policy.get('user_guidance'),
        'phase1_budget': policy.get('phase1_budget'),
        'opencode_timeout_s': policy.get('opencode_timeout_s'),
        'phase1_llm_timeout_s': policy.get('phase1_llm_timeout_s'),
        'phase1_demo_allowed_origins': policy.get('phase1_demo_allowed_origins'),
        'llm_config': policy.get('llm_config'),
    }
    payload = json.dumps({'target': target, 'policy': relevant_policy}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _demo_allowed_origins(policy: Mapping[str, Any]) -> list[str]:
    configured = policy.get('phase1_demo_allowed_origins') or []
    if not isinstance(configured, (list, tuple)):
        raise ValueError('phase1_demo_allowed_origins_invalid')
    values = [str(item).strip() for item in configured if str(item).strip()]
    for name in (
        'LAZYMIND_DOCUMENT_PROCESSOR_URL', 'LAZYMIND_EVO_TARGET_CHAT_URL',
        'LAZYMIND_EVO_KB_BASE_URL', 'LAZYMIND_EVO_CHUNK_BASE_URL',
    ):
        value = os.getenv(name, '').strip()
        if value:
            values.append(value)
    result = []
    for value in values:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError(f'phase1_demo_allowed_origin_invalid:{value}')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        origin = f'{parsed.scheme}://{parsed.hostname}:{port}'
        if origin not in result:
            result.append(origin)
    return result


def _validate_live_urls(urls: list[str], allowed_origins: list[str]) -> None:
    for url in urls:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        origin = f'{parsed.scheme}://{parsed.hostname}:{port}' if parsed.hostname else ''
        if parsed.scheme not in {'http', 'https'} or origin not in allowed_origins:
            raise ValueError(f'demo_live_url_not_allowed:{url}')


def _llm_config(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    value = policy.get('llm_config')
    return value if isinstance(value, Mapping) else {}
