from __future__ import annotations

"""Behavior tests for Dataset document and topic detail projections."""

import asyncio

import pytest

from evo import artifacts as A
from evo.artifact_runtime import ArtifactKey, ArtifactRecord, ArtifactRef
from evo.service.contracts import ServiceError
from evo.service.projections import ProjectionService


class _DetailFlow:
    def __init__(self, values: dict[ArtifactKey, dict[int, object]], *, has_thread: bool = True) -> None:
        self._values = values
        self._has_thread = has_thread

    async def has_run(self, thread_id: str) -> bool:
        return self._has_thread

    async def head(self, thread_id: str, key: ArtifactKey) -> ArtifactRecord | None:
        versions = self._values.get(key, {})
        return self._record(key, max(versions)) if versions else None

    async def record(self, thread_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        return self._record(ref.key, ref.version) if ref.version in self._values.get(ref.key, {}) else None

    async def read(self, thread_id: str, ref: ArtifactRef) -> object:
        return self._values[ref.key][ref.version]

    @staticmethod
    def _record(key: ArtifactKey, version: int) -> ArtifactRecord:
        return ArtifactRecord(ArtifactRef(key, version), producer='test')


def _service(values: dict[ArtifactKey, dict[int, object]]) -> ProjectionService:
    return ProjectionService(_DetailFlow(values), definition=None)


def _selected_docs(*documents: tuple[str, str, str, str, bool]) -> dict:
    return {'documents': [
        {
            'kb_id': kb_id,
            'knowledge_base_name': kb_name,
            'doc_id': doc_id,
            'filename': filename,
            'included': included,
        }
        for kb_id, kb_name, doc_id, filename, included in documents
    ]}


def _candidate(*, chunk_id: str, selected: bool, split_rule: str, discovery_index: int,
               text: str = '正文', layout_type: str = 'paragraph') -> dict:
    return {
        'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': chunk_id,
        'selected': selected, 'group': split_rule, 'type': layout_type,
        'text': text, 'discovery_index': discovery_index,
    }


def _document_detail(service: ProjectionService, *, page_token: str = '', page_size: int = 50,
                     selected: bool | None = None, split_rule: str = '') -> dict:
    return asyncio.run(service.material_document_detail(
        'thr-1', 'kb-a', 'doc-1', selected=selected, split_rule=split_rule,
        page_size=page_size, page_token=page_token,
    ))


def _topic_detail(service: ProjectionService, *, page_token: str = '', page_size: int = 50) -> dict:
    return asyncio.run(service.topic_detail(
        'thr-1', 'topic-1', page_size=page_size, page_token=page_token,
    ))


def test_document_detail_projects_document_quotas_and_filtered_chunk_page() -> None:
    service = _service({
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): {1: _selected_docs(
            ('kb-a', '产品知识库', 'doc-1', '产品手册.pdf', True),
        )},
        ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES): {3: {
            'chunks': [
                _candidate(chunk_id='chunk-1', selected=True, split_rule='block', discovery_index=0),
                _candidate(chunk_id='chunk-2', selected=False, split_rule='block', discovery_index=1),
                _candidate(chunk_id='chunk-3', selected=True, split_rule='line', discovery_index=2),
            ],
            'quotas': [
                {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'group': 'block', 'required': 1},
                {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'group': 'line', 'required': 1},
            ],
        }},
    })

    result = _document_detail(service, selected=True, split_rule='block')

    assert set(result) == {'thread_id', 'revision', 'document', 'chunk_summary', 'quotas', 'chunks'}
    assert result['document'] == {
        'id': 'doc-1', 'name': '产品手册.pdf', 'included': True,
        'knowledge_base': {'id': 'kb-a', 'name': '产品知识库'},
    }
    assert result['chunk_summary'] == {'effective': 3, 'selected': 2}
    assert result['quotas'] == [
        {'split_rule': 'block', 'required': 1, 'selected': 1},
        {'split_rule': 'line', 'required': 1, 'selected': 1},
    ]
    assert result['chunks'] == {
        'items': [{
            'chunk_id': 'chunk-1', 'split_rule': 'block', 'layout_type': 'paragraph',
            'text': '正文', 'selected': True,
        }],
        'next_page_token': '',
    }


