from __future__ import annotations

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.evo.adapter import build_evo_artifact_adapter
from evo.artifact_runtime.evo.flow_ops import default_evo_ops
from evo.artifact_runtime.kernel import ArtifactKey, SQLiteArtifactStore
from evo.operations.dataset.topic_discovery import (
    topic_discovery_embedding_cluster,
    topic_discovery_embedding_label,
    topic_discovery_entity_build_graph,
    topic_discovery_entity_cluster,
    topic_discovery_manifest,
)


def test_topic_discovery_entity_path_outputs_clusters():
    graph = topic_discovery_entity_build_graph(None, {
        'chunk_entity': (
            {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
             'entities': ['Tesla']},
            {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block',
             'entities': ['Tesla']},
            {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block',
             'entities': ['SpaceX', 'Shanghai']},
        ),
        'topic_discovery_entity_build_graph_params': {'entity_similarity_threshold': 0.8},
    })['entity_graph']

    output = topic_discovery_entity_cluster(None, {
        'entity_graph': graph,
        'topic_discovery_entity_cluster_params': {},
    })['entity_clusters']

    assert output['clusters'] == [
        {
            'cluster_id': 'entity_cluster_000001',
            'cluster_type': 'entity',
            'topics': ['Tesla'],
            'chunk_ids': ['chunk-1', 'chunk-2'],
            'chunk_count': 2,
            'scores': {},
            'metadata': {},
        },
        {
            'cluster_id': 'entity_cluster_000002',
            'cluster_type': 'entity',
            'topics': ['SpaceX', 'Shanghai'],
            'chunk_ids': ['chunk-3'],
            'chunk_count': 1,
            'scores': {},
            'metadata': {},
        },
    ]
    assert output['stats']['singleton_cluster_count'] == 1


def test_topic_discovery_embedding_path_labels_candidates():
    candidates = topic_discovery_embedding_cluster(
        None,
        {
            'chunk': (
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                 'text': 'Tesla builds electric cars.', 'embedding': {'model': 'default', 'vector': [1.0, 0.0]}},
                {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block',
                 'text': 'SpaceX launches rockets.', 'embedding': {'model': 'default', 'vector': [0.99, 0.01]}},
                {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block',
                 'text': 'Shanghai has factories.', 'embedding': {'model': 'default', 'vector': [0.0, 1.0]}},
            ),
            'topic_discovery_embedding_cluster_params': {
                'umap_n_neighbors': 2,
                'umap_n_components': 1,
                'min_cluster_size': 2,
                'min_samples': 1,
            },
        },
        reducer=lambda matrix, params: matrix,
        clusterer=lambda matrix, params: [0, 0, -1],
    )['embedding_cluster_candidates']

    output = topic_discovery_embedding_label(
        None,
        {
            'embedding_cluster_candidates': candidates,
            'chunk': (
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                 'text': 'Tesla builds electric cars.', 'embedding': {}},
                {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block',
                 'text': 'SpaceX launches rockets.', 'embedding': {}},
                {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block',
                 'text': 'Shanghai has factories.', 'embedding': {}},
            ),
            'topic_discovery_embedding_label_params': {'max_topics_per_cluster': 2},
        },
        llm_complete=lambda prompt: '{"topics":["mobility"]}' if 'Tesla' in prompt else '{"topics":["city"]}',
    )['embedding_clusters']

    assert output['clusters'] == [
        {
            'cluster_id': 'embedding_cluster_000001',
            'cluster_type': 'embedding',
            'topics': ['mobility'],
            'chunk_ids': ['chunk-1', 'chunk-2'],
            'chunk_count': 2,
            'scores': {},
            'metadata': {},
        },
        {
            'cluster_id': 'embedding_cluster_000002',
            'cluster_type': 'embedding',
            'topics': ['city'],
            'chunk_ids': ['chunk-3'],
            'chunk_count': 1,
            'scores': {},
            'metadata': {},
        },
    ]
    assert 'points' not in output


