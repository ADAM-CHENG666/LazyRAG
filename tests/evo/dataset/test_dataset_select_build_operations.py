from types import SimpleNamespace

import pytest

from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef
from evo.operations.dataset import (
    BuildChunksParams,
    build_chunk_candidates,
    build_chunks,
    build_chunks_manifest,
    select_docs,
)
import evo.operations.dataset.chunks_build as chunks_build_module


class FakeContext:
    def __init__(self, artifact_graph, *, params=None, input_refs=None, operation_run_id='op_1'):
        self.artifact_graph = artifact_graph
        self.params = params or {}
        self.input_refs = input_refs or []
        self.operation_run_id = operation_run_id
        self.progress = []
        self.call_recorder = FakeCallRecorder()

    def check_interrupt(self):
        return None

    def report_progress(self, **payload):
        self.progress.append(payload)


class FakeCallRecorder:
    def __init__(self):
        self.records = []

    def succeeded(self, idempotency_key, *, idempotency_scope='operation'):
        return None

    def record(self, adapter_type, request, response=None, *, phase='', item_ref='', status='succeeded',
               idempotency_key='', idempotency_scope='operation', error=None):
        record = SimpleNamespace(
            operation_run_id='op_1',
            adapter_type=adapter_type,
            request=request,
            response=response,
            phase=phase,
            item_ref=item_ref,
            status=status,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
            error=error,
            call_id=f'call_{len(self.records) + 1}',
            record_ref='',
        )
        self.records.append(record)
        return record


class FakeKnowledgeBaseClient:
    def __init__(self, documents=None, chunks=None):
        self.documents = documents or []
        self.chunks = chunks or {}
        self.list_calls = []
        self.chunk_calls = []

    def list_documents(self, kb_id):
        self.list_calls.append(kb_id)
        return list(self.documents)

    def iter_chunks(self, kb_id, doc_ids, groups, page_size):
        for doc_id in doc_ids:
            for group in groups:
                self.chunk_calls.append({'kb_id': kb_id, 'doc_id': doc_id, 'group': group, 'page_size': page_size})
                for batch in self.chunks.get((doc_id, group), []):
                    yield batch


def node(uid, text='chunk text', group='block', embedding=None, metadata=None, global_metadata=None):
    return SimpleNamespace(
        uid=uid,
        text=text,
        embedding=embedding if embedding is not None else {'default': [1.0, 2.0]},
        group=group,
        metadata=metadata if metadata is not None else {'type': 'text', 'page': 1},
        global_metadata=global_metadata if global_metadata is not None else {'filename': 'fallback.pdf'},
    )


def chunk_ctx(partition):
    return SimpleNamespace(output_key_by_name={'chunk': ArtifactKey('dataset.chunk', partition)})


def candidate_payload(selected, params, client):
    fallback_kb_id = selected.get('kb_id', '')
    selected = {
        **selected,
        'kb_ids': selected.get('kb_ids', [fallback_kb_id]),
        'docs': [{**doc, 'kb_id': doc.get('kb_id', fallback_kb_id)} for doc in selected.get('docs', [])],
    }
    target_case_count = selected.get('params', {}).get('target_case_count', 100)
    return build_chunk_candidates(
        None,
        {
            'selected_docs': selected,
            'build_chunks_params': params,
            'import_cases_manifest': {'stats': {'case_allocation': {
                'target_case_count': target_case_count,
                'import_case_count': 0,
                'auto_case_count': target_case_count,
                'assignments': {},
            }}}},
        client,
    )['build_chunk_candidates']


def manifest_ctx(partitions):
    refs = {ArtifactKey.of('dataset.selected_docs'): ArtifactRef(ArtifactKey.of('dataset.selected_docs'), 1)}
    refs.update({
        ArtifactKey('dataset.chunk', partition): ArtifactRef(ArtifactKey('dataset.chunk', partition), index)
        for index, partition in enumerate(partitions, start=1)
    })
    return SimpleNamespace(input_ref_by_key=refs)


DEFAULT_ALLOWED_TYPES = ['text', 'paragraph', 'table', 'formula', 'equation', 'unknown']


def import_manifest(*, target=2, imported=0):
    return {'stats': {'case_allocation': {
        'target_case_count': target,
        'import_case_count': imported,
        'auto_case_count': target - imported,
        'assignments': {},
    }}}


