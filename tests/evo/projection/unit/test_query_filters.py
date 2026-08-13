from __future__ import annotations

import pytest

from evo.service.contracts import ServiceError
from evo.service.projections import ProjectionService


def test_normalize_filters_makes_equivalent_query_mappings_identical() -> None:
    left = ProjectionService._normalize_filters({
        'question_type': 'precision',
        'min_chunk_count': 2,
    })
    right = ProjectionService._normalize_filters({
        'min_chunk_count': 2,
        'question_type': 'precision',
    })

    assert left == right


def test_normalize_filters_distinguishes_meaningful_query_changes() -> None:
    original = ProjectionService._normalize_filters({'question_type': 'precision'})
    changed = ProjectionService._normalize_filters({'question_type': 'reasoning'})

    assert original != changed


@pytest.mark.parametrize('filters', [
    {'question_type': ['precision']},
    {'min_chunk_count': object()},
    {1: 'precision'},
])
def test_normalize_filters_rejects_non_scalar_or_non_string_keys(filters: dict[object, object]) -> None:
    with pytest.raises(ServiceError) as error:
        ProjectionService._normalize_filters(filters)

    assert error.value.status_code == 400
