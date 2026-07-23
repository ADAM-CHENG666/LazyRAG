from types import SimpleNamespace

import pytest

from evo.operations.dataset.qaplan import qaplan_plan


LANES = (
    'entity_precision_easy',
    'entity_precision_medium',
    'entity_precision_hard',
    'embedding_reasoning_easy',
    'embedding_reasoning_medium',
    'embedding_reasoning_hard',
)


def _chunk(chunk_id, doc_id=None, *, text=None, available=True):
    return {
        'available': available,
        'kb_id': 'kb-1',
        'chunk_id': chunk_id,
        'doc_id': doc_id or f'doc-{chunk_id}',
        'text': text or f'text for {chunk_id}',
    }


def _cluster(cluster_id, cluster_type, topics, chunk_ids, *, chunk_count=None):
    return {
        'cluster_id': cluster_id,
        'cluster_type': cluster_type,
        'topics': topics,
        'chunk_ids': chunk_ids,
        'chunk_count': chunk_count if chunk_count is not None else len(chunk_ids),
    }


def _context(case_count):
    return SimpleNamespace(case_ids=tuple(f'case_{index:04d}' for index in range(1, case_count + 1)))


def _large_capacity_clusters(topic_count):
    return [
        _cluster(
            'entity_000001',
            'entity',
            [f'entity topic {index}' for index in range(1, topic_count + 1)],
            ['entity-1', 'entity-2', 'entity-3'],
        ),
        _cluster(
            'embedding_000001',
            'embedding',
            [f'embedding topic {index}' for index in range(1, topic_count + 1)],
            ['embedding-1', 'embedding-2', 'embedding-3'],
        ),
    ]


def _inputs(*, target_case_count=6, clusters=None, chunks=None, lane_ratios=None, lane_case_counts=None):
    entity_chunks = ['entity-1', 'entity-2', 'entity-3']
    embedding_chunks = ['embedding-1', 'embedding-2', 'embedding-3']
    return {
        'source_config': {'kb_ids': ['kb-1']},
        'import_cases_manifest': {'stats': {'case_allocation': {
            'target_case_count': target_case_count,
            'import_case_count': 0,
            'auto_case_count': target_case_count,
            'assignments': {f'case_{index:04d}': {'mode': 'generated'}
                            for index in range(1, target_case_count + 1)},
        }}, 'details': []},
        'topic_discovery_manifest': {
            'clusters': clusters if clusters is not None else [
                _cluster('entity_000001', 'entity', ['entity topic 1', 'entity topic 2', 'entity topic 3'], entity_chunks),
                _cluster(
                    'embedding_000001',
                    'embedding',
                    ['embedding topic 1', 'embedding topic 2', 'embedding topic 3'],
                    embedding_chunks,
                ),
            ],
        },
        'chunk': chunks if chunks is not None else tuple(
            _chunk(chunk_id) for chunk_id in [*entity_chunks, *embedding_chunks]
        ),
        'qaplan_plan_params': {
            **({'lane_ratios': lane_ratios} if lane_ratios is not None else {}),
            **({'lane_case_counts': lane_case_counts} if lane_case_counts is not None else {}),
        },
    }


def _plan(*, target_case_count=6, runtime_case_count=None, **kwargs):
    return qaplan_plan(
        _context(runtime_case_count if runtime_case_count is not None else target_case_count),
        _inputs(target_case_count=target_case_count, **kwargs),
    )['qaplan_plan']


def test_qaplan_plan_uses_default_equal_lane_ratios_and_returns_source_and_six_ordered_items():
    payload = _plan()

    assert list(payload) == ['source', 'items', 'stats', 'params']
    assert payload['source'] == {'kb_ids': ['kb-1']}
    assert [item['lane'] for item in payload['items']] == list(LANES)
    assert [item['question_type'] for item in payload['items']] == [
        'precision', 'precision', 'precision', 'reasoning', 'reasoning', 'reasoning',
    ]
    assert [len(item['references']) for item in payload['items']] == [1, 2, 3, 1, 2, 3]
    assert payload['stats']['target_case_count'] == 6
    assert payload['stats']['planned_case_count'] == 6
    assert payload['params']['resolved_lane_quotas'] == dict.fromkeys(LANES, 1)


