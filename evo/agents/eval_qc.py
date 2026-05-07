from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from evo.conductor.prompts import load as load_prompt
from evo.domain import EDGE_SPECS
from evo.harness.react import LLMInvoker
from evo.harness.schemas import SCHEMAS
from evo.harness.structured import invoke_structured
from evo.runtime.session import AnalysisSession

EVAL_QC_NAME = 'eval_qc'
_CLAIMS_EDGE_ID = 'gt_text_to_gt_answer'
_SPLIT_NAME = 'eval_qc_split'
_WITH_CLAIMS_NAME = 'eval_qc_with_claims'
_FAILURE_REASON = 'eval_qc two-phase failed'
_FALLBACK_REASON = 'eval_qc fallback failed'


def _example_edge(spec: Any) -> str:
    return f'    {{"id": "{spec.id}", "reason": "...", "score": 0.72}}'


def _build_eval_qc_prompt() -> str:
    return _build_eval_qc_with_claims_prompt()


def _build_eval_qc_single_phase_prompt() -> str:
    return _build_prompt(EVAL_QC_NAME)


def _build_eval_qc_with_claims_prompt() -> str:
    return _build_prompt(_WITH_CLAIMS_NAME, exclude_edge_ids={_CLAIMS_EDGE_ID})


def _build_prompt(name: str, exclude_edge_ids: set[str] | None = None) -> str:
    prompt = load_prompt(name)
    edge_specs = [
        spec for spec in EDGE_SPECS
        if not (exclude_edge_ids and spec.id in exclude_edge_ids)
    ]
    edge_lines = '\n'.join(
        f'- `{spec.id}` (anchor=`{spec.anchor}`, target=`{spec.target}`): {spec.definition}'
        for spec in edge_specs
    )
    edge_id_lines = '\n'.join(f'  - `{spec.id}`' for spec in edge_specs)
    example_edges = ',\n'.join(_example_edge(spec) for spec in edge_specs)
    return (
        prompt
        .replace('{{EDGE_DEFINITIONS}}', edge_lines)
        .replace('{{EDGE_ID_LIST}}', edge_id_lines)
        .replace('{{EDGE_JSON_EXAMPLE}}', example_edges)
    )


def _enrich_computed_scores(parsed: dict[str, Any]) -> dict[str, Any]:
    claims = parsed.pop('claims_judgment', None)
    if not isinstance(claims, list):
        return parsed
    score = min(
        (float(claim.get('score', 0.0)) for claim in claims if isinstance(claim, dict)),
        default=0.0,
    )
    gt_edge = {
        'id': _CLAIMS_EDGE_ID,
        'score': max(0.0, min(1.0, score)),
        'reason': parsed.get('summary_reason', ''),
        'claims': claims,
    }
    _patch_gt_text_edge(parsed, gt_edge)
    return parsed


def _build_split_prompt() -> str:
    return load_prompt(_SPLIT_NAME)


def _fallback_failure_claim() -> dict[str, Any]:
    return {
        'id': 'fallback',
        'text': _FALLBACK_REASON,
        'score': 0.0,
        'evidence': '',
    }


def _two_phase_failure_edge() -> dict[str, Any]:
    return {
        'id': _CLAIMS_EDGE_ID,
        'claims': [_fallback_failure_claim()],
        'reason': _FAILURE_REASON,
    }


def _normalize_split_claims(raw_claims: Any) -> list[dict[str, str]]:
    if not isinstance(raw_claims, list):
        return []
    claims: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_claims:
        if not isinstance(item, dict):
            return []
        claim_id = str(item.get('id') or '').strip()
        text = str(item.get('text') or '').strip()
        if not claim_id or not text or claim_id in seen:
            return []
        seen.add(claim_id)
        claims.append({'id': claim_id, 'text': text})
    return claims


def _run_split_claims(
    session: AnalysisSession,
    *,
    gt_answer: Any,
    llm: Any | None = None,
) -> list[dict[str, str]]:
    split_invoker = LLMInvoker(
        session=session,
        system_prompt=_build_split_prompt(),
        llm=llm,
    )
    split_user = json.dumps({'gt_answer': gt_answer}, ensure_ascii=False, indent=2)
    split_parsed = invoke_structured(
        session,
        split_invoker,
        split_user,
        agent=_SPLIT_NAME,
        schema=SCHEMAS[_SPLIT_NAME],
    )
    return _normalize_split_claims(split_parsed.get('claims'))


def _run_eval_qc_with_claims(
    session: AnalysisSession,
    payload: dict[str, Any],
    split_claims: list[dict[str, str]],
    *,
    llm: Any | None = None,
) -> dict[str, Any]:
    invoker = LLMInvoker(
        session=session,
        system_prompt=_build_eval_qc_with_claims_prompt(),
        llm=llm,
    )
    user_payload = {**payload, 'gt_answer_claims': split_claims}
    user = json.dumps(user_payload, ensure_ascii=False, indent=2)
    return invoke_structured(
        session,
        invoker,
        user,
        agent=_WITH_CLAIMS_NAME,
        schema=SCHEMAS[_WITH_CLAIMS_NAME],
    )


def _run_single_phase_eval_qc(
    session: AnalysisSession,
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
) -> dict[str, Any]:
    invoker = LLMInvoker(
        session=session,
        system_prompt=_build_eval_qc_single_phase_prompt(),
        llm=llm,
    )
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    return invoke_structured(
        session,
        invoker,
        user,
        agent=EVAL_QC_NAME,
        schema=SCHEMAS[EVAL_QC_NAME],
    )


