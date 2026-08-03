from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from evo.artifact_runtime import record_event
from evo.operations.abtest.candidate import async_candidate_rag_answer, candidate_service, discard_candidate
from evo.operations.abtest.comparison import GOODCASE_MAX_OVERALL_DROP, compare_eval_detail_for_repair
from evo.operations.eval.judge import judge_case
from evo.operations.eval.summary import build_eval_detail_summary


PUBLIC_SERVICE_KEYS = {
    'status', 'service_kind', 'algorithm_id', 'router_chat_url', 'router_admin_url', 'code_path',
}
EXTERNAL_CHAT_FAILURE_TYPES = frozenset({
    'chat_config_error', 'chat_http_error', 'chat_protocol_error', 'chat_runtime_error', 'chat_timeout',
    'chat_transport_error', 'chat_unknown_error', 'router_algorithm_mismatch', 'router_algorithm_protocol_error',
    'router_algorithm_timeout', 'router_algorithm_transport_error', 'router_algorithm_unavailable',
    'router_algorithm_unhealthy', 'router_header_missing',
})
EPSILON = 0.0001
TARGET_MIN_OVERALL_GAIN = 0.10
TRACE_ID = re.compile(r'^[0-9a-f]{32}$')
KB_TRACE_TOOLS = frozenset({'KBToolkit_kb_search', 'KBToolkit_kb_keyword_search'})


async def validate_candidate_patch(
    root: Path,
    diff: str,
    validation_case_ids: Sequence[str],
    target_case_ids: Sequence[str],
    category_baseline: Mapping[str, Any],
    success_metric: str,
    cases: Mapping[str, Mapping[str, Any]],
    baseline_judges: Mapping[str, Mapping[str, Any]],
    eval_policy: Mapping[str, Any],
    candidate_config: Mapping[str, Any],
    ctx: Any,
    attempt: int | None = None,
) -> dict[str, Any]:
    required = list(dict.fromkeys(str(item).strip() for item in validation_case_ids if str(item).strip()))
    selected = {case_id: cases[case_id] for case_id in required if case_id in cases}
    missing_cases = [case_id for case_id in required if case_id not in cases]
    missing_baseline = [case_id for case_id in required if case_id in cases and case_id not in baseline_judges]
    if missing_cases or missing_baseline or not selected:
        return {
            'status': 'rejected', 'accepted': False,
            'reason': 'validation_case_coverage_missing' if selected else 'no_validation_cases',
            'missing_cases': missing_cases, 'missing_baseline_judges': missing_baseline,
        }
    patch = {'status': 'verified', 'workspace_ref': str(root), 'diff': diff}
    record_event('candidate.service_started', status='started', attempt=attempt, data={'case_count': len(selected)})
    service: Mapping[str, Any] | None = None
    try:
        service = candidate_service(candidate_config, patch, ctx, temporary=True)
        public_service = _public_service(service)
        if service.get('status') != 'ready':
            return {
                'status': 'rejected', 'accepted': False, 'reason': 'candidate_service_failed',
                'service': public_service, 'case_ids': list(selected),
            }
        record_event('candidate.service_ready', status='completed', attempt=attempt, data={'service': public_service})
        answers: dict[str, Mapping[str, Any]] = {}
        judges: dict[str, Mapping[str, Any]] = {}
        external_failure = ''
        for case_id, case in selected.items():
            record_event('candidate.case_started', status='started', attempt=attempt, case_id=case_id)
            answer = await async_candidate_rag_answer(case, service)
            judge = judge_case(case, answer, eval_policy)
            answers[case_id], judges[case_id] = answer, judge
            chat_error = answer.get('chat_error') if isinstance(answer.get('chat_error'), Mapping) else {}
            if judge.get('failure_type') == 'infra_failure' and chat_error.get('type') in EXTERNAL_CHAT_FAILURE_TYPES:
                external_failure = str(chat_error.get('type'))
            record_event('candidate.case_completed', status='completed', attempt=attempt, data={
                'case_id': case_id, 'answer_status': answer.get('status'),
                'overall_score': judge.get('overall_score'), 'failure_type': judge.get('failure_type'),
            })
            if external_failure:
                break
        evidence = _evaluation_evidence(selected, answers, judges, public_service)
        if external_failure:
            return {
                'status': 'rejected', 'accepted': False,
                'reason': f'candidate_eval_stopped:{external_failure}',
                'early_stop_reason': external_failure, **evidence,
            }
        baseline_summary = build_eval_detail_summary(tuple(baseline_judges[case_id] for case_id in judges))
        candidate_summary = build_eval_detail_summary(tuple(judges.values())) | {'id': 'repair.candidate_eval_summary'}
        comparison = compare_eval_detail_for_repair(baseline_summary, candidate_summary)
        category_metrics = _category_metrics(
            category_baseline, target_case_ids, baseline_judges, judges,
        )
        mechanism_gate = _mechanism_gate(success_metric, category_metrics)
        gate = _score_gate(comparison, target_case_ids, baseline_judges, judges)
        accepted = comparison.get('status') == 'completed' and not candidate_summary.get('execution_failures') \
            and mechanism_gate['status'] == 'passed' and gate['status'] == 'passed'
        reason = 'validation_passed' if accepted else str(
            mechanism_gate.get('reason') or gate.get('reason')
            or comparison.get('verdict') or 'candidate_validation_failed'
        )
        record_event('candidate.eval_summary_completed', status='completed' if accepted else 'failed',
                     attempt=attempt, data={
                         'reason': reason, 'mechanism_gate': mechanism_gate,
                         'gate': gate, 'category_metrics': category_metrics,
                     })
        return {
            'status': 'accepted' if accepted else 'rejected',
            'accepted': accepted,
            'reason': reason,
            **evidence,
            'candidate_eval_summary': candidate_summary,
            'comparison': comparison,
            'score_gate': gate,
            'mechanism_gate': mechanism_gate,
            'category_metrics': category_metrics,
        }
    finally:
        _cleanup_candidate_service(service, attempt)


