from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from evo.service.contracts import ServiceError
from evo.service.core import EvoService


class _PausedCaseGenerationFlow:
    def __init__(self) -> None:
        self.retry_calls: list[tuple[str, str]] = []

    async def snapshot(self, _: str) -> SimpleNamespace:
        return SimpleNamespace(status='paused', current_stage='dataset.case_generation')

    async def retry_stage(self, thread_id: str, stage: str, *, request_id: str) -> SimpleNamespace:
        self.retry_calls.append((thread_id, stage))
        return SimpleNamespace()

    async def configuration(self, _: str) -> SimpleNamespace:
        return SimpleNamespace(values={'automatic': False})


def test_retry_does_not_claim_to_restart_case_generation_while_plan_adjustment_is_required() -> None:
    flow = _PausedCaseGenerationFlow()
    service = object.__new__(EvoService)
    service.flow = flow
    service._control_locks = {}

    with pytest.raises(ServiceError, match='adjusted plan') as error:
        asyncio.run(service.retry('thr-1', {'stage': 'dataset.case_generation', 'command_id': 'retry-1'}))

    assert error.value.status_code == 409
    assert flow.retry_calls == []
