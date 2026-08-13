import pytest

from evo.operations.dataset.topic_discovery import EmbeddingClusterParams, topic_discovery_embedding_cluster


def _chunk(index, vector):
    return {
        'available': True,
        'chunk_id': f'chunk-{index}',
        'doc_id': f'doc-{index}',
        'group': 'block',
        'text': f'chunk {index}',
        'embedding': {'model': 'default', 'vector': vector},
    }


def _inputs(params=None):
    return {
        'chunk': (_chunk(1, [1.0, 0.0]), _chunk(2, [0.9, 0.1]), _chunk(3, [0.0, 1.0])),
        'topic_discovery_embedding_cluster_params': params or {
            'umap_n_neighbors': 2,
            'umap_n_components': 1,
            'min_cluster_size': 2,
            'min_samples': 1,
        },
    }


def test_embedding_cluster_uses_documented_default_parameters():
    assert EmbeddingClusterParams.from_dict({}).to_dict() == {
        'umap_n_neighbors': 15,
        'umap_n_components': 10,
        'min_cluster_size': 2,
        'min_samples': 2,
    }


def test_embedding_cluster_consumes_normalized_build_chunk_vectors():
    output = topic_discovery_embedding_cluster(
        None,
        _inputs(),
        reducer=lambda matrix, params: matrix,
        clusterer=lambda matrix, params: [0, 0, -1],
    )['embedding_cluster_candidates']

    assert [item['chunk_ids'] for item in output['clusters']] == [['chunk-1', 'chunk-2'], ['chunk-3']]


def test_embedding_cluster_returns_empty_candidates_when_capacity_is_insufficient():
    inputs = _inputs({
        'umap_n_neighbors': 3,
        'umap_n_components': 1,
        'min_cluster_size': 3,
        'min_samples': 1,
    })
    inputs['chunk'] = inputs['chunk'][:2]

    output = topic_discovery_embedding_cluster(
        None,
        inputs,
        reducer=lambda *_: pytest.fail('UMAP must not run when embedding capacity is insufficient'),
        clusterer=lambda *_: pytest.fail('HDBSCAN must not run when embedding capacity is insufficient'),
    )['embedding_cluster_candidates']

    assert output['clusters'] == []
    assert output['skipped_chunks'] == [
        {'chunk_id': 'chunk-1', 'reason': 'insufficient_embedding_capacity',
         'detail': '2 eligible embedding chunks; 4 required'},
        {'chunk_id': 'chunk-2', 'reason': 'insufficient_embedding_capacity',
         'detail': '2 eligible embedding chunks; 4 required'},
    ]
    assert output['stats'] == {
        'source_chunk_count': 2,
        'eligible_embedding_chunk_count': 2,
        'embedding_chunk_count': 0,
        'required_embedding_chunk_count': 4,
        'skipped_chunk_count': 2,
        'candidate_count': 0,
        'noise_candidate_count': 0,
    }


def test_embedding_cluster_returns_empty_candidates_when_all_chunks_are_unavailable():
    inputs = _inputs()
    inputs['chunk'] = tuple({**chunk, 'available': False} for chunk in inputs['chunk'])

    output = topic_discovery_embedding_cluster(
        None,
        inputs,
        reducer=lambda *_: pytest.fail('UMAP must not run without eligible embeddings'),
        clusterer=lambda *_: pytest.fail('HDBSCAN must not run without eligible embeddings'),
    )['embedding_cluster_candidates']

    assert output['clusters'] == []
    assert [item['reason'] for item in output['skipped_chunks']] == [
        'unavailable_chunk', 'unavailable_chunk', 'unavailable_chunk',
    ]
    assert output['stats'] == {
        'source_chunk_count': 3,
        'eligible_embedding_chunk_count': 0,
        'embedding_chunk_count': 0,
        'required_embedding_chunk_count': 3,
        'skipped_chunk_count': 3,
        'candidate_count': 0,
        'noise_candidate_count': 0,
    }


@pytest.mark.parametrize('params', [
    {'umap_n_neighbors': 1},
    {'min_cluster_size': 1},
])
def test_embedding_cluster_rejects_parameters_that_must_be_greater_than_one(params):
    with pytest.raises(ValueError, match='greater than 1'):
        topic_discovery_embedding_cluster(
            None, _inputs(params), reducer=lambda matrix, params: matrix, clusterer=lambda matrix, params: [0] * len(matrix)
        )


@pytest.mark.parametrize('embedding', [
    {}, {'model': 'default'}, {'vector': [1.0, 0.0]}, {'model': 'default', 'vector': [0.0, 0.0]},
])
def test_embedding_cluster_skips_invalid_normalized_embeddings(embedding):
    values = list(_inputs()['chunk'])
    values[0] = {**values[0], 'embedding': embedding}
    output = topic_discovery_embedding_cluster(
        None, {**_inputs(), 'chunk': tuple(values)}, reducer=lambda matrix, params: matrix,
        clusterer=lambda matrix, params: [0] * len(matrix),
    )['embedding_cluster_candidates']

    assert output['clusters'] == []
    assert output['skipped_chunks'][0]['chunk_id'] == 'chunk-1'
    assert output['skipped_chunks'][0]['reason'] == 'invalid_embedding'
