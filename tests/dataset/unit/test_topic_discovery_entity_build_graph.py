import pytest

from evo.operations.dataset.topic_discovery import topic_discovery_entity_build_graph


def _chunk_entity(**overrides):
    item = {
        'available': True,
        'chunk_id': 'chunk-1',
        'doc_id': 'doc-1',
        'group': 'block',
        'entities': ['Tesla'],
    }
    item.update(overrides)
    return item


def _inputs(*, chunk_entity=None, params=None):
    return {
        'chunk_entity': chunk_entity or (
            _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla']),
            _chunk_entity(chunk_id='chunk-2', doc_id='doc-2', entities=['Tesla']),
        ),
        'topic_discovery_entity_build_graph_params': params or {},
    }


def test_topic_discovery_entity_build_graph_returns_graph_payload_with_defaults():
    output = topic_discovery_entity_build_graph(
        None,
        _inputs(chunk_entity=(
            _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla']),
            _chunk_entity(chunk_id='chunk-2', doc_id='doc-2', entities=['Tesla']),
            _chunk_entity(chunk_id='chunk-3', doc_id='doc-3', entities=['SpaceX']),
        )),
    )

    assert list(output.keys()) == ['entity_graph']
    assert output['entity_graph'] == {
        'nodes': [
            {'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block', 'entities': ['Tesla']},
            {'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block', 'entities': ['Tesla']},
            {'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block', 'entities': ['SpaceX']},
        ],
        'edges': [
            {
                'source_chunk_id': 'chunk-1',
                'target_chunk_id': 'chunk-2',
                'score': 1.0,
                'overlapped_items': ['Tesla'],
            },
        ],
        'skipped_chunks': [],
        'stats': {
            'source_chunk_count': 3,
            'node_count': 3,
            'edge_count': 1,
            'skipped_chunk_count': 0,
            'noisy_entity_count': 0,
        },
        'params': {
            'entity_similarity_threshold': 0.9,
            'edge_score_threshold': 0.01,
            'noisy_entity_top_percent': 0.05,
        },
    }


def test_topic_discovery_entity_build_graph_skips_unavailable_and_empty_entities():
    output = topic_discovery_entity_build_graph(
        None,
        _inputs(chunk_entity=(
            _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla']),
            _chunk_entity(chunk_id='chunk-2', doc_id='doc-2', available=False, entities=['Tesla']),
            _chunk_entity(chunk_id='chunk-3', doc_id='doc-3', entities=[]),
        )),
    )['entity_graph']

    assert output['nodes'] == [
        {'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block', 'entities': ['Tesla']},
    ]
    assert output['skipped_chunks'] == [
        {'chunk_id': 'chunk-2', 'reason': 'unavailable_chunk', 'detail': 'chunk is unavailable'},
        {'chunk_id': 'chunk-3', 'reason': 'empty_entities', 'detail': 'chunk entities is empty'},
    ]
    assert output['stats']['skipped_chunk_count'] == 2


def test_topic_discovery_entity_build_graph_rejects_invalid_input():
    with pytest.raises(ValueError, match='chunk_entity input must be a partitioned tuple'):
        topic_discovery_entity_build_graph(
            None,
            {'chunk_entity': [], 'topic_discovery_entity_build_graph_params': {}},
        )

    with pytest.raises(ValueError, match='chunk_entity\\[\\] must be a mapping'):
        topic_discovery_entity_build_graph(
            None,
            {'chunk_entity': ('bad',), 'topic_discovery_entity_build_graph_params': {}},
        )

    with pytest.raises(ValueError, match='entities must be list\\[string\\]'):
        topic_discovery_entity_build_graph(
            None,
            _inputs(chunk_entity=(
                _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', entities='Tesla'),
            )),
        )

    with pytest.raises(ValueError, match='entities must contain only non-empty strings'):
        topic_discovery_entity_build_graph(
            None,
            _inputs(chunk_entity=(
                _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla', ' ']),
            )),
        )

    with pytest.raises(ValueError, match='entity_similarity_threshold must be between 0 and 1'):
        topic_discovery_entity_build_graph(
            None,
            _inputs(params={'entity_similarity_threshold': 2}),
        )

    with pytest.raises(ValueError, match='edge_score_threshold must be between 0 and 1'):
        topic_discovery_entity_build_graph(
            None,
            _inputs(params={'edge_score_threshold': -0.1}),
        )

    with pytest.raises(ValueError, match='noisy_entity_top_percent must be between 0 and 1'):
        topic_discovery_entity_build_graph(
            None,
            _inputs(params={'noisy_entity_top_percent': 1.1}),
        )


def test_topic_discovery_entity_build_graph_rejects_when_no_valid_nodes():
    with pytest.raises(ValueError, match='requires at least one valid node'):
        topic_discovery_entity_build_graph(
            None,
            _inputs(chunk_entity=(
                _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', available=False, entities=['Tesla']),
                _chunk_entity(chunk_id='chunk-2', doc_id='doc-2', entities=[]),
            )),
        )


def test_topic_discovery_entity_build_graph_filters_noisy_entities():
    output = topic_discovery_entity_build_graph(
        None,
        _inputs(
            chunk_entity=(
                _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla', 'Austin']),
                _chunk_entity(chunk_id='chunk-2', doc_id='doc-2', entities=['Tesla', 'Berlin']),
                _chunk_entity(chunk_id='chunk-3', doc_id='doc-3', entities=['Tesla', 'Shanghai']),
            ),
            params={'noisy_entity_top_percent': 0.5},
        ),
    )['entity_graph']

    assert output['edges'] == []
    assert output['stats']['noisy_entity_count'] == 2


def test_topic_discovery_entity_build_graph_uses_longer_overlap_item_and_applies_threshold():
    inputs = _inputs(
        chunk_entity=(
            _chunk_entity(chunk_id='chunk-1', doc_id='doc-1', entities=['Tesla', 'Robot']),
            _chunk_entity(chunk_id='chunk-2', doc_id='doc-2', entities=['Tesla Inc']),
        ),
        params={'entity_similarity_threshold': 0.8, 'edge_score_threshold': 0.5},
    )

    output = topic_discovery_entity_build_graph(None, inputs)['entity_graph']

    assert output['edges'] == [
        {
            'source_chunk_id': 'chunk-1',
            'target_chunk_id': 'chunk-2',
            'score': 0.5,
            'overlapped_items': ['Tesla Inc'],
        },
    ]

    stricter = topic_discovery_entity_build_graph(
        None,
        _inputs(
            chunk_entity=inputs['chunk_entity'],
            params={'entity_similarity_threshold': 0.8, 'edge_score_threshold': 0.6},
        ),
    )['entity_graph']
    assert stricter['edges'] == []
