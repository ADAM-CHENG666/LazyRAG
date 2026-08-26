from evo.artifact_flow import StageProgress
from evo.artifact_runtime import ArtifactKey, RuntimeProgress
from evo.service.projections import _display_step_status


def test_completed_checkpoint_with_failed_cases_stays_completed() -> None:
    """Partition failures belong on the progress layer, not an extended /steps status."""
    progress = StageProgress(
        'dataset.case_generation',
        ArtifactKey.scalar('dataset.assemble_manifest'),
        status='awaiting_approval',
        progress=RuntimeProgress(
            total=64,
            completed=63,
            failed=1,
            percentage=100,
            case_total=64,
            case_completed=63,
            case_failed=1,
        ),
    )

    assert _display_step_status('completed', progress) == 'completed'


def test_completed_checkpoint_without_failed_cases_remains_completed() -> None:
    progress = StageProgress(
        'dataset.case_generation',
        ArtifactKey.scalar('dataset.assemble_manifest'),
        status='awaiting_approval',
        progress=RuntimeProgress(
            total=64,
            completed=64,
            percentage=100,
            case_total=64,
            case_completed=64,
        ),
    )

    assert _display_step_status('completed', progress) == 'completed'
