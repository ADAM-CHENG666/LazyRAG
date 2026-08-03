from __future__ import annotations

import json
import re
import signal
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, ValidationError

from evo.llm import LazyLLMClient, parse_json_object
from evo.repair_model import EvoModelConfigError, resolve_evo_model

from .contracts import AgentAssessment, AgentTurn, ExperimentReview, PatchReview, StopReview
from .validation import inside_repair_scope


_ACTION_CONTRACTS = {
    'search_web': (
        'Search discovery only. question must be a concise 3-10 keyword query, not a long natural-language '
        'question. It does not read page bodies.'
    ),
    'read_web': 'Read exact URLs returned by search_web or supplied by the user; fetched body becomes evidence.',
    'investigate': (
        'Ask OpenCode a read-only code question. It may search/read source and report findings, but must never '
        'create or edit Demo files.'
    ),
    'experiment': (
        'Freeze repair_method/success_metric/code_scope/repair_requirements for the later formal patch, and separately freeze '
        'demo_method/demo_requirements/inputs/must_observe/must_not_observe for the isolated experiment. The system '
        'then asks the same OpenCode session to write the Demo at the fixed demo/run_demo.py entry; demo_method must '
        'not name a source/ or algorithm/ path. Every input payload is one complete independently runnable scenario, '
        'never a parameter fragment. Demo execution is pure in-process: no socket, HTTP server, loopback, subprocess '
        'or file writes. Controlled in-process doubles may represent an external input when the Demo claim is only '
        'about local control flow or the output contract; label them as controlled inputs and never use them to '
        'claim a live service fact. live_urls are required only when the selected method claims a concrete endpoint '
        'is reachable, ready, or returns a particular real response. The trusted runner captures those facts as '
        '_repair_live_probes. Do not use investigate to write.'
    ),
    'revise_demo': 'Revise an already-created Demo after a real run or readiness failure; unavailable before experiment.',
    'stop': 'Stop only when blocked, exhausted or failed; explain the concrete blocker.',
}


class ModelCallTimeout(TimeoutError):
    pass


class ModelCallError(RuntimeError):
    pass



