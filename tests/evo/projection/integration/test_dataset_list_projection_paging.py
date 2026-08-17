from __future__ import annotations

"""Behavior tests shared by Dataset ProjectionService list endpoints."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo import artifacts as A
from evo.artifact_runtime import ArtifactKey, ArtifactRecord, ArtifactRef, PartitionSet
from evo.service.api import create_app
from evo.service.contracts import ServiceError
from evo.service.projections import ProjectionService


class _FakeFlow:
    def __init__(self, values: dict[int, dict], *, has_thread: bool = True) -> None:
        self._values = values
        self._has_thread = has_thread

    async def has_run(self, thread_id: str) -> bool:
        return self._has_thread

    async def head(self, thread_id: str, key: ArtifactKey) -> ArtifactRecord | None:
        versions = tuple(self._values)
        if key != ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST) or not versions:
            return None
        return self._record(max(versions))

    async def record(self, thread_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        if ref.key != ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST) or ref.version not in self._values:
            return None
        return self._record(ref.version)

    async def read(self, thread_id: str, ref: ArtifactRef) -> dict:
        return self._values[ref.version]

    @staticmethod
    def _record(version: int) -> ArtifactRecord:
        return ArtifactRecord(
            ArtifactRef(ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST), version),
            producer='test',
        )


class _DocumentsFakeFlow:
    def __init__(self, values: dict[ArtifactKey, dict[int, dict]], *, has_thread: bool = True) -> None:
        self._values = values
        self._has_thread = has_thread

    async def has_run(self, thread_id: str) -> bool:
        return self._has_thread

    async def head(self, thread_id: str, key: ArtifactKey) -> ArtifactRecord | None:
        versions = self._values.get(key, {})
        return self._record(key, max(versions)) if versions else None

    async def record(self, thread_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        return self._record(ref.key, ref.version) if ref.version in self._values.get(ref.key, {}) else None

    async def read(self, thread_id: str, ref: ArtifactRef) -> dict:
        return self._values[ref.key][ref.version]

    @staticmethod
    def _record(key: ArtifactKey, version: int) -> ArtifactRecord:
        return ArtifactRecord(ArtifactRef(key, version), producer='test')


class _CaseListFakeFlow:
    def __init__(self, values: dict[ArtifactKey, dict[int, object]], cases: dict[str, object],
                 *, has_thread: bool = True) -> None:
        self._values = values
        self._cases = cases
        self._has_thread = has_thread

    async def has_run(self, _: str) -> bool:
        return self._has_thread

    async def head(self, _: str, key: ArtifactKey) -> ArtifactRecord | None:
        versions = self._values.get(key, {})
        return None if not versions else self._record(key, max(versions))

    async def record(self, _: str, ref: ArtifactRef) -> ArtifactRecord | None:
        return self._record(ref.key, ref.version) if ref.version in self._values.get(ref.key, {}) else None

    async def read(self, _: str, ref: ArtifactRef) -> object:
        return self._values[ref.key][ref.version]

    async def case_snapshot(self, _: str, case_id: str) -> object:
        return self._cases[case_id]

    @staticmethod
    def _record(key: ArtifactKey, version: int) -> ArtifactRecord:
        return ArtifactRecord(ArtifactRef(key, version), producer='test')


def _manifest(*topics: tuple[str, str, str, int]) -> dict:
    return {
        'topics': [
            {
                'topic_id': topic_id,
                'name': name,
                'question_type': question_type,
                'chunk_ids': [f'{topic_id}-chunk-{index}' for index in range(chunk_count)],
                'chunk_count': chunk_count,
            }
            for topic_id, name, question_type, chunk_count in topics
        ],
        'stats': {},
    }


def _service(*, versions: dict[int, dict], has_thread: bool = True) -> ProjectionService:
    return ProjectionService(_FakeFlow(versions, has_thread=has_thread), definition=None)


def _topics(service: ProjectionService, *, thread_id: str = 'thr-1', page_token: str = '', page_size: int = 2,
            question_type: str = '', min_chunk_count: int | None = None,
            max_chunk_count: int | None = None) -> dict:
    return asyncio.run(service.topics(
        thread_id,
        question_type=question_type,
        min_chunk_count=min_chunk_count,
        max_chunk_count=max_chunk_count,
        page_size=page_size,
        page_token=page_token,
    ))


def _documents_manifest(*rows: tuple[str, str, str, bool, int]) -> dict:
    return {'documents': [
        {
            'kb_id': kb_id,
            'knowledge_base_name': kb_name,
            'doc_id': doc_id,
            'filename': filename,
            'included': included,
            'discovery_index': discovery_index,
        }
        for discovery_index, (kb_id, kb_name, doc_id, filename, included) in enumerate(rows)
    ]}


def _candidate_manifest(*rows: tuple[str, str, str, bool]) -> dict:
    return {'chunks': [
        {'kb_id': kb_id, 'doc_id': doc_id, 'chunk_id': chunk_id, 'selected': selected}
        for kb_id, doc_id, chunk_id, selected in rows
    ]}


def _documents_service(*, selected_versions: dict[int, dict], candidate_versions: dict[int, dict] | None = None,
                       has_thread: bool = True) -> ProjectionService:
    values = {ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): selected_versions}
    if candidate_versions is not None:
        values[ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)] = candidate_versions
    return ProjectionService(_DocumentsFakeFlow(values, has_thread=has_thread), definition=None)


def _documents(service: ProjectionService, *, thread_id: str = 'thr-1', page_token: str = '', page_size: int = 2,
               included: bool | None = None, knowledge_base_id: str = '') -> dict:
    return asyncio.run(service.materials_documents(
        thread_id,
        included=included,
        knowledge_base_id=knowledge_base_id,
        page_size=page_size,
        page_token=page_token,
    ))


def _case_list_values(*, topic_b_name: str = '主题 B') -> dict[ArtifactKey, dict[int, object]]:
    return {
        ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST): {1: {
            'stats': {'case_allocation': {'assignments': {
                'case-1': {'mode': 'imported', 'source_row_number': 1},
                'case-2': {'mode': 'generated'},
                'case-3': {'mode': 'generated'},
            }}},
            'details': [{'source_row_number': 1, 'case': {
                'id': 'case-1', 'question_type': 'reasoning', 'difficulty': 'hard',
            }}],
        }},
        ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN): {1: {'items': [
            {'case_id': 'case-2', 'question_type': 'precision', 'difficulty': 'medium', 'topic_id': 'topic-b'},
            {'case_id': 'case-3', 'question_type': 'reasoning', 'difficulty': 'easy', 'topic_id': 'topic-a'},
        ]}},
        ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {1: {'topics': [
            {'topic_id': 'topic-a', 'name': '主题 A'},
            {'topic_id': 'topic-b', 'name': topic_b_name},
        ]}},
        ArtifactKey.scalar(A.EVAL_CASE_REQUESTS): {1: PartitionSet(('case-1', 'case-2', 'case-3'))},
    }


def _case_snapshots() -> dict[str, object]:
    return {
        'case-1': {'runtime': {'operations': [
            {'operation_id': 'dataset.qaplan_spec', 'status': 'succeeded'},
            {'operation_id': 'dataset.generate_case', 'status': 'succeeded'},
            {'operation_id': 'dataset.enhance_case', 'status': 'succeeded'},
        ]}},
        'case-2': {'runtime': {'operations': [
            {'operation_id': 'dataset.qaplan_spec', 'status': 'succeeded'},
            {'operation_id': 'dataset.generate_case', 'status': 'running'},
            {'operation_id': 'dataset.enhance_case', 'status': 'pending'},
        ]}},
        'case-3': {'runtime': {'operations': [
            {'operation_id': 'dataset.qaplan_spec', 'status': 'failed'},
            {'operation_id': 'dataset.generate_case', 'status': 'pending'},
            {'operation_id': 'dataset.enhance_case', 'status': 'pending'},
        ]}},
    }


def _case_list_service(*, values: dict[ArtifactKey, dict[int, object]] | None = None,
                       has_thread: bool = True) -> ProjectionService:
    return ProjectionService(
        _CaseListFakeFlow(_case_list_values() if values is None else values, _case_snapshots(), has_thread=has_thread),
        definition=None,
    )


def _cases(service: ProjectionService, *, thread_id: str = 'thr-1', page_size: int = 50,
           page_token: str = '', plan_status: str = '', generate_status: str = '', grading_status: str = '',
           source: str = '', question_type: str = '', difficulty: str = '') -> dict:
    return asyncio.run(service.cases(
        thread_id, page_size=page_size, page_token=page_token, plan_status=plan_status,
        generate_status=generate_status, grading_status=grading_status, source=source,
        question_type=question_type, difficulty=difficulty,
    ))


def test_documents_projects_dto_from_selected_docs_and_chunk_candidates() -> None:
    service = _documents_service(
        selected_versions={1: _documents_manifest(
            ('kb-a', '产品资料库', 'doc-1', '产品手册.pdf', True),
            ('kb-b', '研究资料库', 'doc-2', '调研报告.pdf', False),
        )},
        candidate_versions={4: _candidate_manifest(
            ('kb-a', 'doc-1', 'chunk-1', True),
            ('kb-a', 'doc-1', 'chunk-2', False),
        )},
    )

    result = _documents(service, page_size=50)

    assert set(result) == {'thread_id', 'revision', 'items', 'next_page_token'}
    assert result['items'] == [
        {
            'document_id': 'doc-1', 'name': '产品手册.pdf', 'included': True,
            'knowledge_base': {'id': 'kb-a', 'name': '产品资料库'},
            'chunks': {'effective': 2, 'selected': 1, 'selection_rate': 0.5},
        },
        {
            'document_id': 'doc-2', 'name': '调研报告.pdf', 'included': False,
            'knowledge_base': {'id': 'kb-b', 'name': '研究资料库'}, 'chunks': None,
        },
    ]


def test_documents_filters_with_and_semantics_and_rejects_invalid_filter() -> None:
    service = _documents_service(selected_versions={1: _documents_manifest(
        ('kb-a', 'A', 'doc-1', 'one.pdf', True),
        ('kb-a', 'A', 'doc-2', 'two.pdf', False),
        ('kb-b', 'B', 'doc-3', 'three.pdf', True),
    )})

    result = _documents(service, included=True, knowledge_base_id='kb-a')
    assert [item['document_id'] for item in result['items']] == ['doc-1']

    with pytest.raises(ServiceError) as error:
        _documents(service, knowledge_base_id=' ')
    assert error.value.status_code == 400


def test_documents_pagination_snapshot_includes_both_source_artifacts() -> None:
    service = _documents_service(
        selected_versions={1: _documents_manifest(
            ('kb-a', 'A', 'doc-1', 'one.pdf', True),
            ('kb-a', 'A', 'doc-2', 'two.pdf', True),
            ('kb-a', 'A', 'doc-3', 'three.pdf', True),
        )},
        candidate_versions={1: _candidate_manifest(('kb-a', 'doc-1', 'c-1', True))},
    )
    first = _documents(service)
    service.flow._values[ArtifactKey.scalar(A.DATASET_SELECTED_DOCS)][2] = _documents_manifest(
        ('kb-a', 'A', 'doc-0', 'zero.pdf', True),
        ('kb-a', 'A', 'doc-1', 'one.pdf', True),
        ('kb-a', 'A', 'doc-2', 'two.pdf', True),
        ('kb-a', 'A', 'doc-3', 'three.pdf', True),
    )

    continuation = _documents(service, page_token=first['next_page_token'])
    new_first = _documents(service)

    assert [item['document_id'] for item in continuation['items']] == ['doc-3']
    assert continuation['revision'] == first['revision']
    assert [item['document_id'] for item in new_first['items']] == ['doc-0', 'doc-1']

    with pytest.raises(ServiceError) as error:
        _documents(service, page_token=first['next_page_token'], included=True)
    assert error.value.status_code == 400


def test_documents_return_409_when_token_references_unreadable_candidate_snapshot() -> None:
    service = _documents_service(
        selected_versions={1: _documents_manifest(
            ('kb-a', 'A', 'doc-1', 'one.pdf', True),
            ('kb-a', 'A', 'doc-2', 'two.pdf', True),
            ('kb-a', 'A', 'doc-3', 'three.pdf', True),
        )},
        candidate_versions={1: _candidate_manifest(('kb-a', 'doc-1', 'c-1', True))},
    )
    first = _documents(service)
    service.flow._values[ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)] = {}

    with pytest.raises(ServiceError) as error:
        _documents(service, page_token=first['next_page_token'])
    assert error.value.status_code == 409


def test_documents_return_chunks_null_until_candidate_snapshot_exists() -> None:
    service = _documents_service(selected_versions={1: _documents_manifest(
        ('kb-a', 'A', 'doc-1', 'one.pdf', True),
    )})

    result = _documents(service)

    assert result['revision']
    assert result['items'][0]['chunks'] is None


def test_documents_return_unversioned_empty_list_before_document_selection_exists() -> None:
    service = _documents_service(selected_versions={})

    assert _documents(service) == {
        'thread_id': 'thr-1', 'revision': None, 'items': [], 'next_page_token': '',
    }


def test_topics_first_page_returns_revision_and_continuation() -> None:
    service = _service(versions={1: _manifest(
        ('topic-3', 'Gamma', 'precision', 3),
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-2', 'Beta', 'reasoning', 2),
    )})

    result = _topics(service, question_type='precision')

    assert result['thread_id'] == 'thr-1'
    assert result['revision']
    assert [item['topic_id'] for item in result['items']] == ['topic-1', 'topic-3']
    assert result['next_page_token'] == ''


def test_topics_projects_only_the_documented_topic_list_dto() -> None:
    manifest = _manifest(('topic-1', 'Alpha', 'precision', 2))
    manifest['topics'][0]['internal_cluster_id'] = 'cluster-1'
    manifest['topics'][0]['scores'] = {'confidence': 0.99}
    manifest['stats'] = {'total_topic_count': 1}
    service = _service(versions={3: manifest})

    result = _topics(service)

    assert set(result) == {'thread_id', 'revision', 'items', 'next_page_token'}
    assert result['items'] == [{
        'topic_id': 'topic-1',
        'name': 'Alpha',
        'question_type': 'precision',
        'chunk_count': 2,
    }]


def test_topics_applies_question_type_and_chunk_count_filters_with_and_semantics() -> None:
    service = _service(versions={1: _manifest(
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-2', 'Beta', 'precision', 2),
        ('topic-3', 'Gamma', 'precision', 4),
        ('topic-4', 'Delta', 'reasoning', 2),
    )})

    result = _topics(
        service,
        question_type='precision',
        min_chunk_count=2,
        max_chunk_count=3,
        page_size=50,
    )

    assert [item['topic_id'] for item in result['items']] == ['topic-2']


@pytest.mark.parametrize(('question_type', 'min_chunk_count', 'max_chunk_count'), [
    ('unsupported', None, None),
    ('precision', -1, None),
    ('precision', None, -1),
    ('precision', 3, 2),
])
def test_topics_rejects_invalid_filter_values(question_type: str, min_chunk_count: int | None,
                                              max_chunk_count: int | None) -> None:
    service = _service(versions={1: _manifest(('topic-1', 'Alpha', 'precision', 1))})

    with pytest.raises(ServiceError) as error:
        _topics(
            service,
            question_type=question_type,
            min_chunk_count=min_chunk_count,
            max_chunk_count=max_chunk_count,
        )

    assert error.value.status_code == 400


def test_topics_next_page_keeps_same_revision_without_duplicates_or_omissions() -> None:
    service = _service(versions={1: _manifest(
        ('topic-3', 'Gamma', 'precision', 3),
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-4', 'Delta', 'precision', 4),
        ('topic-2', 'Beta', 'precision', 2),
        ('topic-5', 'Epsilon', 'precision', 5),
    )})

    first = _topics(service)
    second = _topics(service, page_token=first['next_page_token'])
    third = _topics(service, page_token=second['next_page_token'])

    assert first['revision'] == second['revision'] == third['revision']
    assert [item['topic_id'] for item in first['items'] + second['items'] + third['items']] == [
        'topic-1', 'topic-2', 'topic-4', 'topic-5', 'topic-3',
    ]
    assert third['next_page_token'] == ''


@pytest.mark.parametrize(('changed', 'value'), [
    ('question_type', 'reasoning'),
    ('min_chunk_count', 2),
    ('page_size', 1),
])
def test_topics_rejects_continuation_when_query_behavior_changes(changed: str, value: str | int) -> None:
    service = _service(versions={1: _manifest(
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-2', 'Beta', 'reasoning', 2),
        ('topic-3', 'Gamma', 'precision', 3),
    )})
    first = _topics(service)
    arguments = {'page_token': first['next_page_token'], changed: value}

    with pytest.raises(ServiceError) as error:
        _topics(service, **arguments)

    assert error.value.status_code == 400


def test_topics_rejects_malformed_or_cross_thread_token() -> None:
    service = _service(versions={1: _manifest(
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-2', 'Beta', 'precision', 2),
        ('topic-3', 'Gamma', 'precision', 3),
    )})
    first = _topics(service)

    with pytest.raises(ServiceError) as malformed_error:
        _topics(service, page_token='not-a-token')
    assert malformed_error.value.status_code == 400

    with pytest.raises(ServiceError) as cross_thread_error:
        _topics(service, thread_id='thr-2', page_token=first['next_page_token'])
    assert cross_thread_error.value.status_code == 400


def test_topics_old_continuation_reads_its_original_revision_after_data_changes() -> None:
    service = _service(versions={1: _manifest(
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-2', 'Beta', 'precision', 2),
        ('topic-3', 'Gamma', 'precision', 3),
    )})

    # 首屏发生在 v1；随后 v2 提交。旧 token 续页仍必须读取 v1。
    first = _topics(service)
    service.flow._values[2] = _manifest(
        ('topic-0', 'Aardvark', 'precision', 1),
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-2', 'Beta', 'precision', 2),
        ('topic-3', 'Gamma', 'precision', 3),
    )

    old_next = _topics(service, page_token=first['next_page_token'])
    new_first = _topics(service)

    assert old_next['revision'] == first['revision']
    assert [item['topic_id'] for item in old_next['items']] == ['topic-3']
    assert new_first['revision'] != first['revision']
    assert [item['topic_id'] for item in new_first['items']] == ['topic-0', 'topic-1']


def test_topics_empty_result_keeps_the_current_revision() -> None:
    service = _service(versions={1: _manifest(('topic-1', 'Alpha', 'precision', 1))})

    result = _topics(service, question_type='reasoning')

    assert result['revision']
    assert result['items'] == []
    assert result['next_page_token'] == ''


def test_topics_returns_an_unversioned_empty_list_before_topic_manifest_exists() -> None:
    service = _service(versions={})

    result = _topics(service)

    assert result == {
        'thread_id': 'thr-1',
        'revision': None,
        'items': [],
        'next_page_token': '',
    }


def test_topics_returns_404_when_the_thread_does_not_exist() -> None:
    service = _service(versions={}, has_thread=False)

    with pytest.raises(ServiceError) as error:
        _topics(service)

    assert error.value.status_code == 404


def test_topics_returns_409_when_the_token_snapshot_cannot_be_read() -> None:
    service = _service(versions={1: _manifest(
        ('topic-1', 'Alpha', 'precision', 1),
        ('topic-2', 'Beta', 'precision', 2),
        ('topic-3', 'Gamma', 'precision', 3),
    )})
    first = _topics(service)
    service.flow._values = {}

    with pytest.raises(ServiceError) as error:
        _topics(service, page_token=first['next_page_token'])

    assert error.value.status_code == 409


def test_cases_projects_plan_metadata_and_runtime_operation_statuses() -> None:
    result = _cases(_case_list_service())

    assert [item['case_id'] for item in result['items']] == ['case-1', 'case-2', 'case-3']
    assert result['items'] == [
        {
            'case_id': 'case-1',
            'stages': {'plan': 'succeeded', 'generate': 'succeeded', 'grading': 'succeeded'},
            'source': 'imported', 'question_type': 'reasoning', 'difficulty': 'hard', 'topic': None,
        },
        {
            'case_id': 'case-2',
            'stages': {'plan': 'succeeded', 'generate': 'running', 'grading': 'pending'},
            'source': 'generated', 'question_type': 'precision', 'difficulty': 'medium',
            'topic': {'topic_id': 'topic-b', 'name': '主题 B'},
        },
        {
            'case_id': 'case-3',
            'stages': {'plan': 'failed', 'generate': 'pending', 'grading': 'pending'},
            'source': 'generated', 'question_type': 'reasoning', 'difficulty': 'easy',
            'topic': {'topic_id': 'topic-a', 'name': '主题 A'},
        },
    ]


def test_cases_combines_all_status_and_business_filters_with_and_semantics() -> None:
    service = _case_list_service()

    result = _cases(
        service,
        plan_status='succeeded', generate_status='running', grading_status='pending',
        source='generated', question_type='precision', difficulty='medium',
    )

    assert [item['case_id'] for item in result['items']] == ['case-2']


def test_cases_pagination_keeps_its_artifact_snapshot_and_binds_query_behavior() -> None:
    service = _case_list_service()
    first = _cases(service, page_size=1)

    # 首屏取得 v1；随后 Topic 名称变更为 v2。旧 token 仍读取 v1。
    service.flow._values[ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)][2] = {
        'topics': [{'topic_id': 'topic-a', 'name': '主题 A'}, {'topic_id': 'topic-b', 'name': '新主题 B'}],
    }
    old_next = _cases(service, page_size=1, page_token=first['next_page_token'])
    new_first = _cases(service, page_size=1)

    assert first['revision'] == old_next['revision']
    assert old_next['items'][0]['topic'] == {'topic_id': 'topic-b', 'name': '主题 B'}
    assert new_first['revision'] != first['revision']

    with pytest.raises(ServiceError) as error:
        _cases(service, page_size=1, page_token=first['next_page_token'], source='generated')
    assert error.value.status_code == 400


@pytest.mark.parametrize(('kwargs', 'message'), [
    ({'plan_status': 'completed'}, 'plan_status'),
    ({'source': 'manual'}, 'source'),
    ({'question_type': 'factual'}, 'question_type'),
    ({'difficulty': 'very-hard'}, 'difficulty'),
])
def test_cases_rejects_invalid_filters(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ServiceError) as error:
        _cases(_case_list_service(), **kwargs)
    assert error.value.status_code == 400
    assert message in str(error.value)


def test_topics_handler_delegates_the_documented_query_parameters(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    class _Projections:
        async def topics(self, thread_id: str, **kwargs: object) -> dict:
            calls.append((thread_id, kwargs))
            return {'thread_id': thread_id, 'revision': 'r1', 'items': [], 'next_page_token': ''}

    class _Service:
        projections = _Projections()

        async def close(self) -> None:
            return None

    async def _open(_: Path) -> _Service:
        return _Service()

    monkeypatch.setattr('evo.service.api.EvoService.open', _open)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(
            '/threads/thr-1/dataset/topics',
            params={
                'question_type': 'precision',
                'min_chunk_count': '2',
                'max_chunk_count': '5',
                'page_size': '20',
                'page_token': 'page-2',
            },
        )
        invalid_response = client.get('/threads/thr-1/dataset/topics', params={'page_size': 'not-an-integer'})

    assert response.status_code == 200
    assert response.json()['revision'] == 'r1'
    assert invalid_response.status_code == 400
    assert calls == [('thr-1', {
        'question_type': 'precision',
        'min_chunk_count': 2,
        'max_chunk_count': 5,
        'page_size': 20,
        'page_token': 'page-2',
    })]


def test_cases_handler_delegates_the_documented_query_parameters(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    class _Projections:
        async def cases(self, thread_id: str, **kwargs: object) -> dict:
            calls.append((thread_id, kwargs))
            return {'thread_id': thread_id, 'revision': 'r1', 'items': [], 'next_page_token': ''}

    class _Service:
        projections = _Projections()

        async def close(self) -> None:
            return None

    async def _open(_: Path) -> _Service:
        return _Service()

    monkeypatch.setattr('evo.service.api.EvoService.open', _open)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(
            '/threads/thr-1/dataset/cases',
            params={
                'plan_status': 'succeeded', 'generate_status': 'running', 'grading_status': 'pending',
                'source': 'generated', 'question_type': 'precision', 'difficulty': 'medium',
                'page_size': '20', 'page_token': 'page-2',
            },
        )
        invalid_response = client.get('/threads/thr-1/dataset/cases', params={'page_size': 'not-an-integer'})

    assert response.status_code == 200
    assert invalid_response.status_code == 400
    assert calls == [('thr-1', {
        'plan_status': 'succeeded', 'generate_status': 'running', 'grading_status': 'pending',
        'source': 'generated', 'question_type': 'precision', 'difficulty': 'medium',
        'page_size': 20, 'page_token': 'page-2',
    })]


def test_documents_handler_delegates_documented_query_parameters(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []

    class _Projections:
        async def materials_documents(self, thread_id: str, **kwargs: object) -> dict:
            calls.append((thread_id, kwargs))
            return {'thread_id': thread_id, 'revision': 'r1', 'items': [], 'next_page_token': ''}

    class _Service:
        projections = _Projections()

        async def close(self) -> None:
            return None

    async def _open(_: Path) -> _Service:
        return _Service()

    monkeypatch.setattr('evo.service.api.EvoService.open', _open)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(
            '/threads/thr-1/dataset/materials/documents',
            params={
                'included': 'true', 'knowledge_base_id': 'kb-1', 'page_size': '20', 'page_token': 'page-2',
            },
        )
        invalid_response = client.get(
            '/threads/thr-1/dataset/materials/documents', params={'included': 'sometimes'},
        )

    assert response.status_code == 200
    assert invalid_response.status_code == 400
    assert calls == [('thr-1', {
        'included': True,
        'knowledge_base_id': 'kb-1',
        'page_size': 20,
        'page_token': 'page-2',
    })]