def _public_service(service: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in service.items() if key in PUBLIC_SERVICE_KEYS}
    health = service.get('healthcheck') if isinstance(service.get('healthcheck'), Mapping) else {}
    result['healthcheck'] = {
        key: health.get(key)
        for key in ('status', 'type', 'algorithm_status', 'healthy_instances')
        if key in health
    }
    return result


def _evaluation_evidence(selected: Mapping[str, Any], answers: Mapping[str, Mapping[str, Any]],
                         judges: Mapping[str, Mapping[str, Any]], service: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'case_ids': list(selected),
        'evaluated_case_ids': list(judges),
        'service': dict(service),
        'candidate_answer_refs': {
            case_id: {
                'status': answer.get('status'),
                'trace_id': answer.get('trace_id'),
                'context_count': len(answer.get('contexts') or ()),
                'chunk_count': len(answer.get('chunk_ids') or ()),
                'document_count': len(answer.get('doc_ids') or ()),
                'tool_errors': _tool_error_summary(answer.get('tool_errors')),
                'kb_id': str((answer.get('target') or {}).get('kb_id') or ''),
                'kb_tool_observations': _kb_tool_observations(str(answer.get('trace_id') or '')),
            }
            for case_id, answer in answers.items()
        },
        'candidate_judge_refs': {
            case_id: {
                'quality_label': judge.get('quality_label'),
                'failure_type': judge.get('failure_type'),
                'overall_score': judge.get('overall_score'),
                'answer_correctness': judge.get('answer_correctness'),
                'retrieval_quality_score': judge.get('retrieval_quality_score'),
                'retrieval_failure_type': judge.get('retrieval_failure_type'),
            }
            for case_id, judge in judges.items()
        },
    }