def test_select_docs_materializer_outputs_selected_docs():
    client = FakeKnowledgeBaseClient([
        {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'upload_status': 'success',
         'group_counts': {'block': 8, 'line': 2}},
        {'doc_id': 'doc-2', 'filename': 'b.docx', 'file_type': 'docx', 'upload_status': 'success',
         'group_counts': {'block': 0, 'line': 0}},
        {'doc_id': 'doc-3', 'filename': 'c.txt', 'file_type': 'txt', 'upload_status': 'pending'},
    ])

    output = select_docs(None, {'source_config': {'kb_ids': ['kb-1'], 'max_docs': 2},
                                'import_cases_manifest': import_manifest(target=37)}, client)

    assert output == {'selected_docs': {
        'kb_ids': ['kb-1'],
        'docs': [
            {'kb_id': 'kb-1', 'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 8, 'line': 2}},
            {'kb_id': 'kb-1', 'doc_id': 'doc-2', 'filename': 'b.docx', 'file_type': 'docx', 'status': 'success',
             'group_counts': {'block': 0, 'line': 0}},
        ],
        'stats': {'matched_by_kb': {'kb-1': 3}, 'selected_by_kb': {'kb-1': 2}, 'matched': 3, 'selected': 2},
        'params': {'kb_ids': ['kb-1'], 'max_docs': 2, 'auto_case_count': 37},
    }}
    assert client.list_calls == ['kb-1']


def test_select_docs_materializer_rejects_empty_selection():
    with pytest.raises(ValueError, match='selected no documents'):
        select_docs(None, {'source_config': {'kb_ids': ['kb-1']}, 'import_cases_manifest': import_manifest()}, FakeKnowledgeBaseClient([]))


def test_select_docs_materializer_defaults_target_case_count():
    client = FakeKnowledgeBaseClient([
        {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'upload_status': 'success',
         'group_counts': {'block': 8}},
    ])

    output = select_docs(None, {'source_config': {'kb_ids': ['kb-1']}, 'import_cases_manifest': import_manifest()}, client)

    assert output['selected_docs']['params'] == {'kb_ids': ['kb-1'], 'max_docs': 100, 'auto_case_count': 2}


def test_select_docs_allocates_a_shared_limit_proportionally_across_knowledge_bases():
    class MultiKbClient:
        def __init__(self):
            self.list_calls = []
            self.docs = {
                'kb-a': [{'doc_id': 'a-1'}],
                'kb-b': [{'doc_id': 'b-1'}, {'doc_id': 'b-2'}, {'doc_id': 'b-3'}],
            }

        def list_documents(self, kb_id):
            self.list_calls.append(kb_id)
            return self.docs[kb_id]

    client = MultiKbClient()
    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a', 'kb-b'], 'max_docs': 3},
        'import_cases_manifest': import_manifest(target=3),
    }, client)['selected_docs']

    assert [item['doc_id'] for item in output['docs']] == ['a-1', 'b-1', 'b-2']
    assert output['stats']['selected_by_kb'] == {'kb-a': 1, 'kb-b': 2}
    assert client.list_calls == ['kb-a', 'kb-b']


def test_select_docs_skips_knowledge_base_reads_when_every_case_is_imported():
    class NoReadClient:
        def list_documents(self, kb_id):
            raise AssertionError('all-imported runs must not read knowledge-base documents')

    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a', 'kb-b']},
        'import_cases_manifest': import_manifest(target=2, imported=2),
    }, NoReadClient())

    assert output['selected_docs']['docs'] == []
    assert output['selected_docs']['params']['auto_case_count'] == 0


@pytest.mark.parametrize('params, match', [
    ({'kb_ids': []}, 'kb_ids'),
    ({'kb_ids': ['kb-1'], 'max_docs': 0}, 'max_docs must be a positive integer'),
])
def test_select_docs_materializer_rejects_invalid_params(params, match):
    with pytest.raises(ValueError, match=match):
        select_docs(None, {'source_config': params, 'import_cases_manifest': import_manifest()}, FakeKnowledgeBaseClient([{'doc_id': 'doc-1'}]))


