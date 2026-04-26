from __future__ import annotations

from typing import Any

from evo.agents.eval_qc import run_eval_qc
from evo.domain import EdgeResult, EvalFeature
from evo.runtime.session import AnalysisSession
from evo.tools.eval_qc import (
    apply_hard_filter, apply_hard_rules, build_b_reject_reason,
    normalize_edge_output, resolve_gt_text, threshold_for_edge,
)

EDGE_IDS: tuple[str, ...] = (
    'query_to_gt_answer',
    'query_to_gt_text',
    'query_to_key_points',
    'gt_text_to_gt_answer',
    'gt_answer_to_key_points',
)


def run_eval_qc_step(session: AnalysisSession) -> int:
    config = session.config.eval_qc
    # Disabled mode: skip eval_qc computation; downstream pass-set logic is handled by session.
    if not config.enabled:
        session.set_eval_features({})
        return 0

    score_field = config.a_score_field or session.config.badcase_score_field
    out: dict[str, EvalFeature] = {}
    for dataset_id, judge in session.iter_judge():
        out[dataset_id] = _run_case(session, dataset_id, judge, score_field=score_field)

    session.set_eval_features(out)
    return len(out)


def _run_case(
    session: AnalysisSession,
    dataset_id: str,
    judge: Any,
    *,
    score_field: str,
) -> EvalFeature:
    # Stage A: report-level hard filter (fast gate by score/missing tags).
    report_bad, report_tags, severity = apply_hard_filter(
        judge, score_field=score_field, config=session.config.eval_qc,
    )
    feature = EvalFeature(
        dataset_id=dataset_id,
        a_report_bad=report_bad,
        a_report_bad_tags=report_tags,
        a_severity=severity,
    )
    if not report_bad:
        feature.qc_passed = True
        return feature

    # Stage B: rule-based reject for missing/invalid required fields.
    query = _resolve_query(session, judge)
    gt_answer = _normalize_optional_text(judge.gt_answer)
    gt_text = resolve_gt_text(judge)
    key_points = [str(k) for k in (judge.key or []) if str(k).strip()]
    b_tags = apply_hard_rules(
        query=query,
        gt_answer=gt_answer,
        gt_text=gt_text,
        key_points=key_points,
    )
    feature.b_reject_tags = b_tags
    if b_tags:
        feature.b_reject_reason = build_b_reject_reason(b_tags)
        feature.qc_passed = False
        return feature

    # Stage C: LLM edge scoring + normalized threshold checks.
    payload = {
        'query': query,
        'gt_answer': gt_answer,
        'gt_text': gt_text,
        'key_points': key_points,
    }
    parsed = run_eval_qc(session, payload)
    feature.c_summary_reason = str(parsed.get('summary_reason', '') or '')
    if feature.llm_raw is None:
        feature.llm_raw = parsed
    feature.c_edge_results = _to_edge_results(parsed.get('edges', []), session)
    feature.qc_passed = len(feature.c_edge_results) == len(EDGE_IDS) and all(
        e.passed for e in feature.c_edge_results.values()
    )
    return feature


def _resolve_query(session: AnalysisSession, judge: Any) -> str:
    trace = session.get_trace(judge.trace_id)
    if trace:
        query = _normalize_optional_text(trace.query)
        if query:
            return query
    extra_query = judge.extra.get('query') if isinstance(judge.extra, dict) else None
    return _normalize_optional_text(extra_query)


def _normalize_optional_text(value: Any) -> str:
    if value is None:
        return ''
    return str(value).strip()


def _to_edge_results(raw_edges: Any, session: AnalysisSession) -> dict[str, EdgeResult]:
    by_id: dict[str, EdgeResult] = {}
    if isinstance(raw_edges, list):
        for item in raw_edges:
            edge_id, score, reason = normalize_edge_output(item)
            if edge_id and edge_id in EDGE_IDS:
                th = threshold_for_edge(edge_id, session.config.eval_qc)
                by_id[edge_id] = EdgeResult(
                    score=score,
                    reason=reason,
                    threshold=th,
                    passed=score >= th,
                )

    for edge_id in EDGE_IDS:
        if edge_id in by_id:
            continue
        th = threshold_for_edge(edge_id, session.config.eval_qc)
        by_id[edge_id] = EdgeResult(
            score=0.0,
            reason='edge missing in model output',
            threshold=th,
            passed=False,
        )
    return by_id
