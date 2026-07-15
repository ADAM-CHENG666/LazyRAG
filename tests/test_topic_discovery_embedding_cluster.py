import pytest

from evo.operations.dataset.topic_discovery import topic_discovery_embedding_cluster


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


def test_embedding_cluster_consumes_normalized_build_chunk_vectors():
    output = topic_discovery_embedding_cluster(
        None,
        _inputs(),
        reducer=lambda matrix, params: matrix,
        clusterer=lambda matrix, params: [0, 0, -1],
    )['embedding_cluster_candidates']

    assert [item['chunk_ids'] for item in output['clusters']] == [['chunk-1', 'chunk-2'], ['chunk-3']]


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
def test_embedding_cluster_rejects_invalid_normalized_embedding_contract(embedding):
    values = list(_inputs()['chunk'])
    values[0] = {**values[0], 'embedding': embedding}
    with pytest.raises(ValueError, match='not enough embedding chunks'):
        topic_discovery_embedding_cluster(
            None, {**_inputs(), 'chunk': tuple(values)}, reducer=lambda matrix, params: matrix,
            clusterer=lambda matrix, params: [0] * len(matrix),
        )
