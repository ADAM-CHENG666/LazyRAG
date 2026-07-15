import pytest

from evo.operations.dataset.topic_discovery import topic_discovery_embedding_label


def _candidate(**overrides):
    item = {
        'candidate_id': 'embedding_candidate_000001',
        'cluster_type': 'embedding',
        'topics': [],
        'chunk_ids': ['chunk-1', 'chunk-2'],
        'chunk_count': 2,
        'scores': {},
        'metadata': {},
    }
    item.update(overrides)
    return item


def _chunk(**overrides):
    item = {
        'available': True,
        'chunk_id': 'chunk-1',
        'doc_id': 'doc-1',
        'group': 'block',
        'text': 'Tesla builds electric cars.',
        'embedding': {},
    }
    item.update(overrides)
    return item


def _inputs(*, candidates=None, chunk=None, params=None):
    return {
        'embedding_cluster_candidates': {
            'clusters': candidates if candidates is not None else [
                _candidate(),
                _candidate(
                    candidate_id='embedding_candidate_000002',
                    chunk_ids=['chunk-3'],
                    chunk_count=1,
                ),
            ],
            'skipped_chunks': [{'chunk_id': 'skipped-1', 'reason': 'invalid_embedding', 'detail': 'bad'}],
        },
        'chunk': chunk if chunk is not None else (
            _chunk(chunk_id='chunk-1', doc_id='doc-1', text='Tesla builds electric cars.'),
            _chunk(chunk_id='chunk-2', doc_id='doc-2', text='SpaceX launches rockets.'),
            _chunk(chunk_id='chunk-3', doc_id='doc-3', text='Shanghai has factories.'),
        ),
        'topic_discovery_embedding_label_params': params or {},
    }


def test_topic_discovery_embedding_label_returns_clusters_and_preserves_chunk_order():
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return '{"topics":["mobility"]}' if 'chunk-1' in prompt else '{"topics":["city"]}'

    output = topic_discovery_embedding_label(None, _inputs(), llm_complete=complete)

    assert list(output.keys()) == ['embedding_clusters']
    assert output['embedding_clusters'] == {
        'clusters': [
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
        ],
        'skipped_chunks': [{'chunk_id': 'skipped-1', 'reason': 'invalid_embedding', 'detail': 'bad'}],
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
    }
    assert len(prompts) == 2


def test_topic_discovery_embedding_label_uses_default_params():
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return '{"topics":["topic"]}'

    output = topic_discovery_embedding_label(
        None,
        _inputs(candidates=[_candidate(chunk_ids=['chunk-1'], chunk_count=1)]),
        llm_complete=complete,
    )['embedding_clusters']

    assert output['params'] == {
        'max_topics_per_cluster': 3,
        'max_chars_per_chunk_for_label': 2048,
        'max_label_source_chunks': 8,
    }
    assert 'max_topics: 3' in prompts[0]


def test_topic_discovery_embedding_label_limits_source_chunks_and_truncates_text():
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return '{"topics":["topic"]}'

    topic_discovery_embedding_label(
        None,
        _inputs(
            candidates=[
                _candidate(
                    chunk_ids=['chunk-1', 'chunk-2', 'chunk-3'],
                    chunk_count=3,
                ),
            ],
            chunk=(
                _chunk(chunk_id='chunk-1', text='ABCDEFGHIJ'),
                _chunk(chunk_id='chunk-2', doc_id='doc-2', text='KLMNOPQRST'),
                _chunk(chunk_id='chunk-3', doc_id='doc-3', text='UVWXYZ'),
            ),
            params={'max_label_source_chunks': 2, 'max_chars_per_chunk_for_label': 5},
        ),
        llm_complete=complete,
    )

    assert len(prompts) == 1
    assert '- chunk-1: ABCDE' in prompts[0]
    assert '- chunk-2: KLMNO' in prompts[0]
    assert 'chunk-3' not in prompts[0]