def test_select_docs_materializer_normalizes_optional_doc_fields():
    client = FakeKnowledgeBaseClient([
        {'doc_id': 'doc-1', 'display_name': 'fallback-name.pdf', 'upload_status': 'ready',
         'group_counts': {'block': '4', 'line': None, 'bad': 'x', 'negative': -2, '': 5}},
        {'doc_id': 'doc-2', 'group_counts': 'not-a-map'},
    ])

    output = select_docs(None, {'source_config': {'kb_ids': ['kb-1'], 'max_docs': 2},
                                'import_cases_manifest': import_manifest()}, client)

    assert output['selected_docs']['docs'] == [
        {'kb_id': 'kb-1', 'doc_id': 'doc-1', 'filename': 'fallback-name.pdf', 'file_type': '', 'status': 'ready',
         'group_counts': {'block': 4, 'line': 0, 'bad': 0, 'negative': 0}},
        {'kb_id': 'kb-1', 'doc_id': 'doc-2', 'filename': 'doc-2', 'file_type': '', 'status': '', 'group_counts': {}},
    ]


def test_build_chunks_materializer_outputs_partitioned_chunk(monkeypatch):
    monkeypatch.setattr(chunks_build_module, 'CHUNK_PAGE_SIZE', 2)
    selected = {
        'kb_id': 'kb-1',
        'docs': [
            {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 2}},
            {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 1}},
        ],
        'stats': {'matched': 2, 'selected': 2},
        'params': {'kb_id': 'kb-1', 'max_docs': 2, 'target_case_count': 2},
    }
    client = FakeKnowledgeBaseClient(chunks={
        ('doc-1', 'block'): [[node('chunk-1', text='one', group='block'),
                              node('chunk-2', text='two', group='block')]],
        ('doc-2', 'block'): [[node('chunk-3', text='three', group='block', embedding={'default': [3.0]})]],
    })

    candidates = candidate_payload(selected, {'groups': ['block', 'line']}, client)
    output = build_chunks(chunk_ctx('chunk_0002'), {'build_chunk_candidates': candidates})

    assert output == {'chunk': {
        'available': True,
        'kb_id': 'kb-1',
        'chunk_id': 'chunk-2',
        'doc_id': 'doc-1',
        'filename': 'a.pdf',
        'group': 'block',
        'type': 'text',
        'text': 'two',
        'embedding': {'model': 'default', 'vector': [0.4472135954999579, 0.8944271909999159]},
            'metadata': {
                'doc': {'kb_id': 'kb-1', 'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf',
                        'status': 'success', 'group_counts': {'block': 2}},
                'node_metadata': {'type': 'text', 'page': 1},
                'node_global_metadata': {'filename': 'fallback.pdf'},
            },
    }}
    assert [(call['doc_id'], call['group']) for call in client.chunk_calls] == [
        ('doc-1', 'block'),
        ('doc-2', 'block'),
    ]