def _run_single_phase_fallback(
    session: AnalysisSession,
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
    error: str = '',
) -> dict[str, Any]:
    if error:
        session.telemetry.emit(
            'eval_qc_two_phase_fallback',
            agent=EVAL_QC_NAME,
            error=error[:500],
        )
    try:
        parsed = _run_single_phase_eval_qc(session, payload, llm=llm)
        return _ensure_schema_compatible_eval_output(parsed)
    except Exception as exc:
        session.telemetry.emit(
            'eval_qc_single_phase_fallback_failed',
            agent=EVAL_QC_NAME,
            error=str(exc)[:500],
        )
        return _eval_qc_failure_output()


def _run_two_phase_eval_qc(
    session: AnalysisSession,
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
) -> dict[str, Any]:
    try:
        split_claims = _run_split_claims(
            session,
            gt_answer=payload.get('gt_answer', ''),
            llm=llm,
        )
        if not split_claims:
            return _run_single_phase_fallback(
                session,
                payload,
                llm=llm,
                error='split phase returned no claims',
            )
        parsed = _run_eval_qc_with_claims(
            session,
            payload,
            split_claims,
            llm=llm,
        )
        if not _ensure_claims_judgment(parsed, split_claims):
            return _run_single_phase_fallback(
                session,
                payload,
                llm=llm,
                error='two-phase claims_judgment failed validation',
            )
        schema_errors = _eval_qc_with_claims_schema_errors(parsed)
        if schema_errors:
            return _run_single_phase_fallback(
                session,
                payload,
                llm=llm,
                error='; '.join(schema_errors),
            )
        return parsed
    except Exception as exc:
        session.telemetry.emit(
            'eval_qc_two_phase_failed',
            agent=EVAL_QC_NAME,
            error=str(exc)[:500],
        )
        return _run_single_phase_fallback(
            session,
            payload,
            llm=llm,
            error=str(exc),
        )


def _eval_qc_failure_output() -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    for spec in EDGE_SPECS:
        if spec.id == _CLAIMS_EDGE_ID:
            edges.append(_two_phase_failure_edge())
        else:
            edges.append({
                'id': spec.id,
                'score': 0.0,
                'reason': _FAILURE_REASON,
            })
    return {
        'edges': edges,
        'summary_reason': _FAILURE_REASON,
    }


def _eval_qc_schema_errors(parsed: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(SCHEMAS[EVAL_QC_NAME])
    return [error.message for error in validator.iter_errors(parsed)]


def _eval_qc_with_claims_schema_errors(parsed: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(SCHEMAS[_WITH_CLAIMS_NAME])
    return [error.message for error in validator.iter_errors(parsed)]


def _ensure_schema_compatible_eval_output(parsed: dict[str, Any]) -> dict[str, Any]:
    edges = parsed.get('edges')
    if not isinstance(edges, list):
        return _eval_qc_failure_output()
    for item in edges:
        if not isinstance(item, dict) or item.get('id') != _CLAIMS_EDGE_ID:
            continue
        claims = item.get('claims')
        if not isinstance(claims, list) or not claims:
            _patch_gt_text_edge(parsed, _two_phase_failure_edge())
        break
    else:
        _patch_gt_text_edge(parsed, _two_phase_failure_edge())
    if _eval_qc_schema_errors(parsed):
        return _eval_qc_failure_output()
    return parsed


def _ensure_claims_judgment(
    parsed: dict[str, Any],
    split_claims: list[dict[str, str]],
) -> bool:
    claims = parsed.get('claims_judgment')
    if not isinstance(claims, list):
        return False
    expected_by_id = {claim['id']: claim['text'] for claim in split_claims}
    out_by_id: dict[str, dict[str, Any]] = {}
    for claim in claims:
        if not isinstance(claim, dict):
            return False
        claim_id = str(claim.get('id') or '').strip()
        text = str(claim.get('text') or '').strip()
        evidence = str(claim.get('evidence') or '').strip()
        score_raw = claim.get('score')
        if claim_id not in expected_by_id or claim_id in out_by_id:
            return False
        if text != expected_by_id[claim_id]:
            return False
        if isinstance(score_raw, bool) or not isinstance(score_raw, (int, float)):
            return False
        score = float(score_raw)
        if score < 0.0 or score > 1.0:
            return False
        if score >= 0.2 and not evidence:
            return False
        if score < 0.2 and evidence:
            return False
        out_by_id[claim_id] = {
            'id': claim_id,
            'text': text,
            'score': score,
            'evidence': evidence,
        }
    if set(out_by_id) != set(expected_by_id):
        return False
    parsed['claims_judgment'] = [out_by_id[claim['id']] for claim in split_claims]
    return True


def _patch_gt_text_edge(parsed: dict[str, Any], replacement: dict[str, Any]) -> dict[str, Any]:
    edges = parsed.get('edges')
    if not isinstance(edges, list):
        parsed['edges'] = [replacement]
        return parsed
    for index, item in enumerate(edges):
        if isinstance(item, dict) and item.get('id') == _CLAIMS_EDGE_ID:
            edges[index] = replacement
            return parsed
    edges.append(replacement)
    return parsed


def run_eval_qc(
    session: AnalysisSession,
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
) -> dict[str, Any]:
    return _enrich_computed_scores(_run_two_phase_eval_qc(session, payload, llm=llm))