def test_qaplan_plan_bypasses_topic_planning_when_every_case_is_imported():
    inputs = _inputs(target_case_count=2, clusters=[])
    inputs['import_cases_manifest']['stats']['case_allocation'] = {
        'target_case_count': 2,
        'import_case_count': 2,
        'auto_case_count': 0,
        'assignments': {
            'case_0001': {'mode': 'imported', 'source_row_number': 2},
            'case_0002': {'mode': 'imported', 'source_row_number': 3},
        },
    }

    payload = qaplan_plan(_context(2), inputs)['qaplan_plan']

    assert payload['items'] == []
    assert payload['stats']['planned_case_count'] == 0
    assert payload['stats']['import_case_count'] == 2
    assert payload['stats']['lane_summaries'] == [
        {'lane': lane, 'allocated_case_count': 0, 'eligible_topic_count': 0}
        for lane in LANES
    ]
    assert payload['params']['resolved_lane_quotas'] == dict.fromkeys(LANES, 0)


def test_qaplan_plan_uses_explicit_integer_lane_case_counts_before_ratios():
    counts = {
        'entity_precision_easy': 3,
        'entity_precision_medium': 0,
        'entity_precision_hard': 0,
        'embedding_reasoning_easy': 0,
        'embedding_reasoning_medium': 0,
        'embedding_reasoning_hard': 3,
    }
    payload = _plan(
        lane_case_counts=counts,
        lane_ratios={
            'entity_precision_easy': 0,
            'entity_precision_medium': 1,
            'entity_precision_hard': 0,
            'embedding_reasoning_easy': 1,
            'embedding_reasoning_medium': 0,
            'embedding_reasoning_hard': 0,
        },
    )

    assert payload['params']['resolved_lane_quotas'] == counts
    assert [item['lane'] for item in payload['items']] == [
        'entity_precision_easy',
        'entity_precision_easy',
        'entity_precision_easy',
        'embedding_reasoning_hard',
        'embedding_reasoning_hard',
        'embedding_reasoning_hard',
    ]


def test_qaplan_plan_normalizes_ratios_and_uses_largest_remainder_with_lane_order_tie_break():
    payload = _plan(target_case_count=100, clusters=_large_capacity_clusters(20))

    assert payload['params']['resolved_lane_quotas'] == {
        'entity_precision_easy': 17,
        'entity_precision_medium': 17,
        'entity_precision_hard': 17,
        'embedding_reasoning_easy': 17,
        'embedding_reasoning_medium': 16,
        'embedding_reasoning_hard': 16,
    }

    scaled_payload = _plan(
        target_case_count=60,
        clusters=_large_capacity_clusters(10),
        lane_ratios={lane: 10 for lane in LANES},
    )
    assert scaled_payload['params']['resolved_lane_quotas'] == dict.fromkeys(LANES, 10)


def test_qaplan_plan_selects_topics_by_cluster_diversity_before_reusing_a_cluster():
    clusters = [
        _cluster('entity_a', 'entity', ['a1', 'a2'], ['a-1', 'a-2', 'a-3']),
        _cluster('entity_b', 'entity', ['b1', 'b2'], ['b-1', 'b-2', 'b-3']),
    ]
    chunks = tuple(_chunk(chunk_id) for chunk_id in ('a-1', 'a-2', 'a-3', 'b-1', 'b-2', 'b-3'))
    payload = _plan(
        target_case_count=4,
        clusters=clusters,
        chunks=chunks,
        lane_ratios={
            'entity_precision_easy': 1,
            'entity_precision_medium': 0,
            'entity_precision_hard': 0,
            'embedding_reasoning_easy': 0,
            'embedding_reasoning_medium': 0,
            'embedding_reasoning_hard': 0,
        },
    )

    assert [(item['cluster_id'], item['topic']) for item in payload['items']] == [
        ('entity_a', 'a1'),
        ('entity_b', 'b1'),
        ('entity_a', 'a2'),
        ('entity_b', 'b2'),
    ]


def test_qaplan_plan_preserves_real_kb_reference_ids_order_and_full_chunk_material():
    payload = _plan()
    hard_item = payload['items'][2]

    assert hard_item['references'] == [
        {'chunk_id': 'entity-1', 'kb_id': 'kb-1', 'doc_id': 'doc-entity-1', 'text': 'text for entity-1'},
        {'chunk_id': 'entity-2', 'kb_id': 'kb-1', 'doc_id': 'doc-entity-2', 'text': 'text for entity-2'},
        {'chunk_id': 'entity-3', 'kb_id': 'kb-1', 'doc_id': 'doc-entity-3', 'text': 'text for entity-3'},
    ]


def test_qaplan_plan_reports_lane_summary_for_user_visible_distribution():
    payload = _plan()

    assert payload['stats']['lane_summaries'] == [
        {
            'lane': lane,
            'allocated_case_count': 1,
            'eligible_topic_count': 3,
        }
        for lane in LANES
    ]


