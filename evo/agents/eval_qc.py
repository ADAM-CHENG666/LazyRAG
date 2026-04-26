from __future__ import annotations

import json
from typing import Any

from evo.conductor.prompts import load as load_prompt
from evo.harness.react import LLMInvoker
from evo.harness.schemas import SCHEMAS
from evo.harness.structured import invoke_structured
from evo.runtime.session import AnalysisSession

EVAL_QC_NAME = 'eval_qc'


def run_eval_qc(
    session: AnalysisSession,
    payload: dict[str, Any],
    *,
    llm: Any | None = None,
) -> dict[str, Any]:
    invoker = LLMInvoker(session=session, system_prompt=load_prompt(EVAL_QC_NAME), llm=llm)
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    parsed = invoke_structured(
        session,
        invoker,
        user,
        agent=EVAL_QC_NAME,
        schema=SCHEMAS[EVAL_QC_NAME],
    )
    return parsed
