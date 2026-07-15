import pytest

from evo.operations.dataset.topic_discovery import topic_discovery_manifest


def _entity_cluster(**overrides):
    item = {
        'cluster_id': 'entity_cluster_000001',
        'cluster_type': 'entity',
        'topics': ['Tesla'],
        'chunk_ids': ['chunk-1'],
        'chunk_count': 1,
        'scores': {},
        'metadata': {},
    }
    item.update(overrides)
    return item


def _embedding_cluster(**overrides):
    item = {
        'cluster_id': 'embedding_cluster_000001',
        'cluster_type': 'embedding',
        'topics': ['mobility'],
        'chunk_ids': ['chunk-1', 'chunk-2'],
        'chunk_count': 2,
        'scores': {},
        'metadata': {},
    }
    item.update(overrides)
    return item


def _inputs(*, entity_clusters=None, embedding_clusters=None):
    return {
        'entity_clusters': {'clusters': entity_clusters if entity_clusters is not None else [_entity_cluster()]},
        'embedding_clusters': {'clusters': embedding_clusters if embedding_clusters is not None else [_embedding_cluster()]},
    }


def test_topic_discovery_manifest_merges_two_paths_and_relabels_cluster_ids():
    output = topic_discovery_manifest(None, _inputs())

    assert list(output.keys()) == ['topic_discovery_manifest']
    assert output['topic_discovery_manifest'] == {
        'clusters': [
            {
                'cluster_id': 'entity_000001',
                'cluster_type': 'entity',
                'topics': ['Tesla'],
                'chunk_ids': ['chunk-1'],
                'chunk_count': 1,
                'scores': {},
                'metadata': {},
            },
            {
                'cluster_id': 'embedding_000001',
                'cluster_type': 'embedding',
                'topics': ['mobility'],
                'chunk_ids': ['chunk-1', 'chunk-2'],
                'chunk_count': 2,
                'scores': {},
                'metadata': {},
            },
        ],
        'stats': {
            'entity_cluster_count': 1,
            'embedding_cluster_count': 1,
            'total_cluster_count': 2,
            'unique_chunk_count': 2,
        },
        'params': {},
    }


def test_topic_discovery_manifest_keeps_entity_first_and_does_not_cross_source_dedup():
    output = topic_discovery_manifest(
        None,
        _inputs(
            entity_clusters=[
                _entity_cluster(cluster_id='entity_cluster_000005', topics=['Tesla'], chunk_ids=['chunk-1', 'chunk-2'], chunk_count=2),
                _entity_cluster(cluster_id='entity_cluster_000006', topics=['SpaceX'], chunk_ids=['chunk-3'], chunk_count=1),
            ],
            embedding_clusters=[
                _embedding_cluster(cluster_id='embedding_cluster_000010', topics=['mobility'], chunk_ids=['chunk-1', 'chunk-2'], chunk_count=2),
            ],
        ),
    )['topic_discovery_manifest']

    assert [cluster['cluster_id'] for cluster in output['clusters']] == [
        'entity_000001',
        'entity_000002',
        'embedding_000001',
    ]
    assert [cluster['cluster_type'] for cluster in output['clusters']] == ['entity', 'entity', 'embedding']
    assert output['stats'] == {
        'entity_cluster_count': 2,
        'embedding_cluster_count': 1,
        'total_cluster_count': 3,
        'unique_chunk_count': 3,
    }


def test_topic_discovery_manifest_rejects_invalid_contract_and_validation_errors():
    with pytest.raises(ValueError, match='entity_clusters.clusters must be a list'):
        topic_discovery_manifest(None, {
            'entity_clusters': {'clusters': 'bad'},
            'embedding_clusters': {'clusters': [_embedding_cluster()]},
        })

    with pytest.raises(ValueError, match='cluster_type must be entity'):
        topic_discovery_manifest(None, _inputs(entity_clusters=[_entity_cluster(cluster_type='embedding')]))

    with pytest.raises(ValueError, match='chunk_count must be a positive integer'):
        topic_discovery_manifest(None, _inputs(entity_clusters=[_entity_cluster(chunk_count=0)]))

    with pytest.raises(ValueError, match='mapping must be a mapping'):
        topic_discovery_manifest(None, _inputs(entity_clusters=[_entity_cluster(scores='bad')]))

    with pytest.raises(ValueError, match='mapping must be a mapping'):
        topic_discovery_manifest(None, _inputs(embedding_clusters=[_embedding_cluster(metadata='bad')]))


@pytest.mark.parametrize(
    ('topics', 'match'),
    [
        ([], 'topics must be non-empty'),
        ([''], 'topics must contain only non-empty strings'),
        ('Tesla', 'topics must be list\\[string\\]'),
    ],
)
def test_topic_discovery_manifest_rejects_empty_or_invalid_topics(topics, match):
    with pytest.raises(ValueError, match=match):
        topic_discovery_manifest(None, _inputs(entity_clusters=[_entity_cluster(topics=topics)]))