def _mechanism_gate(success_metric: str, metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    value = metrics.get(success_metric) if isinstance(metrics.get(success_metric), Mapping) else {}
    delta = _number(value.get('delta'))
    reason = '' if math.isfinite(delta) and delta > EPSILON else 'root_cause_metric_not_improved'
    return {
        'status': 'failed' if reason else 'passed',
        'reason': reason,
        'metric': success_metric,
        'baseline': _number(value.get('baseline')),
        'candidate': _number(value.get('candidate')),
        'delta': delta,
    }


def _tool_error_summary(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result = []
    for item in value[:3]:
        if isinstance(item, Mapping):
            text = str(item.get('type') or item.get('message') or item.get('error') or '').strip()
        else:
            text = str(item).strip()
        if text:
            result.append(text[:300])
    return result


def _kb_tool_observations(trace_id: str) -> list[dict[str, Any]]:
    if TRACE_ID.fullmatch(trace_id) is None:
        return []
    root = Path(os.getenv('LAZYLLM_TRACE_LOCAL_STORAGE_DIR') or '')
    if not root.is_dir():
        return []
    observations = []
    for path in sorted(root.glob(f'*_{trace_id}.jsonl')):
        try:
            lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                span = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            attrs = span.get('attributes') if isinstance(span, Mapping) else None
            if not isinstance(attrs, Mapping):
                continue
            tool = str(span.get('name') or attrs.get('lazyllm.entity.name') or '')
            if tool not in KB_TRACE_TOOLS:
                continue
            request = _json_mapping(attrs.get('lazyllm.io.input'))
            response = _json_mapping(attrs.get('lazyllm.io.output'))
            result = response.get('result') if isinstance(response.get('result'), Mapping) else {}
            args = request.get('args') if isinstance(request.get('args'), Sequence) else ()
            payload = args[0] if args and isinstance(args[0], Mapping) else {}
            error = str(
                attrs.get('lazyllm.error.message')
                or attrs.get('exception.message')
                or response.get('error')
                or ''
            ).strip()
            items = result.get('items')
            item_count = (
                len(items)
                if isinstance(items, Sequence) and not isinstance(items, (str, bytes))
                else None
            )
            observations.append({
                'tool': tool,
                'status': str(attrs.get('lazyllm.status') or ''),
                'query': str(payload.get('query') or payload.get('keyword') or '')[:300],
                'result_total': result.get('total'),
                'result_item_count': item_count,
                'error': error[:500],
            })
            if len(observations) >= 6:
                return observations
    return observations


def _json_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _score_gate(comparison: Mapping[str, Any], target_case_ids: Sequence[str],
                baseline: Mapping[str, Mapping[str, Any]], candidate: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    metrics = comparison.get('metrics') if isinstance(comparison.get('metrics'), Mapping) else {}
    delta = metrics.get('delta') if isinstance(metrics.get('delta'), Mapping) else {}
    overall_delta = _number(delta.get('overall_score'))
    target_ids = [case_id for case_id in target_case_ids if case_id in baseline and case_id in candidate]
    target_gain = _average_delta(target_ids, baseline, candidate, 'overall_score')
    good_ids = [case_id for case_id in candidate if case_id not in target_ids
                and str(baseline[case_id].get('quality_label') or '') == 'good']
    good_delta = _average_delta(good_ids, baseline, candidate, 'overall_score') if good_ids else 0.0
    reason = (
        'overall_score_not_improved' if not math.isfinite(overall_delta) or overall_delta <= EPSILON else
        'target_overall_not_improved' if not math.isfinite(target_gain) or target_gain + EPSILON < TARGET_MIN_OVERALL_GAIN else
        'goodcase_overall_regressed' if good_ids and good_delta < -GOODCASE_MAX_OVERALL_DROP - EPSILON else
        ''
    )
    return {
        'status': 'failed' if reason else 'passed',
        'reason': reason,
        'overall_delta': overall_delta,
        'target_case_count': len(target_ids),
        'target_overall_delta': target_gain,
        'target_required_delta': TARGET_MIN_OVERALL_GAIN,
        'goodcase_count': len(good_ids),
        'goodcase_overall_delta': good_delta,
        'goodcase_allowed_drop': GOODCASE_MAX_OVERALL_DROP,
    }


def _category_metrics(metric_inventory: Mapping[str, Any], target_case_ids: Sequence[str],
                      baseline: Mapping[str, Mapping[str, Any]],
                      judges: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    result = {}
    ids = [case_id for case_id in target_case_ids if case_id in baseline and case_id in judges]
    for metric in metric_inventory:
        before_values = [_number(baseline[case_id].get(metric)) for case_id in ids]
        before_values = [value for value in before_values if math.isfinite(value)]
        values = [_number(judges[case_id].get(metric)) for case_id in ids]
        values = [value for value in values if math.isfinite(value)]
        if not before_values or not values:
            continue
        before, after = fmean(before_values), fmean(values)
        result[str(metric)] = {
            'baseline': round(before, 4),
            'candidate': round(after, 4),
            'delta': round(after - before, 4),
        }
    return result


def _average_delta(case_ids: Sequence[str], baseline: Mapping[str, Mapping[str, Any]],
                   candidate: Mapping[str, Mapping[str, Any]], metric: str) -> float:
    values = [
        _number(candidate[case_id].get(metric)) - _number(baseline[case_id].get(metric))
        for case_id in case_ids
    ]
    return round(fmean(values), 4) if values and all(math.isfinite(value) for value in values) else math.nan


def _cleanup_candidate_service(service: Mapping[str, Any] | None, attempt: int | None) -> None:
    result = discard_candidate(service, delete_workspace=False)
    if result['status'] in {'completed', 'failed'}:
        record_event('candidate.service_stopped', status=result['status'], attempt=attempt, data=result)


def _number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan
