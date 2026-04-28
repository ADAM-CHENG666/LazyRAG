from __future__ import annotations

import json
from typing import Any

from evo.conductor.prompts import load as load_prompt
from evo.domain import EDGE_SPECS
from evo.harness.react import LLMInvoker
from evo.harness.schemas import SCHEMAS
from evo.harness.structured import invoke_structured
from evo.runtime.session import AnalysisSession

EVAL_QC_NAME = 'eval_qc'


def _build_eval_qc_prompt() -> str:
    prompt = load_prompt(EVAL_QC_NAME)
    edge_lines = '\n'.join(
        f'- `{spec.id}` (anchor=`{spec.anchor}`, target=`{spec.target}`): {spec.definition}'
        for spec in EDGE_SPECS
    )
    edge_id_lines = '\n'.join(f'  - `{spec.id}`' for spec in EDGE_SPECS)
    example_edges = ',\n'.join(
        f'    {{"id": "{spec.id}", "score": 0.72, "reason": "..."}}'
        for spec in EDGE_SPECS
    )
    return (
        prompt
        .replace('{{EDGE_DEFINITIONS}}', edge_lines)
        .replace('{{EDGE_ID_LIST}}', edge_id_lines)
        .replace('{{EDGE_JSON_EXAMPLE}}', example_edges)
    )


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
    return parsed