def test_document_detail_paginates_a_fixed_selected_docs_and_candidate_snapshot() -> None:
    selected_key = ArtifactKey.scalar(A.DATASET_SELECTED_DOCS)
    candidates_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)
    service = _service({
        selected_key: {1: _selected_docs(('kb-a', 'A', 'doc-1', 'one.pdf', True))},
        candidates_key: {1: {'chunks': [
            _candidate(chunk_id='chunk-1', selected=True, split_rule='block', discovery_index=0),
            _candidate(chunk_id='chunk-2', selected=True, split_rule='block', discovery_index=1),
            _candidate(chunk_id='chunk-3', selected=True, split_rule='block', discovery_index=2),
        ], 'quotas': []}},
    })

    first = _document_detail(service, page_size=2)
    service.flow._values[candidates_key][2] = {'chunks': [
        _candidate(chunk_id='chunk-0', selected=True, split_rule='block', discovery_index=0),
        _candidate(chunk_id='chunk-1', selected=True, split_rule='block', discovery_index=1),
        _candidate(chunk_id='chunk-2', selected=True, split_rule='block', discovery_index=2),
        _candidate(chunk_id='chunk-3', selected=True, split_rule='block', discovery_index=3),
    ], 'quotas': []}

    continuation = _document_detail(service, page_size=2, page_token=first['chunks']['next_page_token'])
    fresh = _document_detail(service, page_size=2)

    assert [row['chunk_id'] for row in continuation['chunks']['items']] == ['chunk-3']
    assert continuation['revision'] == first['revision']
    assert [row['chunk_id'] for row in fresh['chunks']['items']] == ['chunk-0', 'chunk-1']

    with pytest.raises(ServiceError, match='page_token') as error:
        _document_detail(service, page_size=2, page_token=first['chunks']['next_page_token'], selected=True)
    assert error.value.status_code == 400


def test_document_detail_keeps_document_visible_before_candidate_artifact_exists() -> None:
    service = _service({
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): {1: _selected_docs(
            ('kb-a', 'A', 'doc-1', 'one.pdf', True),
        )},
    })

    result = _document_detail(service)

    assert result['revision']
    assert result['chunk_summary'] is None
    assert result['quotas'] == []
    assert result['chunks'] == {'items': [], 'next_page_token': ''}


@pytest.mark.parametrize(('kb_id', 'doc_id'), [('kb-b', 'doc-1'), ('kb-a', 'missing')])
def test_document_detail_returns_404_when_path_does_not_identify_a_selected_document(kb_id: str, doc_id: str) -> None:
    service = _service({
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): {1: _selected_docs(('kb-a', 'A', 'doc-1', 'one.pdf', True))},
    })

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.material_document_detail('thr-1', kb_id, doc_id, page_size=50))
    assert error.value.status_code == 404


