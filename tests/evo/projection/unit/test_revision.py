from __future__ import annotations

import pytest

from evo.artifact_runtime import ArtifactKey, ArtifactRef
from evo.service.contracts import ServiceError
from evo.service.projections import ProjectionService


def _refs(*versions: int) -> tuple[ArtifactRef, ...]:
    return tuple(
        ArtifactRef(ArtifactKey.scalar(f'dataset.source_{index}'), version)
        for index, version in enumerate(versions, start=1)
    )


def test_revision_is_stable_for_the_same_complete_artifact_ref_set() -> None:
    assert ProjectionService._build_revision(_refs(7, 20, 3)) == ProjectionService._build_revision(_refs(7, 20, 3))


def test_revision_changes_when_any_source_artifact_version_changes() -> None:
    assert ProjectionService._build_revision(_refs(7, 20, 3)) != ProjectionService._build_revision(_refs(7, 21, 3))


def test_revision_resolves_to_the_original_complete_artifact_ref_set() -> None:
    refs = _refs(7, 20, 3)

    assert ProjectionService._resolve_revision(ProjectionService._build_revision(refs)) == refs


@pytest.mark.parametrize('revision', ['', 'not-a-revision', 'revision-of-another-format'])
def test_revision_rejects_malformed_values(revision: str) -> None:
    with pytest.raises(ServiceError) as error:
        ProjectionService._resolve_revision(revision)

    assert error.value.status_code == 400
