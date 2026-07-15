from __future__ import annotations

from types import SimpleNamespace

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.evo.adapter import build_evo_artifact_adapter
from evo.artifact_runtime.evo.flow_ops import default_evo_ops
from evo.artifact_runtime.kernel import ArtifactKey, SQLiteArtifactStore
from evo.operations.dataset.chunks_build import build_chunks, build_chunks_manifest


class FakeKnowledgeBaseClient:
    def __init__(self, chunks=None):
        self.chunks = chunks or {}

    def iter_chunks(self, kb_id, doc_ids, groups, page_size):
        for doc_id in doc_ids:
            for group in groups:
                for batch in self.chunks.get((doc_id, group), []):
                    yield batch


def node(uid, text, group='block'):
    return SimpleNamespace(
        uid=uid,
        text=text,
        group=group,
        embedding={'default': [1.0]},
        metadata={'type': 'text'},
        global_metadata={'filename': 'fallback.pdf'},
    )


def test_build_chunks_fixed_ops_materialize_partitioned_chunks_and_manifest(tmp_path):
    ops = {
        op.op_id: op
        for op in default_evo_ops(('chunk_0001', 'chunk_0002', 'chunk_0003'))
        if op.op_id in {'dataset.build_chunks', 'dataset.build_chunks_manifest'}
    }
    store = SQLiteArtifactStore(tmp_path / 'store')
    client = FakeKnowledgeBaseClient(chunks={
        ('doc-1', 'block'): [[node('chunk-1', 'one'), node('chunk-2', 'two')]],
        ('doc-2', 'block'): [[node('chunk-3', 'three')]],
    })
    adapter = build_evo_artifact_adapter(
        store,
        tuple(ops.values()),
        {
            'dataset.build_chunks': lambda ctx, inputs: build_chunks(ctx, inputs, kb_client=client),
            'dataset.build_chunks_manifest': build_chunks_manifest,
        },
    )
    run_id = 'run-1'
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.DATASET_SELECTED_DOCS),
        {
            'kb_id': 'kb-1',
            'docs': [
                {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
                 'group_counts': {'block': 2}},
                {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'status': 'success',
                 'group_counts': {'block': 1}},
            ],
            'stats': {'matched': 2, 'selected': 2},
            'params': {'kb_id': 'kb-1', 'max_docs': 2, 'target_case_count': 2},
        },
        idempotency_key='seed:selected_docs',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.DATASET_BUILD_CHUNKS_PARAMS),
        {'groups': ['block']},
        idempotency_key='seed:build_chunks_params',
    )

    assert adapter.tick(run_id).status == 'ok'

    chunk_ref = adapter.effective_artifacts(run_id)[ArtifactKey(C.DATASET_CHUNK, 'chunk_0002')]
    chunk_record = adapter.get(run_id, chunk_ref)
    assert chunk_record is not None
    assert chunk_record.value['chunk_id'] == 'chunk-2'
    assert chunk_record.value['available'] is True
    assert chunk_record.value['embedding'] == {'model': 'default', 'vector': [1.0]}

    built_ref = adapter.effective_artifacts(run_id)[ArtifactKey.of(C.DATASET_BUILD_CHUNKS_MANIFEST)]
    built_record = adapter.get(run_id, built_ref)
    assert built_record is not None
    assert built_record.value['chunks'] == [
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'filename': 'a.pdf',
         'group': 'block', 'partition': 'chunk_0001'},
        {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-1', 'filename': 'a.pdf',
         'group': 'block', 'partition': 'chunk_0002'},
        {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-2', 'filename': 'b.pdf',
         'group': 'block', 'partition': 'chunk_0003'},
    ]
