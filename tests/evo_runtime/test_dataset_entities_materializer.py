from __future__ import annotations

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.evo.adapter import build_evo_artifact_adapter
from evo.artifact_runtime.evo.flow_ops import default_evo_ops
from evo.artifact_runtime.kernel import ArtifactKey, SQLiteArtifactStore
from evo.operations.dataset.entities import chunk_entities_extract, chunk_entities_extract_manifest


def test_dataset_entity_fixed_ops_materialize_partitioned_chunks(tmp_path):
    ops = {
        op.op_id: op
        for op in default_evo_ops(('case_0001', 'case_0002'))
        if op.op_id in {'dataset.chunk_entities_extract', 'dataset.chunk_entities_extract_manifest'}
    }
    store = SQLiteArtifactStore(tmp_path / 'store')
    responses = iter(['{"entities":["Tesla"]}', '{"entities":[]}'])
    adapter = build_evo_artifact_adapter(
        store,
        tuple(ops.values()),
        {
            'dataset.chunk_entities_extract': lambda ctx, inputs: chunk_entities_extract(
                ctx, inputs, llm_complete=lambda prompt: next(responses)
            ),
            'dataset.chunk_entities_extract_manifest': chunk_entities_extract_manifest,
        },
    )
    run_id = 'run-1'
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.DATASET_BUILD_CHUNKS_MANIFEST),
        {'chunks': [
            {'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block'},
            {'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block'},
        ]},
        idempotency_key='seed:build_chunks_manifest',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.DATASET_CHUNK_ENTITIES_EXTRACT_PARAMS),
        {'max_entities_per_chunk': 3},
        idempotency_key='seed:extract_params',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST_PARAMS),
        {},
        idempotency_key='seed:assemble_params',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK, 'case_0001'),
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block', 'text': 'Tesla grew.'},
        idempotency_key='seed:chunk:1',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK, 'case_0002'),
        {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block', 'text': 'Plain text.'},
        idempotency_key='seed:chunk:2',
    )

    assert adapter.tick(run_id).status == 'ok'

    ref = adapter.effective_artifacts(run_id)[ArtifactKey.of(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST)]
    record = adapter.get(run_id, ref)
    assert record is not None
    assert record.value['chunks'] == [
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
         'partition': '', 'entities': ['Tesla']},
        {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block',
         'partition': '', 'entities': []},
    ]
