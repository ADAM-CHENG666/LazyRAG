"""Behavior contracts for dynamic embedding-candidate labeling.

Clustering remains global.  Only the independent LLM naming work is split by
candidate, then reassembled into the established ``embedding_clusters`` value.
"""

import pytest

from evo.operations.dataset.topic_discovery import (
    topic_discovery_embedding_cluster,
    topic_discovery_embedding_label_cluster,
    topic_discovery_embedding_label_manifest,
)


def _chunk(index: int, vector: list[float]) -> dict[str, object]:
    return {
        'available': True,
        'chunk_id': f'chunk-{index}',
        'kb_id': 'kb-1',
        'doc_id': f'doc-{index}',
        'group': 'block',
        'text': f'content of chunk {index}',
        'embedding': {'model': 'default', 'vector': vector},
    }


def _candidate(candidate_id: str, chunk_ids: list[str]) -> dict[str, object]:
    return {
        'candidate_id': candidate_id,
        'cluster_type': 'embedding',
        'topics': [],
        'chunk_ids': chunk_ids,
        'chunk_count': len(chunk_ids),
        'scores': {'density': 0.8},
        'metadata': {'source': 'umap-hdbscan'},
    }


def _request(candidate_id: str = 'embedding_candidate_000001') -> dict[str, object]:
    return {
        **_candidate(candidate_id, ['chunk-1', 'chunk-2']),
        'chunks': [
            {'chunk_id': 'chunk-1', 'kb_id': 'kb-1', 'doc_id': 'doc-1', 'text': 'first source'},
            {'chunk_id': 'chunk-2', 'kb_id': 'kb-1', 'doc_id': 'doc-2', 'text': 'second source'},
        ],
    }


def test_global_embedding_clustering_creates_one_stable_request_per_candidate():
    """A global clustering result fans out only after all chunks were clustered together."""
    result = topic_discovery_embedding_cluster(
        None,
        {
            'chunk': (
                _chunk(1, [1.0, 0.0]),
                _chunk(2, [0.9, 0.1]),
                _chunk(3, [0.0, 1.0]),
            ),
            'topic_discovery_embedding_cluster_params': {
                'umap_n_neighbors': 2,
                'umap_n_components': 1,
                'min_cluster_size': 2,
                'min_samples': 1,
            },
        },
        reducer=lambda matrix, _params: matrix,
        clusterer=lambda _matrix, _params: [0, 0, -1],
    )

    assert list(result) == [
        'embedding_cluster_candidates',
        'embedding_label_requests',
        'embedding_label_request',
    ]
    assert result['embedding_label_requests'] == (
        'embedding_candidate_000001',
        'embedding_candidate_000002',
    )
    requests = result['embedding_label_request']
    assert requests['embedding_candidate_000001'] == {
        **result['embedding_cluster_candidates']['clusters'][0],
        'chunks': [
            {'chunk_id': 'chunk-1', 'kb_id': 'kb-1', 'doc_id': 'doc-1', 'text': 'content of chunk 1'},
            {'chunk_id': 'chunk-2', 'kb_id': 'kb-1', 'doc_id': 'doc-2', 'text': 'content of chunk 2'},
        ],
    }
    assert requests['embedding_candidate_000002']['chunks'] == [
        {'chunk_id': 'chunk-3', 'kb_id': 'kb-1', 'doc_id': 'doc-3', 'text': 'content of chunk 3'},
    ]


def test_one_partition_labels_exactly_one_candidate_and_keeps_final_cluster_contract():
    calls: list[str] = []

    def complete(prompt: str) -> str:
        calls.append(prompt)
        return '{"topics":["first topic"]}'

    result = topic_discovery_embedding_label_cluster(
        None,
        {'request': _request(), 'topic_discovery_embedding_label_params': {}},
        llm_complete=complete,
    )

    assert len(calls) == 1
    assert 'chunk-1' in calls[0]
    assert 'chunk-2' in calls[0]
    assert result == {'embedding_cluster': {
        'cluster_id': 'embedding_candidate_000001',
        'cluster_type': 'embedding',
        'topics': ['first topic'],
        'chunk_ids': ['chunk-1', 'chunk-2'],
        'chunk_count': 2,
        'scores': {'density': 0.8},
        'metadata': {'source': 'umap-hdbscan'},
    }}


