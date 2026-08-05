from types import SimpleNamespace

import pytest

from evo.operations.dataset.qaplan import qaplan_plan


LANES = ('precision_easy', 'precision_medium', 'precision_hard', 'reasoning_easy', 'reasoning_medium', 'reasoning_hard')


def _ctx(count):
    return SimpleNamespace(case_ids=tuple(f'case_{index:04d}' for index in range(1, count + 1)))


def _topic(index, question_type='precision', chunks=3):
    return {'topic_id': f'topic-{index}', 'name': f'Topic {index}', 'question_type': question_type,
            'chunk_ids': [f'chunk-{index}-{part}' for part in range(chunks)], 'chunk_count': chunks}


def _inputs(count=6, *, topics=None, lane_case_counts=None):
    return {'import_cases_manifest': {'stats': {'case_allocation': {
        'target_case_count': count, 'import_case_count': 0, 'auto_case_count': count,
        'assignments': {f'case_{index:04d}': {'mode': 'generated'} for index in range(1, count + 1)},
    }}}, 'topic_discovery_manifest': {'topics': topics or [
        *[_topic(index) for index in range(1, 4)], *[_topic(index, 'reasoning') for index in range(4, 7)],
    ]}, 'qaplan_plan_params': {} if lane_case_counts is None else {'lane_case_counts': lane_case_counts}}


def test_qaplan_plan_initializes_equal_counts_with_fixed_remainder_order():
    """No submitted distribution uses quotient/remainder allocation, never lane ratios."""
    plan = qaplan_plan(_ctx(5), _inputs(5))['qaplan_plan']
    assert plan['params']['lane_case_counts'] == dict(zip(LANES, (1, 1, 1, 1, 1, 0), strict=True))
    assert [item['lane'] for item in plan['items']] == list(LANES[:5])


def test_qaplan_plan_selects_flat_topics_and_stores_only_topic_id():
    """Plan items retain only the editable selection fact, not Cluster or material snapshots."""
    counts = dict(zip(LANES, (2, 0, 0, 0, 0, 0), strict=True))
    plan = qaplan_plan(_ctx(2), _inputs(2, lane_case_counts=counts))['qaplan_plan']
    assert [item['topic_id'] for item in plan['items']] == ['topic-1', 'topic-2']
    assert {'topic', 'references', 'cluster_id', 'cluster_type', 'selection_round'}.isdisjoint(plan['items'][0])


def test_qaplan_plan_reuses_topic_only_across_different_difficulties():
    """The same Topic can serve easy and medium, but one lane cannot select it twice."""
    counts = dict(zip(LANES, (1, 1, 0, 0, 0, 0), strict=True))
    plan = qaplan_plan(_ctx(2), _inputs(2, topics=[_topic(1, chunks=2)], lane_case_counts=counts))['qaplan_plan']
    assert [item['topic_id'] for item in plan['items']] == ['topic-1', 'topic-1']


def test_qaplan_plan_rejects_ratio_and_incomplete_or_over_capacity_distribution():
    """Historical ratios and incomplete/over-capacity six-lane submissions are invalid."""
    with pytest.raises(ValueError, match='lane_ratios'):
        qaplan_plan(_ctx(1), {**_inputs(1), 'qaplan_plan_params': {'lane_ratios': {}}})
    with pytest.raises(ValueError, match='six lanes'):
        qaplan_plan(_ctx(1), _inputs(1, lane_case_counts={'precision_easy': 1}))
    counts = dict(zip(LANES, (2, 0, 0, 0, 0, 0), strict=True))
    with pytest.raises(ValueError, match='exceeds eligible'):
        qaplan_plan(_ctx(2), _inputs(2, topics=[_topic(1)], lane_case_counts=counts))
