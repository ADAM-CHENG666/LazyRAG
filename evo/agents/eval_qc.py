from __future__ import annotations

import json
from typing import Any

from evo.conductor.prompts import load as load_prompt
from evo.domain import EDGE_SPECS
from evo.harness.react import LLMInvoker
from evo.harness.schemas import SCHEMAS
from evo.harness.structured import invoke_structured
from evo.runtime.session import AnalysisSession
from evo.tools.eval_qc import compute_score_from_claims

EVAL_QC_NAME = 'eval_qc'
_CLAIMS_EDGE_ID = 'gt_text_to_gt_answer'


def _example_edge(spec: Any) -> str:
    if spec.id == _CLAIMS_EDGE_ID:
        return (
            f'    {{"id": "{spec.id}", "claims": ['
            '{"text": "...", "judgment": "supported"}, '
            '{"text": "...", "judgment": "unsupported"}'
            '], "reason": "..."}'
        )
    return f'    {{"id": "{spec.id}", "reason": "...", "score": 0.72}}'


def _build_eval_qc_prompt() -> str:
    prompt = load_prompt(EVAL_QC_NAME)
    edge_lines = '\n'.join(
        f'- `{spec.id}` (anchor=`{spec.anchor}`, target=`{spec.target}`): {spec.definition}'
        for spec in EDGE_SPECS
    )
    edge_id_lines = '\n'.join(f'  - `{spec.id}`' for spec in EDGE_SPECS)
    example_edges = ',\n'.join(_example_edge(spec) for spec in EDGE_SPECS)
    return (
        prompt
        .replace('{{EDGE_DEFINITIONS}}', edge_lines)
        .replace('{{EDGE_ID_LIST}}', edge_id_lines)
        .replace('{{EDGE_JSON_EXAMPLE}}', example_edges)
    )


def _enrich_computed_scores(parsed: dict[str, Any]) -> dict[str, Any]:
    edges = parsed.get('edges')
    if not isinstance(edges, list):
        return parsed
    for item in edges:
        if (
            isinstance(item, dict)
            and item.get('id') == _CLAIMS_EDGE_ID
            and 'claims' in item
        ):
            item['score'] = compute_score_from_claims(item.get('claims'))
    return parsed


def run_eval_qc(
    session: AnalysisSession,
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
) -> dict[str, Any]:
    invoker = LLMInvoker(session=session, system_prompt=_build_eval_qc_prompt(), llm=llm)
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    parsed = invoke_structured(
        session,
        invoker,
        user,
        agent=EVAL_QC_NAME,
        schema=SCHEMAS[EVAL_QC_NAME],
    )
    return _enrich_computed_scores(parsed)