def test_build_chunks_manifest_materializer_outputs_built_chunks():
    selected = {
        'kb_id': 'kb-1',
        'docs': [
            {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 2}},
            {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 1}},
        ],
        'stats': {'matched': 2, 'selected': 2},
        'params': {'kb_id': 'kb-1', 'max_docs': 2, 'target_case_count': 2},
    }
    candidates = {
        'chunks': [],
        'selection_stats': {'scanned_count': 3, 'accepted_count': 3, 'filtered_count_by_type': {}},
        'target_chunk_count': 3,
        'fallback_used': False,
        'params': {'groups': ['block', 'line']},
    }
    output = build_chunks_manifest(
        manifest_ctx(('chunk_0001', 'chunk_0002', 'chunk_0003')),
        {
            'selected_docs': selected,
            'import_cases_manifest': import_manifest(target=3),
            'chunk': (
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'filename': 'a.pdf',
                 'group': 'block', 'type': 'text', 'text': 'one', 'embedding': {}, 'metadata': {}},
                {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-1', 'filename': 'a.pdf',
                 'group': 'block', 'type': 'text', 'text': 'two', 'embedding': {}, 'metadata': {}},
                {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-2', 'filename': 'b.pdf',
                 'group': 'block', 'type': 'text', 'text': 'three', 'embedding': {}, 'metadata': {}},
            ),
            'build_chunk_candidates': candidates,
        },
    )

    assert output == {'build_chunks_manifest': {
            'source': {'kb_ids': [], 'selected_docs_ref': 'dataset.selected_docs@v1'},
        'chunks': [
                {'available': True, 'kb_id': '', 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'filename': 'a.pdf',
             'group': 'block', 'type': 'text', 'partition': 'chunk_0001'},
                {'available': True, 'kb_id': '', 'chunk_id': 'chunk-2', 'doc_id': 'doc-1', 'filename': 'a.pdf',
             'group': 'block', 'type': 'text', 'partition': 'chunk_0002'},
                {'available': True, 'kb_id': '', 'chunk_id': 'chunk-3', 'doc_id': 'doc-2', 'filename': 'b.pdf',
             'group': 'block', 'type': 'text', 'partition': 'chunk_0003'},
        ],
        'stats': {
            'chunk_count': 3,
            'slot_count': 3,
            'empty_count': 0,
            'scanned_count': 3,
            'accepted_count': 3,
            'filtered_count_by_type': {},
                'target_chunk_count': 3,
                'auto_case_count': 3,
            'doc_count': 2,
            'group_counts': {'block': 3},
            'doc_group_stats': [
                {'doc_id': 'doc-1', 'filename': 'a.pdf', 'total': 2, 'groups': {'block': 2}},
                {'doc_id': 'doc-2', 'filename': 'b.pdf', 'total': 1, 'groups': {'block': 1}},
            ],
            'fallback_used': False,
            'warnings': [],
        },
        'params': {'groups': ['block', 'line'], 'allowed_types': DEFAULT_ALLOWED_TYPES},
    }}


def test_build_chunks_params_defaults_and_explicit_allowed_types_replace_defaults():
    assert BuildChunksParams.from_dict({'groups': ['block']}).to_dict() == {
        'groups': ['block'],
        'allowed_types': DEFAULT_ALLOWED_TYPES,
    }
    assert BuildChunksParams.from_dict({
        'groups': [' block '],
        'allowed_types': [' TABLE ', 'unknown'],
    }).to_dict() == {
        'groups': ['block'],
        'allowed_types': ['table', 'unknown'],
    }


def test_build_chunks_filters_types_before_consuming_doc_group_quota():
    selected = {
        'kb_id': 'kb-1',
        'docs': [{'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
                  'group_counts': {'block': 5}}],
        'params': {'target_case_count': 2},
    }
    client = FakeKnowledgeBaseClient(chunks={
        ('doc-1', 'block'): [[
            node('heading-1', text='Chapter 1', metadata={'type': 'heading'}),
            node('paragraph-1', text='Body', metadata={'type': 'paragraph'}),
            node('list-1', text='- item', metadata={'type': 'list'}),
            node('table-1', text='| key | value |', metadata={'type': 'table'}),
            node('formula-1', text='x = y', metadata={'type': 'formula'}),
        ]],
    })
    params = {'groups': ['block']}

    candidates = candidate_payload(selected, params, client)
    first = build_chunks(chunk_ctx('chunk_0001'), {'build_chunk_candidates': candidates})['chunk']
    second = build_chunks(chunk_ctx('chunk_0002'), {'build_chunk_candidates': candidates})['chunk']
    third = build_chunks(chunk_ctx('chunk_0003'), {'build_chunk_candidates': candidates})['chunk']
    assert first['chunk_id'] == 'paragraph-1'
    assert second['chunk_id'] == 'table-1'
    assert third['chunk_id'] == 'formula-1'
    assert third['type'] == 'formula'
    assert candidates['selection_stats'] == {
        'scanned_count': 5,
        'accepted_count': 3,
        'filtered_count_by_type': {'heading': 1, 'list': 1},
    }


