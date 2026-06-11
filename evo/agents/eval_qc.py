from __future__ import annotations

import json
from typing import Any

from evo.apply.errors import ApplyError
from evo.conductor.prompts import load as load_prompt
from evo.domain import LOGIC_IDS
from evo.harness.react import LLMInvoker
from evo.harness.schemas import SCHEMAS
from evo.harness.structured import invoke_structured
from evo.runtime.session import AnalysisSession

EVAL_QC_NAME = 'eval_qc'
_EXTRACT_NAME = 'eval_qc_extract'
_JUDGE_NAME = 'eval_qc_judge'
_EXTRACT_EVENT = 'eval_qc_extract_artifacts'
_FAILURE_REASON = 'eval_qc evaluation failed'
_VALID_LEVELS = frozenset({0.1, 0.3, 0.6, 0.9})


# ---- Stage 1: extract — decompose the query into core/qualifier -----------

def _normalize_query(raw_query: Any) -> dict[str, str]:
    if not isinstance(raw_query, dict):
        return {}
    core = str(raw_query.get('core') or '').strip()
    if not core:
        return {}
    return {'core': core, 'qualifier': str(raw_query.get('qualifier') or '').strip()}


def _extract_query(session: AnalysisSession, *, query: Any, llm: Any | None) -> dict[str, str]:
    invoker = LLMInvoker(session=session, system_prompt=load_prompt(_EXTRACT_NAME), llm=llm)
    user = json.dumps({'query': query}, ensure_ascii=False, indent=2)
    parsed = invoke_structured(
        session, invoker, user, agent=_EXTRACT_NAME, schema=SCHEMAS[_EXTRACT_NAME], max_repair=1
    )
    return _normalize_query(parsed.get('query'))


# ---- Stage 2: judge — score the three query-anchored logics ---------------

def _judge(session: AnalysisSession, payload: dict[str, Any], *, query: dict[str, str], llm: Any | None) -> dict[str, Any]:
    invoker = LLMInvoker(session=session, system_prompt=load_prompt(_JUDGE_NAME), llm=llm)
    user = json.dumps(
        {
            'query': query,
            'gt_answer': payload.get('gt_answer', ''),
            'gt_text': payload.get('gt_text', []),
            'key_points': payload.get('key_points', []),
        },
        ensure_ascii=False,
        indent=2,
    )
    return invoke_structured(session, invoker, user, agent=_JUDGE_NAME, schema=SCHEMAS[_JUDGE_NAME])


def _judgments_valid(parsed: dict[str, Any]) -> bool:
    judgments = parsed.get('judgments')
    if not isinstance(judgments, list):
        return False
    seen: set[str] = set()
    for entry in judgments:
        if not isinstance(entry, dict):
            return False
        logic_id = str(entry.get('logic_id') or '').strip()
        if logic_id not in LOGIC_IDS or logic_id in seen:
            return False
        if entry.get('level') not in _VALID_LEVELS:
            return False
        if not str(entry.get('reason') or '').strip():
            return False
        seen.add(logic_id)
    return seen == set(LOGIC_IDS)


def _failure_output() -> dict[str, Any]:
    return {
        'judgments': [
            {'logic_id': logic_id, 'reason': _FAILURE_REASON, 'level': 0.1} for logic_id in LOGIC_IDS
        ],
        'summary_reason': _FAILURE_REASON,
    }


# ---- Orchestration --------------------------------------------------------

def run_eval_qc(session: AnalysisSession, payload: dict[str, Any], *, llm: Any | None = None) -> dict[str, Any]:
    """Run extract → judge for one case; return {judgments, summary_reason}.

    On a transient/local failure the judgment degrades to a per-case failure
    output. A systemic schema-follow failure (ApplyError) is re-raised so the
    caller can fail the whole task instead of silently producing garbage.
    """
    try:
        query = _extract_query(session, query=payload.get('query', ''), llm=llm)
        session.telemetry.emit(_EXTRACT_EVENT, agent=_EXTRACT_NAME, query=query)
        if not query:
            session.telemetry.emit('eval_qc_eval_failed', agent=EVAL_QC_NAME, error='extract returned no query core')
            return _failure_output()
        parsed = _judge(session, payload, query=query, llm=llm)
        if not _judgments_valid(parsed):
            session.telemetry.emit('eval_qc_eval_failed', agent=EVAL_QC_NAME, error='judgments failed validation')
            return _failure_output()
        return parsed
    except ApplyError:
        raise
    except Exception as exc:
        session.telemetry.emit('eval_qc_eval_failed', agent=EVAL_QC_NAME, error=str(exc)[:500])
        return _failure_output()