def test_topic_detail_reads_chunk_ids_as_direct_partitions_and_keeps_topic_order() -> None:
    service = _service({
        ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {4: {'topics': [{
            'topic_id': 'topic-1', 'name': '补能方式', 'question_type': 'precision',
            'chunk_ids': ['chunk-b', 'chunk-a'], 'chunk_count': 2,
        }]}},
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): {2: _selected_docs(
            ('kb-a', '产品知识库', 'doc-1', '产品手册.pdf', True),
        )},
        ArtifactKey(A.DATASET_CHUNK, 'chunk-a'): {7: {
            'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-a', 'filename': 'ignored.pdf',
            'group': 'block', 'type': 'paragraph', 'text': 'A',
        }},
        ArtifactKey(A.DATASET_CHUNK, 'chunk-b'): {8: {
            'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-b', 'filename': 'ignored.pdf',
            'group': 'line', 'type': 'table', 'text': 'B',
        }},
    })

    result = _topic_detail(service)

    assert result['topic'] == {
        'topic_id': 'topic-1', 'name': '补能方式', 'question_type': 'precision', 'chunk_count': 2,
    }
    assert result['chunks'] == {'items': [
        {
            'chunk_id': 'chunk-b', 'knowledge_base': {'id': 'kb-a', 'name': '产品知识库'},
            'document': {'id': 'doc-1', 'name': '产品手册.pdf'},
            'split_rule': 'line', 'layout_type': 'table', 'text': 'B',
        },
        {
            'chunk_id': 'chunk-a', 'knowledge_base': {'id': 'kb-a', 'name': '产品知识库'},
            'document': {'id': 'doc-1', 'name': '产品手册.pdf'},
            'split_rule': 'block', 'layout_type': 'paragraph', 'text': 'A',
        },
    ], 'next_page_token': ''}


def test_topic_detail_continuation_keeps_manifest_documents_and_chunk_versions() -> None:
    manifest_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
    docs_key = ArtifactKey.scalar(A.DATASET_SELECTED_DOCS)
    chunk_a_key = ArtifactKey(A.DATASET_CHUNK, 'chunk-a')
    chunk_b_key = ArtifactKey(A.DATASET_CHUNK, 'chunk-b')
    chunk_c_key = ArtifactKey(A.DATASET_CHUNK, 'chunk-c')
    service = _service({
        manifest_key: {1: {'topics': [{
            'topic_id': 'topic-1', 'name': 'Topic', 'question_type': 'precision',
            'chunk_ids': ['chunk-a', 'chunk-b', 'chunk-c'], 'chunk_count': 3,
        }]}},
        docs_key: {1: _selected_docs(('kb-a', 'A', 'doc-1', 'one.pdf', True))},
        chunk_a_key: {1: {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-a', 'group': 'block', 'type': 'text', 'text': 'A'}},
        chunk_b_key: {1: {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-b', 'group': 'block', 'type': 'text', 'text': 'B'}},
        chunk_c_key: {1: {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-c', 'group': 'block', 'type': 'text', 'text': 'C'}},
    })

    first = _topic_detail(service, page_size=2)
    service.flow._values[manifest_key][2] = {'topics': [{
        'topic_id': 'topic-1', 'name': 'New', 'question_type': 'precision',
        'chunk_ids': ['chunk-c', 'chunk-b', 'chunk-a'], 'chunk_count': 3,
    }]}
    service.flow._values[chunk_c_key][2] = {
        'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-c', 'group': 'block', 'type': 'text', 'text': 'new C',
    }

    continuation = _topic_detail(service, page_size=2, page_token=first['chunks']['next_page_token'])
    fresh = _topic_detail(service, page_size=2)

    assert continuation['revision'] == first['revision']
    assert [row['chunk_id'] for row in continuation['chunks']['items']] == ['chunk-c']
    assert continuation['chunks']['items'][0]['text'] == 'C'
    assert fresh['topic']['name'] == 'New'
    assert [row['chunk_id'] for row in fresh['chunks']['items']] == ['chunk-c', 'chunk-b']


def test_topic_detail_returns_404_when_topic_or_referenced_chunk_is_missing() -> None:
    service = _service({
        ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {1: {'topics': [{
            'topic_id': 'topic-1', 'name': 'Topic', 'question_type': 'precision',
            'chunk_ids': ['missing-chunk'], 'chunk_count': 1,
        }]}},
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): {1: _selected_docs(('kb-a', 'A', 'doc-1', 'one.pdf', True))},
    })

    with pytest.raises(ServiceError) as chunk_error:
        _topic_detail(service)
    assert chunk_error.value.status_code == 404

    with pytest.raises(ServiceError) as topic_error:
        asyncio.run(service.topic_detail('thr-1', 'missing-topic', page_size=50))
    assert topic_error.value.status_code == 404
