from __future__ import annotations

import asyncio
import pytest

from evo import artifacts as A
from evo.artifact_runtime import ArtifactCommit, ArtifactKey, ArtifactRecord, ArtifactRef
from evo.service.contracts import ServiceError
from evo.service.core import EvoService
from evo.service.projections import ProjectionService


class _ApplyFlow:
    def __init__(self, values: dict[ArtifactKey, tuple[int, object]]) -> None:
        self.values = values
        self.commits: list[ArtifactCommit] = []

    async def has_run(self, _thread_id: str) -> bool:
        return True

    async def head(self, _thread_id: str, key: ArtifactKey) -> ArtifactRecord | None:
        value = self.values.get(key)
        return None if value is None else ArtifactRecord(ArtifactRef(key, value[0]), 'test')

    async def read(self, _thread_id: str, ref: ArtifactRef) -> object:
        version, value = self.values[ref.key]
        assert ref.version == version
        return value

    async def commit(self, _thread_id: str, commit: ArtifactCommit) -> object:
        self.commits.append(commit)
        return object()


def _service(values: dict[ArtifactKey, tuple[int, object]]) -> tuple[EvoService, _ApplyFlow]:
    flow = _ApplyFlow(values)
    service = object.__new__(EvoService)
    service.flow = flow

    async def _continue(_thread_id: str) -> None:
        return None

    service._continue_automatic = _continue  # type: ignore[method-assign]
    return service, flow


class _CapabilityClient:
    def __init__(self, values: dict[str, dict]) -> None:
        self.values = values

    def parser_capabilities(self, kb_ids: list[str]) -> dict[str, dict]:
        return {kb_id: self.values[kb_id] for kb_id in kb_ids}


def _revision(*refs: ArtifactRef) -> str:
    return ProjectionService._build_revision(tuple(refs))


def _material_values() -> dict[ArtifactKey, tuple[int, object]]:
    return {
        ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG): (3, {
            'kb_id': ['kb-a'], 'knowledge_base_names': {'kb-a': 'A'},
            'csv_data': [], 'target_case_count': 3,
        }),
        ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS): (4, {
            'knowledge_bases': [{'kb_id': 'kb-a', 'included': True}],
            'excluded_docs': [],
        }),
        ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS): (5, {
            'groups': ['block'], 'allowed_types': ['text'],
        }),
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): (6, {'documents': [
            {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'included': True},
        ]}),
        ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES): (7, {
            'chunks': [
                {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': True, 'group': 'block'},
                {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-2', 'selected': False, 'group': 'block'},
            ],
            'quotas': [{'kb_id': 'kb-a', 'doc_id': 'doc-1', 'group': 'block', 'required': 1}],
        }),
    }


def test_apply_material_scan_config_commits_complete_changed_values_with_three_way_cas() -> None:
    service, flow = _service(_material_values())
    source = ArtifactRef(ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG), 3)
    selection = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS), 4)
    chunks = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS), 5)

    asyncio.run(service.apply_material_scan_config('thr-1', {
        'request_id': 'scan-1',
        'expected_revision': _revision(source, selection, chunks),
        'changes': {
            'target_case_count': 5,
            'documents': [{'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'included': False}],
            'split_rule_ids': ['block', 'line'],
        },
    }))

    assert len(flow.commits) == 1
    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-materials-scan:scan-1'
    assert commit.expected_heads == {ref.key: ref for ref in (source, selection, chunks)}
    values = {write.key.artifact_id: write.value for write in commit.writes}
    assert values[A.CORPUS_SOURCE_CONFIG]['target_case_count'] == 5
    assert values[A.DATASET_SELECT_DOCS_PARAMS]['excluded_docs'] == [{'kb_id': 'kb-a', 'doc_id': 'doc-1'}]
    assert values[A.DATASET_BUILD_CHUNKS_PARAMS]['groups'] == ['block', 'line']


def test_material_apply_rejects_mixing_scan_configuration_and_chunk_selection() -> None:
    service, _ = _service(_material_values())

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.apply_material_scan_config('thr-1', {
            'request_id': 'mixed-1', 'expected_revision': 'ignored',
            'changes': {'target_case_count': 5, 'chunk_selection_changes': []},
        }))
    assert error.value.status_code == 400


def test_material_apply_rejects_enabling_a_capability_not_supported_by_current_sources() -> None:
    service, _ = _service(_material_values())
    service.capability_client = _CapabilityClient({
        'kb-a': {
            'split_rules': [{'id': 'block', 'name': '段落'}],
            'layout_types': [{'id': 'text', 'name': '文本'}],
        },
    })
    source = ArtifactRef(ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG), 3)
    selection = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS), 4)
    chunks = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS), 5)

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.apply_material_scan_config('thr-1', {
            'request_id': 'unsupported-capability',
            'expected_revision': _revision(source, selection, chunks),
            'changes': {'split_rule_ids': ['line']},
        }))

    assert error.value.status_code == 422