def _next_turn(client: LazyLLMClient, state: Mapping[str, Any], counters: Mapping[str, int],
               budget: Mapping[str, int], timeout_seconds: float,
               phase_deadline: float | None = None) -> AgentTurn:
    available = _available_actions(state, counters, budget)
    prompt = (
        'You are the Repair Phase-1 decision maker. Analysis already supplied a verified root cause. '
        'Every explicit user-guidance entry is a requirement, not optional advice. User guidance is chronological; '
        'when entries conflict, follow the latest explicit direction. Never dismiss an unsatisfied user requirement '
        'as unnecessary. Before freezing an experiment, choose actions that satisfy every still-applicable guidance '
        'requirement; for example, requested web research requires search_web followed by read_web evidence. '
        'Choose exactly one next action that closes the smallest current information gap. OpenCode investigates '
        'local code and writes Demo files; you define the experiment and judge its observed outputs. Web snippets '
        'only select pages; only fetched page bodies are evidence. Do not request pytest, services, eval pipelines, '
        'formal source edits, pause, resume, or cancel. Return one AgentTurn JSON object.\n'
        'A frozen repair method must respect startup causality: never wait for a condition that can only be produced '
        'by a later startup step. Name the exact success predicate, not just the endpoint: an HTTP 200 response and '
        'a payload containing a later-created object are different predicates. Identify which earlier action makes '
        'each predicate true before choosing experiment. In particular, a gate before start/register/create may '
        'check that an endpoint answers HTTP, but it must not '
        'require list membership or object registration that those later steps produce. '
        'repair_steps is the exact real execution order for the formal repair: include unchanged surrounding steps '
        'whenever they produce or consume a required predicate; '
        'mark actual edits as kind=change and unchanged causal steps as kind=context. Context steps must describe '
        'the current code order exactly and must never add, delete, or reorder behavior. A time-qualified instruction '
        'such as "do not wait before startup" does not forbid preserving the same wait after startup. Guidance such '
        'as "only replace A with B" or "prefer only replacing A" forbids extra change steps unless later guidance '
        'explicitly requests them. Each step must list requires and produces. repair_method is only its plain-language '
        'summary. The Demo must exercise the exact predicate and decision proposed by repair_method, not a nearby or '
        'weaker behavior. demo_method must contain only the final selected pure in-process approach; do not narrate '
        'discarded alternatives or mention forbidden network, server, subprocess or source-write techniques even to '
        'reject them. A Demo may use explicit controlled doubles to prove local branching, transformation, and '
        'output-contract behavior under an external condition that Analysis already verified. It must compare old '
        'and repaired behavior under the same controlled input and must not claim that the real dependency is '
        'healthy. Use live_urls only when the selected method claims a concrete endpoint is reachable, ready, or '
        'returns a particular real response, then consume _repair_live_probes. The trusted runner performs that '
        'network call before the isolated Demo starts. For '
        'an endpoint replacement, live_urls must contain both the original failing URL and the proposed URL so the '
        'same run captures the baseline and candidate. Available origins are in State.demo_allowed_origins.\n'
        'code_scope means the planned formal edit scope, not every file consulted as evidence. Paths must be '
        'repository-relative (never prefixed with source/) and stay inside repair_scope. The same repository-relative '
        'path rule applies to repair_steps; never copy the isolated OpenCode workspace prefix into the formal plan.\n'
        'success_metric must be exactly one key from State.target.category.metric_averages. Select the earliest '
        'available score that directly measures the verified broken mechanism, not overall_score or a later answer '
        'quality score when a more direct metric exists. The formal candidate is rejected unless this metric improves.\n'
        f'AgentTurn schema: {json.dumps(AgentTurn.model_json_schema(), ensure_ascii=False)}\n'
        f'Available action contracts now: {json.dumps(available, ensure_ascii=False)}\n'
        f'Budgets: {json.dumps(dict(budget), ensure_ascii=False)}\n'
        f'Used: {json.dumps(dict(counters), ensure_ascii=False)}\n'
        f'State: {_bounded_json(_agent_state(state))}'
    )
    error = ''
    deadline = phase_deadline or time.monotonic() + timeout_seconds * 2
    for _ in range(3):
        remaining = min(timeout_seconds, deadline - time.monotonic())
        if remaining <= 0:
            raise ModelCallTimeout(f'model call exceeded {timeout_seconds:g}s')
        raw = _call_model(
            client, prompt + (f'\nPrevious schema error: {error}' if error else ''), remaining,
        )
        try:
            turn = AgentTurn.model_validate(parse_json_object(raw))
            if turn.action not in available:
                raise ValueError(f'action {turn.action!r} is not currently available')
            if turn.action == 'read_web':
                unread = _known_urls(state) - set(state.get('read_urls') or ())
                stale = [url for url in turn.urls if url not in unread]
                if stale:
                    raise ValueError(
                        'read_web urls must be unread exact URLs from State web search results or user guidance'
                    )
            if turn.action == 'experiment':
                scope = state.get('repair_scope') if isinstance(state.get('repair_scope'), Mapping) else {}
                if not inside_repair_scope(
                    [item.model_dump() for item in turn.code_scope],
                    scope.get('allowed_roots'), scope.get('blocked_roots'),
                ):
                    raise ValueError(
                        'experiment code_scope must contain only repository-relative planned edit paths '
                        'inside repair_scope; omit evidence-only files and the source/ prefix'
                    )
                demo_method = turn.demo_method.casefold()
                if 'source/' in demo_method or 'algorithm/' in demo_method:
                    raise ValueError(
                        'demo_method must describe isolated behavior implemented at fixed demo/run_demo.py; '
                        'never prescribe a source/ or algorithm/ file path'
                    )
            return turn
        except (ValueError, ValidationError) as exc:
            error = str(exc)
    raise ModelCallError('invalid_agent_response')