def test_build_chunks_manifest_exposes_type_and_selection_stats():
    selected = {
        'kb_id': 'kb-1',
        'docs': [{'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
                  'group_counts': {'block': 5}}],
        'params': {'target_case_count': 2},
    }
    stats = {'scanned_count': 5, 'accepted_count': 3, 'filtered_count_by_type': {'heading': 1, 'list': 1}}
    chunks = tuple({
        'available': True,
        'chunk_id': chunk_id,
        'doc_id': 'doc-1',
        'filename': 'a.pdf',
        'group': 'block',
        'type': chunk_type,
        'text': chunk_id,
        'embedding': {},
        'metadata': {},
    } for chunk_id, chunk_type in (
        ('paragraph-1', 'paragraph'), ('table-1', 'table'), ('formula-1', 'formula'),
    ))

    built = build_chunks_manifest(
        manifest_ctx(('chunk_0001', 'chunk_0002', 'chunk_0003')),
            {'selected_docs': selected, 'import_cases_manifest': import_manifest(target=3), 'chunk': chunks, 'build_chunk_candidates': {
            'chunks': list(chunks), 'selection_stats': stats, 'target_chunk_count': 3,
            'fallback_used': False, 'params': {'groups': ['block']},
        }},
    )['build_chunks_manifest']

    assert [item['type'] for item in built['chunks']] == ['paragraph', 'table', 'formula']
    assert built['stats']['scanned_count'] == 5
    assert built['stats']['accepted_count'] == 3
    assert built['stats']['filtered_count_by_type'] == {'heading': 1, 'list': 1}
    assert built['params']['allowed_types'] == DEFAULT_ALLOWED_TYPES


def test_build_chunks_manifest_materializer_samples_group_first_and_falls_back():
    selected = {
        'kb_id': 'kb-1',
        'docs': [
            {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 2, 'line': 4}},
            {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 1, 'line': 4}},
        ],
        'stats': {'matched': 2, 'selected': 2},
        'params': {'kb_id': 'kb-1', 'max_docs': 2, 'target_case_count': 3},
    }

    output = build_chunks_manifest(
        manifest_ctx(('chunk_0001', 'chunk_0002', 'chunk_0003', 'chunk_0004', 'chunk_0005')),
        {
            'selected_docs': selected,
            'import_cases_manifest': import_manifest(target=5),
            'chunk': (
                {'available': True, 'chunk_id': 'doc-1-block-1', 'doc_id': 'doc-1', 'filename': 'a.pdf',
                 'group': 'block', 'type': 'text', 'text': '1', 'embedding': {}, 'metadata': {}},
                {'available': True, 'chunk_id': 'doc-1-block-2', 'doc_id': 'doc-1', 'filename': 'a.pdf',
                 'group': 'block', 'type': 'text', 'text': '2', 'embedding': {}, 'metadata': {}},
                {'available': True, 'chunk_id': 'doc-2-block-1', 'doc_id': 'doc-2', 'filename': 'b.pdf',
                 'group': 'block', 'type': 'text', 'text': '3', 'embedding': {}, 'metadata': {}},
                {'available': True, 'chunk_id': 'doc-1-line-1', 'doc_id': 'doc-1', 'filename': 'a.pdf',
                 'group': 'line', 'type': 'text', 'text': '4', 'embedding': {}, 'metadata': {}},
                {'available': True, 'chunk_id': 'doc-2-line-1', 'doc_id': 'doc-2', 'filename': 'b.pdf',
                 'group': 'line', 'type': 'text', 'text': '5', 'embedding': {}, 'metadata': {}},
            ),
            'build_chunk_candidates': {
                'chunks': [], 'selection_stats': {'scanned_count': 5, 'accepted_count': 5, 'filtered_count_by_type': {}},
                'target_chunk_count': 5, 'fallback_used': True, 'params': {'groups': ['block', 'line']},
            },
        },
    )

    built = output['build_chunks_manifest']
    assert [chunk['chunk_id'] for chunk in built['chunks']] == [
        'doc-1-block-1', 'doc-1-block-2', 'doc-2-block-1', 'doc-1-line-1', 'doc-2-line-1',
    ]
    assert built['stats']['group_counts'] == {'block': 3, 'line': 2}
    assert built['stats']['target_chunk_count'] == 5
    assert built['stats']['fallback_used'] is True
    assert built['stats']['warnings'] == ['fallback group sampling was used']


