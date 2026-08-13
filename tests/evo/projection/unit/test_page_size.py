from __future__ import annotations

import pytest

from evo.service.contracts import ServiceError
from evo.service.projections import ProjectionService


def test_page_size_defaults_to_50() -> None:
    assert ProjectionService._validate_page_size(None) == 50


@pytest.mark.parametrize('value', [1, 50, 200])
def test_page_size_accepts_the_documented_inclusive_range(value: int) -> None:
    assert ProjectionService._validate_page_size(value) == value


@pytest.mark.parametrize('value', [0, 201, -1, True, '50'])
def test_page_size_rejects_values_outside_the_documented_range(value: object) -> None:
    with pytest.raises(ServiceError) as error:
        ProjectionService._validate_page_size(value)

    assert error.value.status_code == 400
