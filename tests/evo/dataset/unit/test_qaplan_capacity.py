from __future__ import annotations

from evo.operations.dataset.qaplan_capacity import (
    auto_case_count,
    default_lane_distribution_exceeds_capacity,
    eligible_lane_counts,
    project_automatic_plan,
    question_type_capacities,
    question_type_difficulties,
)


LANES = (
    'precision_easy', 'precision_medium', 'precision_hard',
    'reasoning_easy', 'reasoning_medium', 'reasoning_hard',
)


def _topic(index: int, *, question_type: str = 'precision', chunks: int = 3) -> dict[str, object]:
    return {
        'topic_id': f'topic-{index}',
        'name': f'Topic {index}',
        'question_type': question_type,
        'chunk_ids': [f'chunk-{index}-{part}' for part in range(chunks)],
        'chunk_count': chunks,
    }


def _import_manifest(*, auto: int = 20) -> dict[str, object]:
    return {
        'stats': {
            'case_allocation': {
                'target_case_count': auto,
                'import_case_count': 0,
                'auto_case_count': auto,
                'assignments': {
                    f'case_{index:04d}': {'mode': 'generated'}
                    for index in range(1, auto + 1)
                },
            },
        },
    }


def _thr_a7f9a9d6_topics() -> list[dict[str, object]]:
    topics = [_topic(index, chunks=2) for index in range(1, 138)]
    topics.append(_topic(138, chunks=3))
    topics.extend(_topic(index, question_type='reasoning', chunks=2) for index in range(139, 163))
    return topics


def test_eligible_lane_counts_requires_topic_type_and_chunk_thresholds() -> None:
    topics = [
        _topic(1, chunks=1),
        _topic(2, chunks=2),
        _topic(3, chunks=3),
        _topic(4, question_type='reasoning', chunks=2),
    ]
    eligible = eligible_lane_counts({'topics': topics})

    assert eligible == {
        'precision_easy': 3,
        'precision_medium': 2,
        'precision_hard': 1,
        'reasoning_easy': 1,
        'reasoning_medium': 1,
        'reasoning_hard': 0,
    }


def test_default_lane_distribution_exceeds_capacity_for_thr_a7f9a9d6_shape() -> None:
    assert default_lane_distribution_exceeds_capacity(
        _import_manifest(auto=20),
        {'topics': _thr_a7f9a9d6_topics()},
        {},
    )


def test_submitted_lane_distribution_within_capacity_is_not_blocked() -> None:
    counts = {
        lane: 0 for lane in LANES
    }
    counts.update({'precision_easy': 10, 'precision_medium': 5, 'precision_hard': 1, 'reasoning_easy': 4})
    assert not default_lane_distribution_exceeds_capacity(
        _import_manifest(auto=20),
        {'topics': _thr_a7f9a9d6_topics()},
        {'lane_case_counts': counts},
    )


def test_project_automatic_plan_before_qaplan_manifest_includes_capacities_and_default_difficulties() -> None:
    plan = project_automatic_plan(
        manifest=None,
        params={},
        topic_manifest={'topics': _thr_a7f9a9d6_topics()},
        import_manifest=_import_manifest(auto=20),
    )

    assert plan is not None
    assert plan['total'] == 20
    assert plan['question_types']['precision']['difficulties'] == {
        'easy': 4, 'medium': 4, 'hard': 3,
    }
    assert plan['question_types']['precision']['capacities'] == question_type_capacities(
        eligible_lane_counts({'topics': _thr_a7f9a9d6_topics()}),
    )['precision']
    assert plan['question_types']['precision']['capacities']['hard'] == 1


def test_project_automatic_plan_from_manifest_keeps_allocated_difficulties_and_adds_capacities() -> None:
    manifest = {
        'stats': {'auto_case_count': 1},
        'lane_summaries': [
            {'lane': lane, 'question_type': question_type, 'difficulty': difficulty,
             'allocated_case_count': 1 if lane == 'precision_easy' else 0,
             'eligible_topic_count': 1}
            for lane, question_type, difficulty in (
                ('precision_easy', 'precision', 'easy'),
                ('precision_medium', 'precision', 'medium'),
                ('precision_hard', 'precision', 'hard'),
                ('reasoning_easy', 'reasoning', 'easy'),
                ('reasoning_medium', 'reasoning', 'medium'),
                ('reasoning_hard', 'reasoning', 'hard'),
            )
        ],
    }
    topics = [_topic(1), _topic(2, question_type='reasoning', chunks=2)]

    plan = project_automatic_plan(
        manifest=manifest,
        params={},
        topic_manifest={'topics': topics},
        import_manifest=_import_manifest(auto=2),
    )

    assert plan == {
        'total': 1,
        'question_types': {
            'precision': {
                'total': 1,
                'difficulties': {'easy': 1, 'medium': 0, 'hard': 0},
                'capacities': question_type_difficulties({
                    lane: eligible_lane_counts({'topics': topics})[lane]
                    for lane in LANES
                })['precision'],
            },
            'reasoning': {
                'total': 0,
                'difficulties': {'easy': 0, 'medium': 0, 'hard': 0},
                'capacities': question_type_difficulties({
                    lane: eligible_lane_counts({'topics': topics})[lane]
                    for lane in LANES
                })['reasoning'],
            },
        },
    }


def test_auto_case_count_returns_zero_for_missing_manifest() -> None:
    assert auto_case_count(None) == 0