def test_apply_material_chunk_selection_preserves_quota_and_uses_document_snapshot_cas() -> None:
    service, flow = _service(_material_values())
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)
    candidates = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES), 7)

    asyncio.run(service.apply_material_chunk_selection('thr-1', {
        'request_id': 'selection-1',
        'expected_revision': _revision(docs, candidates),
        'changes': {'chunk_selection_changes': [
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': False},
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-2', 'selected': True},
        ]},
    }))

    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-materials-selection:selection-1'
    assert commit.expected_heads == {docs.key: docs, candidates.key: candidates}
    assert [row['selected'] for row in commit.writes[0].value['chunks']] == [False, True]


def test_apply_topic_names_changes_only_names_and_keeps_topic_discovery_out_of_the_commit() -> None:
    topic_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
    topic_ref = ArtifactRef(topic_key, 8)
    service, flow = _service({topic_key: (8, {'topics': [
        {'topic_id': 'topic-1', 'name': 'old', 'question_type': 'precision', 'chunk_ids': ['c-1'], 'chunk_count': 1},
        {'topic_id': 'topic-2', 'name': 'unchanged', 'question_type': 'reasoning', 'chunk_ids': ['c-2'], 'chunk_count': 1},
    ]})})

    asyncio.run(service.apply_topic_names('thr-1', {
        'request_id': 'topic-1', 'expected_revision': _revision(topic_ref),
        'changes': [{'topic_id': 'topic-1', 'name': 'new'}],
    }))

    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-topic-names:topic-1'
    assert commit.expected_heads == {topic_key: topic_ref}
    assert commit.writes[0].key == topic_key
    assert commit.writes[0].value['topics'] == [
        {'topic_id': 'topic-1', 'name': 'new', 'question_type': 'precision', 'chunk_ids': ['c-1'], 'chunk_count': 1},
        {'topic_id': 'topic-2', 'name': 'unchanged', 'question_type': 'reasoning', 'chunk_ids': ['c-2'], 'chunk_count': 1},
    ]


def test_apply_services_return_404_for_missing_document_chunk_or_topic() -> None:
    service, _ = _service(_material_values())
    source = ArtifactRef(ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG), 3)
    selection = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS), 4)
    params = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS), 5)
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)
    candidates = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES), 7)

    with pytest.raises(ServiceError) as document_error:
        asyncio.run(service.apply_material_scan_config('thr-1', {
            'request_id': 'missing-document', 'expected_revision': _revision(source, selection, params),
            'changes': {'documents': [{'knowledge_base_id': 'kb-a', 'document_id': 'missing', 'included': False}]},
        }))
    assert document_error.value.status_code == 404

    with pytest.raises(ServiceError) as chunk_error:
        asyncio.run(service.apply_material_chunk_selection('thr-1', {
            'request_id': 'missing-chunk', 'expected_revision': _revision(docs, candidates),
            'changes': {'chunk_selection_changes': [
                {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'missing', 'selected': False},
            ]},
        }))
    assert chunk_error.value.status_code == 404

    topic_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
    topic_ref = ArtifactRef(topic_key, 1)
    topic_service, _ = _service({topic_key: (1, {'topics': []})})
    with pytest.raises(ServiceError) as topic_error:
        asyncio.run(topic_service.apply_topic_names('thr-1', {
            'request_id': 'missing-topic', 'expected_revision': _revision(topic_ref),
            'changes': [{'topic_id': 'missing', 'name': 'new'}],
        }))
    assert topic_error.value.status_code == 404