def test_topic_discovery_embedding_label_rejects_too_many_topics():
    with pytest.raises(ValueError, match='LLM JSON call failed after 3 attempts'):
        topic_discovery_embedding_label(
            None,
            _inputs(
                candidates=[_candidate(chunk_ids=['chunk-1'], chunk_count=1)],
                params={'max_topics_per_cluster': 2},
            ),
            llm_complete=lambda prompt: '{"topics":["a","b","c"]}',
        )


@pytest.mark.parametrize(
    ('inputs', 'match'),
    [
        ({'embedding_cluster_candidates': {'clusters': 'bad'}, 'chunk': (), 'topic_discovery_embedding_label_params': {}},
         'embedding_cluster_candidates.clusters must be a list'),
        ({'embedding_cluster_candidates': {'clusters': [_candidate()]}, 'chunk': [], 'topic_discovery_embedding_label_params': {}},
         'chunk input must be a partitioned tuple'),
        ({'embedding_cluster_candidates': {'clusters': [_candidate(chunk_ids=[])]},
          'chunk': (_chunk(),), 'topic_discovery_embedding_label_params': {}},
         'chunk_ids must be non-empty'),
        ({'embedding_cluster_candidates': {'clusters': [_candidate(chunk_ids=['chunk-1'], chunk_count=1)]},
          'chunk': (_chunk(),), 'topic_discovery_embedding_label_params': {'max_topics_per_cluster': 0}},
         'max_topics_per_cluster must be a positive integer'),
        ({'embedding_cluster_candidates': {'clusters': [_candidate(chunk_ids=['chunk-1'], chunk_count=1)]},
          'chunk': (_chunk(),), 'topic_discovery_embedding_label_params': {'max_chars_per_chunk_for_label': 20001}},
         'max_chars_per_chunk_for_label must be <= 20000'),
        ({'embedding_cluster_candidates': {'clusters': [_candidate(chunk_ids=['chunk-1'], chunk_count=1)]},
          'chunk': (_chunk(),), 'topic_discovery_embedding_label_params': {'max_label_source_chunks': False}},
         'max_label_source_chunks must be a positive integer'),
    ],
)
def test_topic_discovery_embedding_label_rejects_invalid_contract_and_params(inputs, match):
    with pytest.raises(ValueError, match=match):
        topic_discovery_embedding_label(None, inputs, llm_complete=lambda prompt: '{"topics":["topic"]}')


@pytest.mark.parametrize(
    ('chunk', 'match'),
    [
        ((), 'missing chunk for label source: chunk-1'),
        ((_chunk(chunk_id='chunk-1', available=False),), 'chunk is unavailable for label source: chunk-1'),
        ((_chunk(chunk_id='chunk-1', text='   '),), 'missing chunk text for label source: chunk-1'),
    ],
)
def test_topic_discovery_embedding_label_rejects_missing_or_invalid_source_chunk(chunk, match):
    with pytest.raises(ValueError, match=match):
        topic_discovery_embedding_label(
            None,
            _inputs(
                candidates=[_candidate(chunk_ids=['chunk-1'], chunk_count=1)],
                chunk=chunk,
            ),
            llm_complete=lambda prompt: '{"topics":["topic"]}',
        )


@pytest.mark.parametrize(
    ('response', 'match'),
    [
        ('{"topics":[]}', 'topics must be non-empty'),
        ('{"topics":[""]}', 'LLM JSON call failed after 3 attempts'),
        ('not-json', 'LLM JSON call failed after 3 attempts'),
        ('{"label":"x"}', 'LLM JSON call failed after 3 attempts'),
        ('{"topics":"x"}', 'LLM JSON call failed after 3 attempts'),
    ],
)
def test_topic_discovery_embedding_label_rejects_invalid_llm_output(response, match):
    with pytest.raises(Exception, match=match):
        topic_discovery_embedding_label(
            None,
            _inputs(candidates=[_candidate(chunk_ids=['chunk-1'], chunk_count=1)]),
            llm_complete=lambda prompt: response,
        )
