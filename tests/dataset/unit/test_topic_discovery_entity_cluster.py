import pytest

from evo.operations.dataset.topic_discovery import topic_discovery_entity_cluster


def _node(**overrides):
    item = {
        'chunk_id': 'chunk-1',
        'doc_id': 'doc-1',
        'group': 'block',
        'entities': ['Tesla'],
    }
    item.update(overrides)
    return item


def _edge(**overrides):
    item = {
        'source_chunk_id': 'chunk-1',
        'target_chunk_id': 'chunk-2',
        'score': 1.0,
        'overlapped_items': ['Tesla'],
    }
    item.update(overrides)
    return item


def _inputs(*, nodes=None, edges=None, params=None):
    return {
        'entity_graph': {
            'nodes': nodes or [
                _node(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla', 'EV']),
                _node(chunk_id='chunk-2', doc_id='doc-2', entities=['Tesla']),
                _node(chunk_id='chunk-3', doc_id='doc-3', entities=['SpaceX', 'Shanghai']),
            ],
            'edges': edges or [
                _edge(source_chunk_id='chunk-1', target_chunk_id='chunk-2', overlapped_items=['Tesla']),
            ],
        },
        'topic_discovery_entity_cluster_params': params or {},
    }


def test_topic_discovery_entity_cluster_returns_clusters_with_defaults():
    output = topic_discovery_entity_cluster(None, _inputs())

    assert list(output.keys()) == ['entity_clusters']
    assert output['entity_clusters'] == {
        'clusters': [
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
        ],
        'stats': {
            'source_node_count': 3,
            'source_edge_count': 1,
            'edge_cluster_count': 1,
            'singleton_cluster_count': 1,
            'cluster_count': 2,
            'topic_merge_count': 0,
        },
        'params': {'topic_merge_similarity_threshold': 0.95},
    }


def test_topic_discovery_entity_cluster_builds_topic_specific_components_and_overlapping_membership():
    output = topic_discovery_entity_cluster(
        None,
        _inputs(
            nodes=[
                _node(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla', 'EV']),
                _node(chunk_id='chunk-2', doc_id='doc-2', entities=['Tesla', 'EV']),
                _node(chunk_id='chunk-3', doc_id='doc-3', entities=['EV']),
                _node(chunk_id='chunk-4', doc_id='doc-4', entities=['SpaceX']),
            ],
            edges=[
                _edge(source_chunk_id='chunk-1', target_chunk_id='chunk-2', overlapped_items=['Tesla', 'EV']),
                _edge(source_chunk_id='chunk-2', target_chunk_id='chunk-3', overlapped_items=['EV']),
            ],
        ),
    )['entity_clusters']

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
            'topics': ['EV'],
            'chunk_ids': ['chunk-1', 'chunk-2', 'chunk-3'],
            'chunk_count': 3,
            'scores': {},
            'metadata': {},
        },
        {
            'cluster_id': 'entity_cluster_000003',
            'cluster_type': 'entity',
            'topics': ['SpaceX'],
            'chunk_ids': ['chunk-4'],
            'chunk_count': 1,
            'scores': {},
            'metadata': {},
        },
    ]


def test_topic_discovery_entity_cluster_merges_only_edge_derived_clusters_and_keeps_singletons_separate():
    output = topic_discovery_entity_cluster(
        None,
        _inputs(
            nodes=[
                _node(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla']),
                _node(chunk_id='chunk-2', doc_id='doc-2', entities=['Tesla Inc']),
                _node(chunk_id='chunk-3', doc_id='doc-3', entities=['Tesla Incorporated']),
                _node(chunk_id='chunk-4', doc_id='doc-4', entities=['Tesla Inc']),
            ],
            edges=[
                _edge(source_chunk_id='chunk-1', target_chunk_id='chunk-2', overlapped_items=['Tesla']),
                _edge(source_chunk_id='chunk-2', target_chunk_id='chunk-3', overlapped_items=['Tesla Inc']),
            ],
            params={'topic_merge_similarity_threshold': 0.8},
        ),
    )['entity_clusters']

    assert output['clusters'] == [
        {
            'cluster_id': 'entity_cluster_000001',
            'cluster_type': 'entity',
            'topics': ['Tesla', 'Tesla Inc'],
            'chunk_ids': ['chunk-1', 'chunk-2', 'chunk-3'],
            'chunk_count': 3,
            'scores': {},
            'metadata': {},
        },
        {
            'cluster_id': 'entity_cluster_000002',
            'cluster_type': 'entity',
            'topics': ['Tesla Inc'],
            'chunk_ids': ['chunk-4'],
            'chunk_count': 1,
            'scores': {},
            'metadata': {},
        },
    ]
    assert output['stats']['topic_merge_count'] == 1
    assert output['stats']['singleton_cluster_count'] == 1


def test_topic_discovery_entity_cluster_rejects_invalid_contract_and_params():
    empty = topic_discovery_entity_cluster(
        None,
        {'entity_graph': {'nodes': [], 'edges': []}, 'topic_discovery_entity_cluster_params': {}},
    )['entity_clusters']
    assert empty['clusters'] == []
    assert empty['stats']['cluster_count'] == 0

    with pytest.raises(ValueError, match='duplicate node chunk_id: chunk-1'):
        topic_discovery_entity_cluster(
            None,
            _inputs(nodes=[_node(chunk_id='chunk-1'), _node(chunk_id='chunk-1', doc_id='doc-2')]),
        )

    with pytest.raises(ValueError, match='entities must contain only non-empty strings'):
        topic_discovery_entity_cluster(
            None,
            _inputs(nodes=[_node(entities=['Tesla', ' '])], edges=[]),
        )

    with pytest.raises(ValueError, match='edge endpoint must belong to entity_graph.nodes'):
        topic_discovery_entity_cluster(
            None,
            _inputs(edges=[_edge(target_chunk_id='chunk-999')]),
        )

    with pytest.raises(ValueError, match='topic_merge_similarity_threshold must be between 0 and 1'):
        topic_discovery_entity_cluster(
            None,
            _inputs(params={'topic_merge_similarity_threshold': 1.1}),
        )