def test_qaplan_plan_reports_eligible_topics_even_when_lane_quota_is_zero():
    counts = {
        'entity_precision_easy': 1,
        'entity_precision_medium': 0,
        'entity_precision_hard': 0,
        'embedding_reasoning_easy': 0,
        'embedding_reasoning_medium': 0,
        'embedding_reasoning_hard': 0,
    }
    payload = _plan(target_case_count=1, lane_case_counts=counts)

    assert payload['stats']['lane_summaries'] == [
        {
            'lane': lane,
            'allocated_case_count': counts[lane],
            'eligible_topic_count': 3,
        }
        for lane in LANES
    ]


def test_qaplan_plan_rejects_target_case_count_that_does_not_match_runtime_partitions():
    with pytest.raises(ValueError, match='case assignments must match runtime case partitions'):
        _plan(target_case_count=6, runtime_case_count=5)


@pytest.mark.parametrize(
    ('lane_ratios', 'match'),
    [
        ({lane: 0 for lane in LANES}, 'lane_ratios.*positive'),
        ({**dict.fromkeys(LANES, 1), 'entity_precision_easy': -1}, 'lane_ratios.*non-negative'),
        ({**dict.fromkeys(LANES, 1), 'entity_precision_easy': True}, 'lane_ratios.*number'),
    ],
)
def test_qaplan_plan_rejects_invalid_lane_ratios(lane_ratios, match):
    with pytest.raises(ValueError, match=match):
        _plan(lane_ratios=lane_ratios)


@pytest.mark.parametrize(
    ('lane_case_counts', 'match'),
    [
        (
            {
                'entity_precision_easy': 1,
                'entity_precision_medium': 0,
                'entity_precision_hard': 0,
                'embedding_reasoning_easy': 0,
                'embedding_reasoning_medium': 0,
            },
            'lane_case_counts.*six lanes',
        ),
        (
            {
                **dict.fromkeys(LANES, 0),
                'entity_precision_easy': True,
            },
            'lane_case_counts.*non-negative integers',
        ),
        (
            {
                **dict.fromkeys(LANES, 0),
                'entity_precision_easy': -1,
            },
            'lane_case_counts.*non-negative integers',
        ),
        (
            {
                **dict.fromkeys(LANES, 0),
                'entity_precision_easy': 1,
            },
            'lane_case_counts.*auto_case_count',
        ),
    ],
)
def test_qaplan_plan_rejects_invalid_explicit_lane_case_counts(lane_case_counts, match):
    with pytest.raises(ValueError, match=match):
        _plan(target_case_count=2, lane_case_counts=lane_case_counts)


def test_qaplan_plan_rejects_insufficient_eligible_topics():
    with pytest.raises(ValueError, match='entity_precision_hard.*quota.*eligible topics'):
        _plan(
            target_case_count=1,
            lane_case_counts={
                'entity_precision_easy': 0,
                'entity_precision_medium': 0,
                'entity_precision_hard': 1,
                'embedding_reasoning_easy': 0,
                'embedding_reasoning_medium': 0,
                'embedding_reasoning_hard': 0,
            },
            clusters=[_cluster('entity_000001', 'entity', ['only topic'], ['entity-1', 'entity-2'])],
            chunks=(_chunk('entity-1'), _chunk('entity-2')),
        )


@pytest.mark.parametrize(
    ('clusters', 'chunks', 'match'),
    [
        (
            [
                _cluster('duplicate', 'entity', ['topic a'], ['entity-1']),
                _cluster('duplicate', 'embedding', ['topic b'], ['embedding-1']),
            ],
            (_chunk('entity-1'), _chunk('embedding-1')),
            'cluster_id.*unique',
        ),
        (
            [_cluster('entity_000001', 'entity', ['topic'], ['entity-1'], chunk_count=2)],
            (_chunk('entity-1'),),
            'chunk_count.*chunk_ids',
        ),
        (
            [_cluster('entity_000001', 'entity', ['topic'], ['entity-1'])],
            (_chunk('entity-1'), _chunk('entity-1')),
            'chunk_id.*unique',
        ),
    ],
)
def test_qaplan_plan_rejects_invalid_manifest_or_chunk_contract(clusters, chunks, match):
    with pytest.raises(ValueError, match=match):
        _plan(
            target_case_count=1,
            clusters=clusters,
            chunks=chunks,
            lane_ratios={
                'entity_precision_easy': 1,
                'entity_precision_medium': 0,
                'entity_precision_hard': 0,
                'embedding_reasoning_easy': 0,
                'embedding_reasoning_medium': 0,
                'embedding_reasoning_hard': 0,
            },
        )
