"""Standalone, session-free entrypoint for eval-set quality check.

The callable handed to the backend: takes a batch of cases plus an injected
`judge_llm`, runs field validation (B) then the three query-anchored logic
judgments (C) per case, and returns one result per case in the public contract.

Per-case contract:
    in : {id, query, gt_answer, gt_text, key_points}
    out: {id, passed, [reject], [logics], summary}

See docs/domains/eval_qc/design/02_task_endpoint_design.md for the full spec.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from evo.agents.eval_qc import run_eval_qc
from evo.domain import LOGIC_IDS
from evo.runtime.config import EvoConfig, load_config
from evo.runtime.session import create_session, session_scope
from evo.tools.eval_qc import missing_required_fields


def run_eval_qc_batch(
    cases: Sequence[dict[str, Any]],
    *,
    judge_llm: Any,
    config: EvoConfig | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Run eval_qc over a batch of cases.

    Args:
        cases: each is {id, query, gt_answer, gt_text, key_points}.
        judge_llm: injected LLM callable (replaces the session-bound client).
        config: full EvoConfig (threshold lives in config.eval_qc); load_config() if None.
        on_progress: called with (done, total) after each case.
        cancel: polled before each case; returns True to stop early (partial results).

    Returns:
        One result dict per processed case, in input order.
    """
    cfg = config if isinstance(config, EvoConfig) else load_config()
    threshold = cfg.eval_qc.threshold
    total = len(cases)
    results: list[dict[str, Any]] = []
    with session_scope(session := create_session(cfg)):
        for index, case in enumerate(cases):
            if cancel is not None and cancel():
                break
            results.append(_check_case(session, case, judge_llm=judge_llm, threshold=threshold))
            if on_progress is not None:
                on_progress(index + 1, total)
    return results


def _check_case(session: Any, case: dict[str, Any], *, judge_llm: Any, threshold: float) -> dict[str, Any]:
    case_id = case.get('id')
    query = _text(case.get('query'))
    gt_answer = _text(case.get('gt_answer'))
    gt_text = _text_list(case.get('gt_text'))
    key_points = _text_list(case.get('key_points'))

    # Stage B: required-field validation (cheap, deterministic, no LLM).
    missing = missing_required_fields(query=query, gt_answer=gt_answer, gt_text=gt_text, key_points=key_points)
    if missing:
        return {
            'id': case_id,
            'passed': False,
            'reject': {'type': 'error_format_input', 'missing_fields': missing},
            'summary': f"{'、'.join(missing)} 为空",
        }

    # Stage C: three query-anchored logic judgments via the (session-driven) LLM.
    parsed = run_eval_qc(
        session,
        {'query': query, 'gt_answer': gt_answer, 'gt_text': gt_text, 'key_points': key_points},
        llm=judge_llm,
    )
    logics = _judge_logics(parsed.get('judgments'), threshold)
    passed = len(logics) == len(LOGIC_IDS) and all(logic['passed'] for logic in logics.values())
    result: dict[str, Any] = {
        'id': case_id,
        'passed': passed,
        'logics': logics,
        'summary': str(parsed.get('summary_reason') or ''),
    }
    if not passed:
        result['reject'] = {'type': 'error_unpassed'}
    return result


def _judge_logics(judgments: Any, threshold: float) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(judgments, list):
        for entry in judgments:
            if not isinstance(entry, dict):
                continue
            logic_id = str(entry.get('logic_id') or '').strip()
            if logic_id not in LOGIC_IDS or logic_id in by_id:
                continue
            raw_level = entry.get('level')
            level = float(raw_level) if isinstance(raw_level, (int, float)) else 0.0
            reason = str(entry.get('reason') or '').strip()
            by_id[logic_id] = {'level': level, 'passed': level >= threshold, 'reason': reason}
    for logic_id in LOGIC_IDS:
        by_id.setdefault(logic_id, {'level': 0.0, 'passed': False, 'reason': 'logic missing in model output'})
    return by_id


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for item in value if (text := str(item).strip())]


__all__ = ['run_eval_qc_batch']