def test_label_failure_is_scoped_to_its_candidate_partition():
    with pytest.raises(ValueError, match='topics must be non-empty'):
        topic_discovery_embedding_label_cluster(
            None,
            {'request': _request('embedding_candidate_000002'), 'topic_discovery_embedding_label_params': {}},
            llm_complete=lambda _prompt: '{"topics":[]}',
        )


def test_manifest_publishes_established_clusters_only_after_all_partitions_succeed():
    candidates = {
        'clusters': [
            _candidate('embedding_candidate_000001', ['chunk-1', 'chunk-2']),
            _candidate('embedding_candidate_000002', ['chunk-3']),
        ],
        'skipped_chunks': [{'chunk_id': 'bad', 'reason': 'invalid_embedding', 'detail': 'bad vector'}],
        'stats': {'candidate_count': 2},
        'params': {'umap_n_neighbors': 15},
    }
    first = {
        'cluster_id': 'embedding_candidate_000001', 'cluster_type': 'embedding',
        'topics': ['first'], 'chunk_ids': ['chunk-1', 'chunk-2'], 'chunk_count': 2,
        'scores': {'density': 0.8}, 'metadata': {'source': 'umap-hdbscan'},
    }
    second = {
        'cluster_id': 'embedding_candidate_000002', 'cluster_type': 'embedding',
        'topics': ['second'], 'chunk_ids': ['chunk-3'], 'chunk_count': 1,
        'scores': {'density': 0.8}, 'metadata': {'source': 'umap-hdbscan'},
    }

    result = topic_discovery_embedding_label_manifest(None, {
        'embedding_cluster_candidates': candidates,
        'embedding_label_requests': ('embedding_candidate_000001', 'embedding_candidate_000002'),
        # Completion order is intentionally reversed; publish order follows partition order.
        'embedding_cluster': (second, first),
        'topic_discovery_embedding_label_params': {
            'max_topics_per_cluster': 3,
            'max_chars_per_chunk_for_label': 2048,
            'max_label_source_chunks': 8,
        },
    })

    assert result == {'embedding_clusters': {
        'clusters': [first, second],
        'skipped_chunks': candidates['skipped_chunks'],
        'stats': {
            'candidate_count': 2,
            'cluster_count': 2,
            'labeled_cluster_count': 2,
        },
        'params': {
            'max_topics_per_cluster': 3,
            'max_chars_per_chunk_for_label': 2048,
            'max_label_source_chunks': 8,
        },
    }}


@pytest.mark.parametrize('clusters', [
    (),
    ({
        'cluster_id': 'embedding_candidate_000001', 'cluster_type': 'embedding',
        'topics': ['first'], 'chunk_ids': ['chunk-1'], 'chunk_count': 1,
        'scores': {}, 'metadata': {},
    },) * 2,
])
def test_manifest_does_not_publish_a_partial_or_duplicate_dynamic_result(clusters):
    with pytest.raises(ValueError, match='missing or duplicate labeled embedding cluster'):
        topic_discovery_embedding_label_manifest(None, {
            'embedding_cluster_candidates': {
                'clusters': [
                    _candidate('embedding_candidate_000001', ['chunk-1']),
                    _candidate('embedding_candidate_000002', ['chunk-2']),
                ],
                'skipped_chunks': [],
                'stats': {},
                'params': {},
            },
            'embedding_label_requests': ('embedding_candidate_000001', 'embedding_candidate_000002'),
            'embedding_cluster': clusters,
            'topic_discovery_embedding_label_params': {},
        })