def _assess(client: LazyLLMClient, spec: Mapping[str, Any], runs: tuple[Mapping[str, Any], ...],
            timeout_seconds: float) -> AgentAssessment:
    demo_contract = {
        'repair_method': spec.get('repair_method'),
        'success_metric': spec.get('success_metric'),
        'repair_steps': spec.get('repair_steps'),
        'repair_requirements': spec.get('repair_requirements'),
        'demo_method': spec.get('demo_method'),
        'demo_requirements': spec.get('demo_requirements'),
        'live_urls': spec.get('live_urls'),
        'live_probe_ref': spec.get('live_probe_ref'),
        'expected': spec.get('expected'),
    }
    prompt = (
        'Judge whether the real Demo JSON outputs support the fixed repair mechanism and Demo contract. Phase-1 '
        'does not apply the formal source edit, so do not require a changed file or formal entrypoint execution. '
        'However, return inconclusive or rejects if the Demo demonstrates only adjacent behavior, assumes a '
        'precondition unavailable to repair_method, or can pass while the proposed mechanism still fails. '
        'Return supports only when every must_observe is demonstrated and no must_not_observe occurs. Return rejects '
        'when output contradicts demo_method, otherwise inconclusive. A zero exit code alone is not support. '
        'When live_urls are present, the runner-provided _repair_live_probes values are the only acceptable service '
        'observations; reject invented or mock statuses, unreachable required endpoints, and outputs that do not '
        'connect their decision to those values. The trusted runner intentionally performs HTTP before the isolated '
        'Demo; consuming its persisted values is the expected evidence path. Never reject a Demo merely because it '
        'does not call those URLs again—the isolated Demo is forbidden from making network calls. '
        'Return one AgentAssessment JSON object.\n'
        f'AgentAssessment schema: {json.dumps(AgentAssessment.model_json_schema(), ensure_ascii=False)}\n'
        f'Demo contract: {_bounded_json(demo_contract)}\n'
        f'Runs: {_bounded_json([dict(item) for item in runs])}'
    )
    return _validated_model_call(
        client, prompt, AgentAssessment, timeout_seconds, 'invalid_demo_assessment',
    )


