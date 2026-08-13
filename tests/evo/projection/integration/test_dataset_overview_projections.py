from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo import artifacts as A
from evo.artifact_flow import StageProgress, StageSnapshot
from evo.artifact_runtime import ArtifactKey, ArtifactRecord, ArtifactRef
from evo.service.api import create_app
from evo.service.projections import ProjectionService


_MATERIAL_STAGE = 'dataset.material_preparation'
_TOPIC_STAGE = 'dataset.topic_discovery'


class _OverviewFlow:
    def __init__(self, values: dict[ArtifactKey, dict[int, dict]], statuses: dict[str, str]) -> None:
        self.values = values
        self.statuses = statuses

    async def has_run(self, _: str) -> bool:
        return True

    async def head(self, _: str, key: ArtifactKey) -> ArtifactRecord | None:
        versions = self.values.get(key, {})
        return None if not versions else self._record(key, max(versions))

    async def record(self, _: str, ref: ArtifactRef) -> ArtifactRecord | None:
        return self._record(ref.key, ref.version) if ref.version in self.values.get(ref.key, {}) else None

    async def read(self, _: str, ref: ArtifactRef) -> dict:
        return self.values[ref.key][ref.version]

    async def stage_snapshot(self, _: str, stage: str) -> StageSnapshot:
        return StageSnapshot(StageProgress(stage, ArtifactKey.scalar('test.result'), status=self.statuses[stage]))

    @staticmethod
    def _record(key: ArtifactKey, version: int) -> ArtifactRecord:
        return ArtifactRecord(ArtifactRef(key, version), producer='test')


def _service(*, values: dict[ArtifactKey, dict[int, dict]], statuses: dict[str, str]) -> ProjectionService:
    return ProjectionService(_OverviewFlow(values, statuses), definition=None)


def _enable_dataset_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, 'STEPS', (_MATERIAL_STAGE, _TOPIC_STAGE))


def test_materials_overview_projects_current_manifest_and_stage_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(
        values={ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_MANIFEST): {4: {
            'source': {'case_counts': {'target': 100, 'imported': 20, 'automatic': 80}},
            'summary': {'chunk_counts': {'scanned': 800, 'effective': 600, 'selected': 240}},
            'warnings': ['chunk candidate capacity is short by 5; selected 240'],
        }}},
        statuses={_MATERIAL_STAGE: 'completed'},
    )

    result = asyncio.run(service.materials_overview('thr-1'))

    assert result == {
        'thread_id': 'thr-1',
        'revision': service._build_revision((ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_MANIFEST), 4),)),
        'status': 'completed',
        'case_plan': {'target': 100, 'imported': 20, 'automatic': 80},
        'chunks': {
            'scanned': 800, 'effective': 600, 'selected': 240,
            'effective_rate': 0.75, 'selection_rate': 0.4,
        },
        'warnings': ['chunk candidate capacity is short by 5; selected 240'],
    }


def test_materials_overview_returns_current_status_without_a_stable_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(values={}, statuses={_MATERIAL_STAGE: 'running'})

    assert asyncio.run(service.materials_overview('thr-1')) == {
        'thread_id': 'thr-1', 'revision': None, 'status': 'running',
        'case_plan': None, 'chunks': None, 'warnings': [],
    }


def test_materials_overview_uses_null_rates_when_their_denominators_are_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(
        values={ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_MANIFEST): {1: {
            'source': {'case_counts': {'target': 0, 'imported': 0, 'automatic': 0}},
            'summary': {'chunk_counts': {'scanned': 0, 'effective': 0, 'selected': 0}},
            'warnings': [],
        }}},
        statuses={_MATERIAL_STAGE: 'completed'},
    )

    assert asyncio.run(service.materials_overview('thr-1'))['chunks'] == {
        'scanned': 0, 'effective': 0, 'selected': 0,
        'effective_rate': None, 'selection_rate': None,
    }


def test_topics_overview_projects_current_manifest_and_calculates_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(
        values={ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {3: {
            'stats': {
                'total_topic_count': 30,
                'question_types': {'precision': {'count': 18}, 'reasoning': {'count': 12}},
            },
        }}},
        statuses={_TOPIC_STAGE: 'completed'},
    )

    result = asyncio.run(service.topics_overview('thr-1'))

    assert result == {
        'thread_id': 'thr-1',
        'revision': service._build_revision((ArtifactRef(ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST), 3),)),
        'status': 'completed',
        'total_topics': 30,
        'question_types': {
            'precision': {'count': 18, 'rate': 0.6},
            'reasoning': {'count': 12, 'rate': 0.4},
        },
    }


def test_topics_overview_keeps_a_valid_empty_manifest_distinct_from_no_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    empty = _service(
        values={ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {1: {
            'stats': {'total_topic_count': 0, 'question_types': {'precision': {'count': 0}, 'reasoning': {'count': 0}}},
        }}},
        statuses={_TOPIC_STAGE: 'completed'},
    )
    missing = _service(values={}, statuses={_TOPIC_STAGE: 'running'})

    assert asyncio.run(empty.topics_overview('thr-1'))['question_types'] == {
        'precision': {'count': 0, 'rate': None},
        'reasoning': {'count': 0, 'rate': None},
    }
    assert asyncio.run(missing.topics_overview('thr-1')) == {
        'thread_id': 'thr-1', 'revision': None, 'status': 'running',
        'total_topics': None, 'question_types': None,
    }


@pytest.mark.parametrize(('path', 'method'), [
    ('/threads/thr-1/dataset/materials/overview', 'materials_overview'),
    ('/threads/thr-1/dataset/topics/overview', 'topics_overview'),
])
def test_overview_handlers_delegate_without_query_parameters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                              path: str, method: str) -> None:
    calls: list[tuple[str, str]] = []

    class _Projections:
        async def materials_overview(self, thread_id: str) -> dict:
            calls.append(('materials_overview', thread_id))
            return {'thread_id': thread_id}

        async def topics_overview(self, thread_id: str) -> dict:
            calls.append(('topics_overview', thread_id))
            return {'thread_id': thread_id}

    class _Service:
        projections = _Projections()

        async def close(self) -> None:
            return None

    async def _open(_: Path) -> _Service:
        return _Service()

    monkeypatch.setattr('evo.service.api.EvoService.open', _open)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(path)
        unsupported = client.get(path, params={'page_size': '50'})

    assert response.status_code == 200
    assert response.json() == {'thread_id': 'thr-1'}
    assert unsupported.status_code == 422
    assert calls == [(method, 'thr-1')]