def test_topic_discovery_manifest_merges_two_paths_without_points():
    output = topic_discovery_manifest(None, {
        'entity_clusters': {'clusters': [{
            'cluster_id': 'entity_cluster_000001',
            'cluster_type': 'entity',
            'topics': ['Tesla'],
            'chunk_ids': ['chunk-1'],
            'chunk_count': 1,
        }]},
        'embedding_clusters': {'clusters': [{
            'cluster_id': 'embedding_cluster_000001',
            'cluster_type': 'embedding',
            'topics': ['mobility'],
            'chunk_ids': ['chunk-1', 'chunk-2'],
            'chunk_count': 2,
        }]},
    })['topic_discovery_manifest']

    assert output == {
        'clusters': [
            {'cluster_id': 'entity_000001', 'cluster_type': 'entity', 'topics': ['Tesla'],
             'chunk_ids': ['chunk-1'], 'chunk_count': 1, 'scores': {}, 'metadata': {}},
            {'cluster_id': 'embedding_000001', 'cluster_type': 'embedding', 'topics': ['mobility'],
             'chunk_ids': ['chunk-1', 'chunk-2'], 'chunk_count': 2, 'scores': {}, 'metadata': {}},
        ],
        'stats': {
            'entity_cluster_count': 1,
            'embedding_cluster_count': 1,
            'total_cluster_count': 2,
            'unique_chunk_count': 2,
        },
        'params': {},
    }


def test_topic_discovery_fixed_ops_materialize_manifest(tmp_path):
    ops = {
        op.op_id: op
        for op in default_evo_ops(('chunk_0001', 'chunk_0002', 'chunk_0003'))
        if op.op_id.startswith('dataset.topic_discovery_')
    }
    store = SQLiteArtifactStore(tmp_path / 'store')
    adapter = build_evo_artifact_adapter(
        store,
        tuple(ops.values()),
        {
            'dataset.topic_discovery_entity_build_graph': topic_discovery_entity_build_graph,
            'dataset.topic_discovery_entity_cluster': topic_discovery_entity_cluster,
            'dataset.topic_discovery_embedding_cluster': lambda ctx, inputs: topic_discovery_embedding_cluster(
                ctx,
                inputs,
                reducer=lambda matrix, params: matrix,
                    clusterer=lambda matrix, params: [0, -1, -1],
            ),
            'dataset.topic_discovery_embedding_label': lambda ctx, inputs: topic_discovery_embedding_label(
                ctx, inputs, llm_complete=lambda prompt: '{"topics":["topic"]}'
            ),
            'dataset.topic_discovery_manifest': topic_discovery_manifest,
        },
    )
    run_id = 'run-1'
    _seed_params(adapter, run_id)
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK, 'chunk_0001'),
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
             'text': 'one', 'embedding': {'model': 'default', 'vector': [1.0, 0.0]}},
        idempotency_key='seed:chunk:1',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK, 'chunk_0002'),
        {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block',
             'text': 'two', 'embedding': {'model': 'default', 'vector': [0.0, 1.0]}},
        idempotency_key='seed:chunk:2',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK, 'chunk_0003'),
            {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block',
             'text': 'three', 'embedding': {'model': 'default', 'vector': [0.5, 0.5]}},
        idempotency_key='seed:chunk:3',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK_ENTITY, 'chunk_0001'),
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block', 'entities': ['Tesla']},
        idempotency_key='seed:entity:1',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK_ENTITY, 'chunk_0002'),
        {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block', 'entities': ['Tesla']},
        idempotency_key='seed:entity:2',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey(C.DATASET_CHUNK_ENTITY, 'chunk_0003'),
            {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block', 'entities': ['Tesla']},
        idempotency_key='seed:entity:3',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST),
        {'chunks': []},
        idempotency_key='seed:chunk-entities-manifest',
    )

    for _ in range(5):
        result = adapter.tick(run_id)
        if result.status == 'idle':
            break
        assert result.status == 'ok'

    ref = adapter.effective_artifacts(run_id)[ArtifactKey.of(C.DATASET_TOPIC_DISCOVERY_MANIFEST)]
    record = adapter.get(run_id, ref)
    assert record is not None
    assert record.value['stats'] == {
        'entity_cluster_count': 1,
        'embedding_cluster_count': 3,
        'total_cluster_count': 4,
        'unique_chunk_count': 3,
    }


def _seed_params(adapter, run_id):
    for key, value in (
        (C.DATASET_TOPIC_DISCOVERY_ENTITY_BUILD_GRAPH_PARAMS, {}),
        (C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTER_PARAMS, {}),
        (C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_PARAMS, {
            'umap_n_neighbors': 2,
            'umap_n_components': 1,
            'min_cluster_size': 2,
            'min_samples': 1,
        }),
        (C.DATASET_TOPIC_DISCOVERY_EMBEDDING_LABEL_PARAMS, {}),
    ):
        adapter.commit_external(run_id, ArtifactKey.of(key), value, idempotency_key=f'seed:{key}')