def _review_experiment(client: LazyLLMClient, state: Mapping[str, Any], candidate: Mapping[str, Any],
                       timeout_seconds: float) -> ExperimentReview:
    working = _agent_state(state)
    evidence = {
        'root_cause': working.get('target'),
        'user_guidance': working.get('user_guidance'),
        'code_findings': working.get('code_findings'),
        'web_pages': working.get('web_pages'),
    }
    prompt = (
        'Audit one proposed Repair Phase-1 experiment before it is frozen. Analysis already verified the root cause. '
        'Set success_metric_matches_root_cause true only when candidate.success_metric is an existing category metric '
        'and is the earliest available metric that directly measures the broken mechanism. For example, a retrieval '
        'failure must use retrieval_quality_score rather than overall_score or answer_correctness when that metric is '
        'available. A parser failure may use retrieval_quality_score when missing parsed content is observed through '
        'retrieval, but must not use a downstream score merely because it is easier to improve. '
        'Treat candidate.repair_steps as the sole authority for real execution order; repair_method prose cannot '
        'override it. Steps are numbered from 1. Set producer_origin=external_precondition and producer_step=null '
        'when the predicate is already provided before Repair step 1. Set producer_origin=repair_step and its exact '
        '1-based producer_step when a listed step creates it. Set producer_origin=unknown and producer_step=null when '
        'evidence cannot establish the producer. For every input predicate required by repair_method, emit one '
        'causal_checks item. Use role=new_dependency for predicates the repaired code will still wait or branch on. '
        'A failing predicate that repair_method explicitly removes is not a new dependency; if included only to '
        'explain the baseline or Demo contrast, use role=removed_baseline. A removed_baseline order never invalidates '
        'the repaired method. Name the exact action that produces the predicate and the exact repaired wait/check '
        'that consumes it, then record their 1-based step numbers. Leave producer_step or consumer_step null when '
        'evidence does not establish it. A new_dependency is invalid only when its producer step is later than its '
        'consumer step; otherwise the method is a deadlock even when a Demo can fake that predicate as already '
        'present. Equal step numbers are allowed for sequential work inside one atomic step. Do not emit a causal '
        'check for a final output consumed outside the listed repair steps; describe it in produces instead. HTTP '
        '200 endpoint availability and a payload containing a later-created object are separate '
        'predicates and must be separate causal checks. Set demo_distinguishes_method true when the isolated Demo '
        'compares the original failing predicate with the proposed predicate and its output can show old-path '
        'failure, new-path success, and at least a marker that the next step was reached. When the root cause or '
        'proposed method claims that a concrete endpoint is reachable, ready, or returns a specific real response, '
        'demo_distinguishes_method must be false unless candidate.live_urls includes the exact endpoints and '
        'demo_method consumes the trusted _repair_live_probes results; controlled doubles cannot prove that live '
        'claim. A local fallback/control-flow method may instead use clearly labeled controlled inputs to compare '
        'old failure and new output without live_urls, because actual service health is checked after the formal '
        'patch. Merely calling an external client in the eventual patch is not a live-health claim: do not require '
        'a probe when the Demo proves only fallback selection, request shaping, transformation, or output contracts '
        'with controlled responses and explicitly disclaims service health. For an endpoint replacement, both the original '
        'failing URL and proposed URL must appear in candidate.live_urls; one live side plus one simulated side is '
        'not a comparison. It is otherwise a method-level, pure in-process Demo: it need not import modified source, '
        'prove the formal edit, start a service, or prove downstream service health. Those are Phase-2 checks and '
        'must not make demo_distinguishes_method false. repair_steps_unambiguous is true only when every step commits '
        'to one implementation; reject alternatives such as "add a helper or inline it", optional branches, '
        'placeholder choices, and requires entries that say either/or. It also requires repair_method prose and '
        'repair_steps to agree on sync/async behavior, ordering, scope, and success predicates. A kind=context step '
        'must reproduce the actual code order from code_findings; if it reorders calls while claiming to preserve '
        'them, set repair_steps_unambiguous=false. repair_steps_minimal is true only when each kind=change edit is '
        'necessary for the selected method, every newly introduced symbol is actually consumed by a later step, '
        'unchanged behavior is described only when needed to establish causal order as kind=context, and code_scope '
        'contains only symbols that will really be edited. Never broaden a temporal prohibition: "do not wait for X '
        'before Y" permits an existing wait after Y. Never turn "only/prefer only replace A with B" into deleting a '
        'separate later check. Such an extra delete must make repair_steps_minimal=false and guidance contradicted. '
        'Emit exactly one guidance_checks item for every user_guidance entry, in the same order, copying its full '
        'text exactly into guidance. Evaluate its complete meaning, including every negative clause such as '
        'do-not/不要/不得 and every preference clause such as prefer/优先. satisfied is false when the candidate '
        'implements only one clause or adds a stronger precondition that defeats the requested behavior. '
        'contradicted is true whenever any repair step, requirement, Demo expectation, or prose statement conflicts '
        'with any clause; selecting the requested endpoint does not compensate for also waiting on an object that '
        'the guidance explicitly says not to wait for. evidence must quote or closely identify the decisive '
        'candidate content. List concrete issues for any unsatisfied or contradicted guidance as well as a '
        'non-before causal order, false Demo check, or mismatched success metric. Keep at most four non-overlapping issues, each under 500 '
        'characters. In reason, summarize the chronological producer-to-consumer chain rather than judging endpoint '
        'names alone. Return one ExperimentReview JSON object.\n'
        f'ExperimentReview schema: {json.dumps(ExperimentReview.model_json_schema(), ensure_ascii=False)}\n'
        f'Evidence: {_bounded_json(evidence, 35_000)}\n'
        f'Candidate: {_bounded_json(candidate, 20_000)}'
    )
    return _validated_model_call(
        client, prompt, ExperimentReview, timeout_seconds, 'invalid_experiment_review',
    )


