from __future__ import annotations

from typing import Any

from evo.domain import JudgeRecord
from evo.runtime.config import EvalQCConfig


# ---- Shared preprocessing -------------------------------------------------

def resolve_gt_text(judge: JudgeRecord) -> list[str]:
    out: list[str] = []
    for item in judge.gt_text:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


# ---- Stage A: report-level hard filter -----------------------------------

def apply_hard_filter(
    judge: JudgeRecord, *, score_field: str, config: EvalQCConfig,
) -> tuple[bool, list[str], str | None]:
    raw = getattr(judge, score_field, None)
    tags: list[str] = []
    score_val: float | None = None
    if isinstance(raw, (int, float)):
        score_val = float(raw)
    if score_val is None:
        tags.append('ac_missing')
    elif score_val < config.a_ac_threshold:
        tags.append('ac_low')
    report_bad = len(tags) > 0
    severity = _severity_from_score(score_val, report_bad)
    return report_bad, tags, severity


# ---- Stage B: rule-based reject ------------------------------------------

def apply_hard_rules(
    *,
    query: str,
    gt_answer: str,
    gt_text: list[str],
    key_points: list[str],
) -> list[str]:
    tags: list[str] = []
    if not str(gt_answer).strip():
        tags.append('gt_answer_empty')
    if len(gt_text) == 0:
        tags.append('gt_text_empty')
    if len(key_points) == 0:
        tags.append('key_points_empty')
    if not str(query).strip():
        tags.append('query_empty')
    return tags


def build_b_reject_reason(tags: list[str]) -> str:
    if not tags:
        return ''
    joined = ', '.join(tags)
    return f'B-stage hard rule rejected: {joined}.'


# ---- Stage C: edge score normalization -----------------------------------

def threshold_for_edge(edge_id: str, config: EvalQCConfig) -> float:
    _ = edge_id
    return config.c_edge_default_threshold


def _severity_from_score(score_val: float | None, report_bad: bool) -> str | None:
    if not report_bad:
        return None
    if score_val is None or score_val < 0.1:
        return 'high'
    if score_val < 0.3:
        return 'medium'
    return 'low'


def normalize_edge_output(raw: Any) -> tuple[str, float, str]:
    if not isinstance(raw, dict):
        return '', 0.0, 'invalid edge payload'
    edge_id = str(raw.get('id', '')).strip()
    score = raw.get('score', 0.0)
    try:
        score_val = float(score)
    except (TypeError, ValueError):
        score_val = 0.0
    score_val = max(0.0, min(1.0, score_val))
    reason = str(raw.get('reason', '')).strip()
    return edge_id, score_val, reason
