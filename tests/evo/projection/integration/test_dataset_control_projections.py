from __future__ import annotations

"""Behavior tests for Dataset material controls and Case topic replacement options."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo import artifacts as A
from evo.artifact_runtime import ArtifactKey, ArtifactRecord, ArtifactRef
from evo.service.contracts import ServiceError
from evo.service.api import create_app
from evo.service.projections import ProjectionService


class _ControlFlow:
    def __init__(self, values: dict[ArtifactKey, dict[int, object]], *, has_thread: bool = True) -> None:
        self.values = values
        self.has_thread_value = has_thread

    async def has_run(self, _thread_id: str) -> bool:
        return self.has_thread_value

    async def head(self, _thread_id: str, key: ArtifactKey) -> ArtifactRecord | None:
        versions = self.values.get(key, {})
        return None if not versions else self._record(key, max(versions))

    async def record(self, _thread_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        return self._record(ref.key, ref.version) if ref.version in self.values.get(ref.key, {}) else None

    async def read(self, _thread_id: str, ref: ArtifactRef) -> object:
        return self.values[ref.key][ref.version]

    @staticmethod
    def _record(key: ArtifactKey, version: int) -> ArtifactRecord:
        return ArtifactRecord(ArtifactRef(key, version), producer='test')


def _service(values: dict[ArtifactKey, dict[int, object]], *, has_thread: bool = True) -> ProjectionService:
    return ProjectionService(_ControlFlow(values, has_thread=has_thread), definition=None)


def _topic(topic_id: str, *, question_type: str = 'precision', chunk_count: int = 1) -> dict:
    return {
        'topic_id': topic_id,
        'name': f'{topic_id} name',
        'question_type': question_type,
        'chunk_ids': [f'{topic_id}-chunk-{index}' for index in range(chunk_count)],
        'chunk_count': chunk_count,
    }


def _topic_options(service: ProjectionService, case_id: str = 'case-1', *, page_size: int = 50,
                   page_token: str = '') -> dict:
    return asyncio.run(service.case_topic_options(
        'thr-1', case_id, page_size=page_size, page_token=page_token,
    ))


def test_material_adjustment_options_projects_current_configuration_and_composite_revision() -> None:
    source_key = ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG)
    selection_key = ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS)
    chunks_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS)
    service = _service({
        source_key: {3: {
            'kb_id': ['kb-a', 'kb-b'],
            'knowledge_base_names': {'kb-a': '产品知识库', 'kb-b': '研究资料库'},
            'csv_data': [], 'target_case_count': 12,
        }},
        selection_key: {5: {
            'knowledge_bases': [
                {'kb_id': 'kb-a', 'included': True},
                {'kb_id': 'kb-b', 'included': False},
            ],
            'excluded_docs': [{'kb_id': 'kb-a', 'doc_id': 'doc-1'}],
        }},
        chunks_key: {2: {'groups': ['block'], 'allowed_types': ['text', 'table']}},
    })

    result = asyncio.run(service.material_adjustment_options('thr-1'))

    assert result['thread_id'] == 'thr-1'
    assert result['revision'] == service._build_revision((
        ArtifactRef(source_key, 3), ArtifactRef(selection_key, 5), ArtifactRef(chunks_key, 2),
    ))
    assert result['target_case_count'] == 12
    assert result['knowledge_bases'] == [
        {'id': 'kb-a', 'name': '产品知识库', 'included': True},
        {'id': 'kb-b', 'name': '研究资料库', 'included': False},
    ]


def test_material_adjustment_options_requires_all_current_configuration_artifacts() -> None:
    service = _service({
        ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG): {1: {
            'kb_id': ['kb-a'], 'csv_data': [], 'target_case_count': 1,
        }},
    })

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.material_adjustment_options('thr-1'))
    assert error.value.status_code == 404


def test_case_topic_options_filters_by_case_lane_and_excludes_current_and_occupied_topics() -> None:
    service = _service({
        ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN): {4: {'items': [
            {'case_id': 'case-1', 'question_type': 'precision', 'difficulty': 'medium', 'topic_id': 'current'},
            {'case_id': 'case-2', 'question_type': 'precision', 'difficulty': 'medium', 'topic_id': 'occupied'},
            {'case_id': 'case-3', 'question_type': 'precision', 'difficulty': 'easy', 'topic_id': 'easy-occupied'},
        ]}},
        ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {7: {'topics': [
            _topic('current', chunk_count=2),
            _topic('occupied', chunk_count=3),
            _topic('candidate-b', chunk_count=2),
            _topic('candidate-a', chunk_count=3),
            _topic('too-short', chunk_count=1),
            _topic('reasoning', question_type='reasoning', chunk_count=3),
            _topic('easy-occupied', chunk_count=1),
        ]}},
    })

    result = _topic_options(service)

    assert result['case_id'] == 'case-1'
    assert [row['topic_id'] for row in result['items']] == ['candidate-a', 'candidate-b']
    assert result['items'] == [
        {'topic_id': 'candidate-a', 'name': 'candidate-a name', 'chunk_count': 3},
        {'topic_id': 'candidate-b', 'name': 'candidate-b name', 'chunk_count': 2},
    ]


def test_case_topic_options_paginates_a_fixed_plan_and_topic_snapshot() -> None:
    plan_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN)
    topic_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
    service = _service({
        plan_key: {1: {'items': [
            {'case_id': 'case-1', 'question_type': 'precision', 'difficulty': 'easy', 'topic_id': 'current'},
        ]}},
        topic_key: {1: {'topics': [
            _topic('current'), _topic('topic-a'), _topic('topic-b'), _topic('topic-c'),
        ]}},
    })

    first = _topic_options(service, page_size=2)
    service.flow.values[topic_key][2] = {'topics': [
        _topic('current'), _topic('topic-0'), _topic('topic-a'), _topic('topic-b'), _topic('topic-c'),
    ]}

    continuation = _topic_options(service, page_size=2, page_token=first['next_page_token'])
    fresh = _topic_options(service, page_size=2)

    assert [row['topic_id'] for row in continuation['items']] == ['topic-c']
    assert [row['topic_id'] for row in fresh['items']] == ['topic-0', 'topic-a']


@pytest.mark.parametrize('case_id', ['missing', 'imported-case'])
def test_case_topic_options_returns_404_when_case_has_no_generated_plan_item(case_id: str) -> None:
    service = _service({
        ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN): {1: {'items': [
            {'case_id': 'case-1', 'question_type': 'precision', 'difficulty': 'easy', 'topic_id': 'current'},
        ]}},
        ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {1: {'topics': [_topic('current')]}},
    })

    with pytest.raises(ServiceError) as error:
        _topic_options(service, case_id)
    assert error.value.status_code == 404


def test_control_handlers_delegate_their_path_and_pagination_parameters(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    class _Projections:
        async def material_adjustment_options(self, thread_id: str) -> dict:
            calls.append(('materials', thread_id))
            return {'thread_id': thread_id}

        async def case_topic_options(self, thread_id: str, case_id: str, *, page_size: int | None,
                                     page_token: str) -> dict:
            calls.append(('topics', thread_id, case_id, page_size, page_token))
            return {'thread_id': thread_id, 'case_id': case_id}

    class _Service:
        projections = _Projections()

        async def close(self) -> None:
            return None

    async def _open(_: Path) -> _Service:
        return _Service()

    monkeypatch.setattr('evo.service.api.EvoService.open', _open)
    with TestClient(create_app(tmp_path)) as client:
        materials = client.get('/threads/thr-1/dataset/materials/adjustment-options')
        topics = client.get(
            '/threads/thr-1/dataset/cases/case-1/topic-options',
            params={'page_size': '20', 'page_token': 'p1.token'},
        )

    assert materials.status_code == 200
    assert topics.status_code == 200
    assert calls == [
        ('materials', 'thr-1'),
        ('topics', 'thr-1', 'case-1', 20, 'p1.token'),
    ]