def _review_stop(client: LazyLLMClient, state: Mapping[str, Any], turn: AgentTurn,
                 timeout_seconds: float) -> StopReview:
    evidence = _agent_state(state)
    decision = {
        'status': turn.stop_status,
        'reason': turn.reason,
        'repair_scope': state.get('repair_scope'),
    }
    prompt = (
        'Audit a proposed early stop in Repair Phase-1. requirements_resolved is true only when every explicit user '
        'guidance item is either satisfied by persisted evidence or has a concrete evidenced reason it cannot be '
        'satisfied; search snippets do not count as page-body evidence. in_scope_alternative_exists is true when the '
        'verified root cause can still be tested or repaired at any allowed producer OR consumer location. In '
        'particular, a blocked provider file does not justify stopping when an allowed consumer can switch from a '
        'missing predicate to an already-existing one. terminal_justified is true only when the requested blocked, '
        'failed, or exhausted status follows from real evidence and current budgets, not from a preference for one '
        'repair shape. List only concrete reasons for continuing. Return one StopReview JSON object.\n'
        f'StopReview schema: {json.dumps(StopReview.model_json_schema(), ensure_ascii=False)}\n'
        f'Proposed stop: {_bounded_json(decision)}\n'
        f'Evidence: {_bounded_json(evidence, 45_000)}'
    )
    return _validated_model_call(client, prompt, StopReview, timeout_seconds, 'invalid_stop_review')


def review_patch(client: LazyLLMClient, plan: Mapping[str, Any], root_cause: Mapping[str, Any],
                 diff: str, worker_report: Mapping[str, Any], previous_attempts: list[Mapping[str, Any]],
                 timeout_seconds: float) -> PatchReview:
    prompt = (
        'Independently review one formal Repair diff using a fresh context. Do not propose a new repair method. '
        'Set matches_verified_method true only when the actual diff implements the verified mechanism and every '
        'claimed recovery path can activate at the real exception boundary. Candidate evidence from an earlier '
        'attempt may justify covering an earlier occurrence of the same verified failure; that is not a new method. '
        'Set preserves_contracts_and_data_scope false for any output-contract break, hard-coded evaluation detail, '
        'weakened tenant/KB/document filter, unfiltered retry after a scoped query, silent cross-scope data access, '
        'or broader exception handling than the recovery mechanism needs. A fallback must keep every caller-supplied '
        'scope restriction on every retry. Set minimal false for unused helpers, duplicated transformations, broad '
        'catch-all behavior, unrelated edits, or noisy abstractions. Judge the diff, not the worker summary. Return '
        'one PatchReview JSON object.\n'
        f'PatchReview schema: {json.dumps(PatchReview.model_json_schema(), ensure_ascii=False)}\n'
        f'Root cause: {_bounded_json(root_cause, 8_000)}\n'
        f'Verified plan: {_bounded_json(plan, 16_000)}\n'
        f'Previous candidate evidence: {_bounded_json(previous_attempts[-2:], 10_000)}\n'
        f'Worker report: {_bounded_json(worker_report, 8_000)}\n'
        f'Diff: {diff[:40_000]}'
    )
    return _validated_model_call(client, prompt, PatchReview, timeout_seconds, 'invalid_patch_review')


def _validated_model_call(client: LazyLLMClient, prompt: str, model_type: type[BaseModel],
                          timeout_seconds: float, error_code: str) -> Any:
    deadline = time.monotonic() + timeout_seconds
    validation_error = ''
    for _ in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelCallTimeout(f'model call exceeded {timeout_seconds:g}s')
        raw = _call_model(
            client,
            prompt + (f'\nPrevious schema error: {validation_error}' if validation_error else ''),
            remaining,
        )
        try:
            return model_type.model_validate(parse_json_object(raw))
        except (ValueError, ValidationError) as exc:
            validation_error = str(exc)
    raise ModelCallError(error_code)


def _known_urls(state: Mapping[str, Any]) -> set[str]:
    urls = {
        str(item.get('url') or '').strip()
        for search in state.get('web_searches') or ()
        for item in search.get('results') or ()
        if isinstance(item, Mapping) and str(item.get('url') or '').strip()
    }
    for guidance in state.get('user_guidance') or ():
        urls.update(
            token.rstrip('.,;，。；')
            for token in str(guidance).split()
            if token.startswith(('http://', 'https://'))
        )
    return urls


