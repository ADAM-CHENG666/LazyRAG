from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from evo import artifacts as A
from evo.artifact_runtime import ArtifactCommit, ArtifactKey, ArtifactRecord, ArtifactRef, PartitionSet
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


class _PausedApprovalFlow:
    def __init__(self) -> None:
        self.resumed = False
        self.resume_calls: list[str] = []
        self.approve_calls: list[tuple[str, str]] = []

    async def configuration(self, _: str) -> SimpleNamespace:
        return SimpleNamespace(values={'automatic': False})

    async def snapshot(self, _: str) -> SimpleNamespace:
        if not self.resumed:
            return SimpleNamespace(status='paused', pending_approval=None)
        return SimpleNamespace(
            status='awaiting_approval',
            pending_approval=SimpleNamespace(stage='dataset.topic_discovery'),
        )

    async def resume(self, thread_id: str) -> SimpleNamespace:
        self.resumed = True
        self.resume_calls.append(thread_id)
        return SimpleNamespace()

    async def approve(self, thread_id: str, stage: str) -> SimpleNamespace:
        self.approve_calls.append((thread_id, stage))
        return SimpleNamespace()


def test_continue_recovers_a_paused_approval_boundary() -> None:
    flow = _PausedApprovalFlow()
    service = object.__new__(EvoService)
    service.flow = flow
    service._control_locks = {}

    asyncio.run(service.continue_thread('thr-1', {'command_id': 'continue-topic'}))

    assert flow.resume_calls == ['thr-1']
    assert flow.approve_calls == [('thr-1', 'dataset.topic_discovery')]


class _MaterialRetryFlow:
    def __init__(self) -> None:
        self.retry_calls: list[tuple[str, str]] = []
        self.commits: list[ArtifactCommit] = []
        self.values = {
            ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES): (3, {
                'chunks': [
                    {'chunk_id': 'chunk-old', 'selected': False},
                    {'chunk_id': 'chunk-new', 'selected': True},
                ],
            }),
            ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS): (2, PartitionSet(('chunk-old',))),
        }

    async def snapshot(self, _: str) -> SimpleNamespace:
        return SimpleNamespace(status='failed', current_stage='dataset.material_preparation')

    async def head(self, _: str, key: ArtifactKey) -> ArtifactRecord | None:
        value = self.values.get(key)
        return None if value is None else ArtifactRecord(ArtifactRef(key, value[0]), 'test')

    async def read(self, _: str, ref: ArtifactRef) -> object:
        version, value = self.values[ref.key]
        assert ref.version == version
        return value

    async def commit(self, _: str, commit: ArtifactCommit) -> SimpleNamespace:
        self.commits.append(commit)
        return SimpleNamespace()

    async def retry_stage(self, thread_id: str, stage: str, *, request_id: str) -> SimpleNamespace:
        del request_id
        self.retry_calls.append((thread_id, stage))
        return SimpleNamespace()

    async def configuration(self, _: str) -> SimpleNamespace:
        return SimpleNamespace(values={'automatic': False})


def test_retry_material_preparation_repairs_stale_chunk_request_partitions() -> None:
    flow = _MaterialRetryFlow()
    service = object.__new__(EvoService)
    service.flow = flow
    service._control_locks = {}

    asyncio.run(service.retry('thr-1', {
        'stage': 'dataset.material_preparation',
        'command_id': 'retry-materials',
    }))

    assert flow.retry_calls == [('thr-1', 'dataset.material_preparation')]
    commit = flow.commits[0]
    assert [write.key for write in commit.writes] == [
        ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS),
        ArtifactKey.partition(A.DATASET_CHUNK_REQUEST, 'chunk-new'),
    ]
    assert commit.writes[0].value == PartitionSet(('chunk-new',))