def test_build_chunks_materializer_outputs_placeholder_when_actual_chunks_below_target():
    selected = {
        'kb_id': 'kb-1',
        'docs': [
            {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 5}},
        ],
        'stats': {'matched': 1, 'selected': 1},
        'params': {'kb_id': 'kb-1', 'max_docs': 1, 'target_case_count': 5},
    }
    client = FakeKnowledgeBaseClient(chunks={
        ('doc-1', 'block'): [[node('chunk-1', group='block'), node('chunk-2', group='block')]],
    })

    candidates = candidate_payload(selected, {'groups': ['block']}, client)
    output = build_chunks(chunk_ctx('chunk_0008'), {'build_chunk_candidates': candidates})

    assert output == {'chunk': {
        'available': False,
        'chunk_id': 'unavailable:chunk_0008',
        'doc_id': '__unavailable__',
        'filename': '',
        'group': 'block',
        'type': 'placeholder',
        'text': 'Unavailable chunk placeholder.',
        'embedding': {'model': '', 'vector': []},
        'metadata': {'partition': 'chunk_0008', 'available': False},
    }}


def test_build_chunks_manifest_materializer_warns_when_actual_chunks_below_target():
    selected = {
        'kb_id': 'kb-1',
        'docs': [
            {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 5}},
        ],
        'stats': {'matched': 1, 'selected': 1},
        'params': {'kb_id': 'kb-1', 'max_docs': 1, 'target_case_count': 5},
    }
    partitions = tuple(f'chunk_{index:04d}' for index in range(1, 9))
    chunks = tuple(
        {'available': index < 2, 'chunk_id': f'chunk-{index + 1}' if index < 2 else f'unavailable:{partitions[index]}',
         'doc_id': 'doc-1' if index < 2 else '__unavailable__',
         'filename': 'a.pdf' if index < 2 else '',
         'group': 'block',
         'type': 'text' if index < 2 else 'placeholder',
         'text': 'chunk text' if index < 2 else 'Unavailable chunk placeholder.',
         'embedding': {},
         'metadata': {}}
        for index in range(8)
    )

    output = build_chunks_manifest(
        manifest_ctx(partitions),
        {'selected_docs': selected, 'import_cases_manifest': import_manifest(target=8), 'chunk': chunks, 'build_chunk_candidates': {
            'chunks': [], 'selection_stats': {'scanned_count': 2, 'accepted_count': 2, 'filtered_count_by_type': {}},
            'target_chunk_count': 8, 'fallback_used': False, 'params': {'groups': ['block']},
        }},
    )

    stats = output['build_chunks_manifest']['stats']
    assert stats['chunk_count'] == 2
    assert stats['slot_count'] == 8
    assert stats['empty_count'] == 6
    assert stats['target_chunk_count'] == 8
    assert stats['fallback_used'] is False
    assert stats['warnings'] == ['chunk build produced 2 chunks, below target 8; continuing']


def test_build_chunks_materializer_rejects_empty_sampling_plan():
    selected = {
        'kb_id': 'kb-1',
        'docs': [{'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
                  'group_counts': {}}],
        'stats': {'matched': 1, 'selected': 1},
        'params': {'kb_id': 'kb-1', 'max_docs': 1},
    }

    with pytest.raises(ValueError, match='sampling plan is empty'):
        candidate_payload(selected, {'groups': ['block']}, FakeKnowledgeBaseClient())


def test_build_chunks_manifest_keeps_static_slots_when_auto_chunk_target_is_smaller_or_larger():
    selected = {
        'kb_id': 'kb-1',
        'docs': [
            {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 3}},
        ],
        'params': {'target_case_count': 2},
    }

    output = build_chunks_manifest(
            manifest_ctx(('chunk_0001', 'chunk_0002')),
            {
                'selected_docs': selected,
                'import_cases_manifest': import_manifest(target=5),
                'chunk': (
                    {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'filename': 'a.pdf',
                     'group': 'block', 'type': 'text', 'text': '1', 'embedding': {}, 'metadata': {}},
                    {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-1', 'filename': 'a.pdf',
                     'group': 'block', 'type': 'text', 'text': '2', 'embedding': {}, 'metadata': {}},
                ),
                'build_chunk_candidates': {
                    'chunks': [], 'selection_stats': {'scanned_count': 0, 'accepted_count': 0, 'filtered_count_by_type': {}},
                    'target_chunk_count': 3, 'fallback_used': False, 'params': {'groups': ['block']},
                },
            },
    )['build_chunks_manifest']

    assert output['stats']['slot_count'] == 2
    assert output['stats']['target_chunk_count'] == 3