def _guidance_evidence_issues(state: Mapping[str, Any]) -> list[str]:
    """Reject tool-use claims that have no persisted tool evidence."""
    searches = state.get('web_searches') or ()
    has_search = any(
        isinstance(item, Mapping)
        and item.get('status') == 'completed'
        and bool(item.get('results'))
        for item in searches
    )
    page_groups = state.get('web_pages') or ()
    has_readable_page = any(
        isinstance(page, Mapping)
        and page.get('status') == 'readable'
        and isinstance(page.get('content_ref'), Mapping)
        for group in page_groups if isinstance(group, Mapping)
        for page in group.get('pages') or ()
    )
    issues = []
    for index, guidance in enumerate(state.get('user_guidance') or (), 1):
        text = str(guidance).casefold()
        requires_search = any(marker in text for marker in (
            '联网', '网络搜索', '网页搜索', 'web search', 'websearch', 'search web',
        ))
        requires_read = any(marker in text for marker in (
            '正文', '网页内容', '读取网页', '阅读网页', 'read page', 'read web',
        )) or (requires_search and '查阅' in text)
        if requires_search and not has_search:
            issues.append(f'guidance_{index}_requires_persisted_web_search_results')
        if requires_read and not has_readable_page:
            issues.append(f'guidance_{index}_requires_persisted_readable_web_page')
    return issues


def _experiment_grounding_issues(state: Mapping[str, Any], candidate: Mapping[str, Any],
                                 review: ExperimentReview) -> list[str]:
    """Mechanically ground claims that model review cannot establish by prose alone."""
    issues = _guidance_evidence_issues(state)
    live_urls = [str(item).strip() for item in candidate.get('live_urls') or () if str(item).strip()]
    claim_text = ' '.join(str(candidate.get(key) or '') for key in ('repair_method', 'demo_method')).casefold()
    service_claim = bool(re.search(r'(?<![a-z0-9_])/(?:[a-z0-9_-]+)(?:/[a-z0-9_-]+)*', claim_text)) and any(
        marker in claim_text for marker in (
            'status 200', 'status 404', 'returned 200', 'returned 404',
            'ready', 'readiness', 'healthy',
        )
    )
    if service_claim and not live_urls:
        issues.append('running_service_claim_requires_live_probes')
    steps = [item for item in candidate.get('repair_steps') or () if isinstance(item, Mapping)]
    lifecycle_markers = ('start', 'register', 'create', 'build', 'reset', '启动', '注册', '创建', '构建', '重置')
    membership_markers = ('contains', 'entry', 'registered', 'registration', 'exists', '存在', '包含', '注册')
    for check in review.causal_checks:
        if (
            check.role != 'new_dependency'
            or check.producer_origin != 'external_precondition'
            or check.consumer_step is None
            or not any(marker in check.predicate.casefold() for marker in membership_markers)
        ):
            continue
        predicate_tokens = _causal_tokens(check.predicate)
        for later in steps[check.consumer_step:]:
            later_text = ' '.join((
                str(later.get('action') or ''),
                *(str(item) for item in later.get('produces') or ()),
            )).casefold()
            if not any(marker in later_text for marker in lifecycle_markers):
                continue
            shared = predicate_tokens & _causal_tokens(later_text)
            if any(len(token) >= 5 for token in shared) or len(shared) >= 2:
                issues.append(
                    f'causal_precondition_may_be_produced_later:consumer_step_{check.consumer_step}'
                )
                break
    return list(dict.fromkeys(issues))


def _causal_tokens(value: str) -> set[str]:
    stopwords = {
        'after', 'before', 'contains', 'data', 'entry', 'from', 'http', 'into', 'list',
        'response', 'returns', 'step', 'that', 'the', 'with',
    }
    return {
        token for token in re.findall(r'[a-z][a-z0-9_]{2,}', value.casefold())
        if token not in stopwords
    }


