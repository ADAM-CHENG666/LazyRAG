from evo.artifact_flow.state import FlowSnapshot, StageProgress
from evo.artifact_runtime import ArtifactKey, RuntimeErrorInfo, RuntimeProgress, RuntimeSnapshot
from evo.message_intent.turn import _flow_observation


def test_flow_observation_includes_unpartitioned_runtime_error() -> None:
    error = RuntimeErrorInfo(
        kind='OperationExecutionError',
        message='dataset.import_cases worker failed: grading_guidance is required',
    )
    progress = RuntimeProgress(total=1, failed=1, percentage=100.0)
    snapshot = FlowSnapshot(
        runtime=RuntimeSnapshot(
            run_id='thr-import-failed',
            status='failed',
            error=error,
            progress=progress,
        ),
        stages=(StageProgress(
            stage='dataset.material_preparation',
            result_key=ArtifactKey.scalar('dataset.build_chunks_manifest'),
            status='failed',
            progress=progress,
            error=error,
        ),),
        progress=progress,
        failures=(),
    )

    observation = _flow_observation(snapshot)

    assert observation['runtime']['error'] == {
        'kind': 'OperationExecutionError',
        'message': 'dataset.import_cases worker failed: grading_guidance is required',
    }
    assert observation['stages'][0]['error'] == observation['runtime']['error']