def _agent_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Build bounded working memory while keeping complete evidence in artifacts."""
    view = dict(state)
    view.pop('read_urls', None)
    view['code_findings'] = [
        {
            'path': item.get('path'),
            'symbol': item.get('symbol'),
            'observation': str(item.get('observation') or '')[:1600],
        }
        for item in list(state.get('code_findings') or ())[-24:]
        if isinstance(item, Mapping)
    ]
    view['failures'] = list(state.get('failures') or ())[-8:]
    view['web_searches'] = [
        {
            'query': search.get('query'),
            'status': search.get('status'),
            'results': [
                {'title': item.get('title'), 'url': item.get('url')}
                for item in search.get('results') or ()
                if isinstance(item, Mapping)
            ],
        }
        for search in state.get('web_searches') or ()
        if isinstance(search, Mapping)
    ]
    return view


def _available_actions(state: Mapping[str, Any], counters: Mapping[str, int],
                       budget: Mapping[str, int]) -> dict[str, str]:
    actions = []
    if counters['web_searches'] < budget['web_searches']:
        actions.append('search_web')
    unread = _known_urls(state) - set(state.get('read_urls') or ())
    if unread and counters['page_reads'] < budget['page_reads']:
        actions.append('read_web')
    if counters['opencode_calls'] < budget['opencode_calls']:
        actions.append('investigate')
    if (
        state.get('code_findings')
        and not _guidance_evidence_issues(state)
        and counters['experiments'] < budget['experiments']
    ):
        actions.append('experiment')
    if state.get('experiment') and counters['opencode_calls'] < budget['opencode_calls']:
        actions.append('revise_demo')
    return {name: _ACTION_CONTRACTS[name] for name in (*actions, 'stop')}


def _call_model(client: LazyLLMClient, prompt: str, timeout_seconds: float) -> Any:
    seconds = max(0.1, float(timeout_seconds))
    attempts = 2 if seconds >= 20 else 1
    deadline = time.monotonic() + seconds
    with _model_deadline(seconds):
        for attempt in range(attempts):
            remaining = max(0.1, deadline - time.monotonic())
            request_timeout = min(45.0, seconds * 0.4) if attempt == 0 and attempts > 1 else remaining
            try:
                return client(
                    prompt, stream=False, response_format={'type': 'json_object'},
                    max_retries=1, timeout=request_timeout, max_tokens=4096,
                    **_structured_model_options(client),
                )
            except ModelCallTimeout:
                raise
            except Exception as exc:
                reason = _model_error_code(exc)
                transient = reason in {
                    'provider_read_timeout', 'provider_connection_error',
                    'provider_stream_interrupted', 'provider_rate_limit', 'provider_server_error',
                }
                if transient and attempt + 1 < attempts:
                    continue
                raise ModelCallError(reason) from exc
    raise ModelCallTimeout(f'model call exceeded {seconds:g}s')


def _structured_model_options(client: object) -> dict[str, object]:
    config = getattr(client, 'llm_config', None)
    role_name = str(getattr(client, 'model', '') or '')
    role = config.get(role_name) if isinstance(config, Mapping) else None
    try:
        provider, _ = resolve_evo_model(role)
    except EvoModelConfigError:
        return {}
    return {'thinking': {'type': 'disabled'}} if provider == 'deepseek' else {}


def _model_error_code(exc: Exception) -> str:
    message = str(exc).casefold()
    if 'timed out' in message or 'timeout' in message:
        return 'provider_read_timeout'
    if 'connection' in message:
        return 'provider_connection_error'
    if any(marker in message for marker in ('chunked', 'premature', 'incomplete read', 'stream interrupted')):
        return 'provider_stream_interrupted'
    if '429:' in message or 'rate limit' in message:
        return 'provider_rate_limit'
    if any(f'{status}:' in message for status in range(500, 600)):
        return 'provider_server_error'
    if '401:' in message or '403:' in message or 'authentication' in message:
        return 'provider_auth_error'
    if '400:' in message or 'invalid_request' in message:
        return 'provider_invalid_request'
    return 'provider_error'


@contextmanager
def _model_deadline(timeout_seconds: float):
    seconds = max(0.1, float(timeout_seconds))
    previous_handler = signal.getsignal(signal.SIGALRM)

    def timeout_handler(signum: int, frame: object) -> None:
        raise ModelCallTimeout(f'model call exceeded {seconds:g}s')

    signal.signal(signal.SIGALRM, timeout_handler)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def _bounded_json(value: object, limit: int = 50_000) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + '…'
