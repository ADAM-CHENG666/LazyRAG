from evo import artifacts as A
from evo.artifact_flow.projection import project_flow
from evo.artifact_runtime import ArtifactKey, ArtifactRef, RuntimeSnapshot
from evo.operations.flow import evo_flow_definition


def _ref(artifact_id: str, version: int = 1) -> tuple[ArtifactKey, ArtifactRef]:
    key = ArtifactKey.scalar(artifact_id)
    return key, ArtifactRef(key, version)


def _completed(*artifact_ids: str) -> dict[ArtifactKey, ArtifactRef]:
    return dict(_ref(artifact_id) for artifact_id in artifact_ids)


def _statuses(runtime: RuntimeSnapshot) -> dict[str, str]:
    snapshot = project_flow(evo_flow_definition(), runtime)
    return {stage.stage: stage.status for stage in snapshot.stages}


_TOPIC_GATE = (
    A.DATASET_BUILD_CHUNKS_MANIFEST,
    A.APPROVAL_DATASET_MATERIAL_PREPARATION,
    A.DATASET_TOPIC_MANIFEST,
    A.APPROVAL_DATASET_TOPIC_DISCOVERY,
)


def test_running_flow_marks_current_stage_running_before_attempts_start() -> None:
    statuses = _statuses(RuntimeSnapshot(
        run_id='thr-plan-applied',
        status='running',
        completed_artifacts=_completed(*_TOPIC_GATE),
    ))

    assert statuses['dataset.material_preparation'] == 'completed'
    assert statuses['dataset.topic_discovery'] == 'completed'
    assert statuses['dataset.case_generation'] == 'running'
    assert statuses['eval'] == 'pending'


def test_paused_flow_keeps_current_stage_paused_without_attempts() -> None:
    statuses = _statuses(RuntimeSnapshot(
        run_id='thr-quota-pause',
        status='paused',
        completed_artifacts=_completed(*_TOPIC_GATE),
    ))

    assert statuses['dataset.case_generation'] == 'paused'
    assert statuses['eval'] == 'pending'


def test_approval_gate_stays_awaiting_approval_while_flow_is_running() -> None:
    statuses = _statuses(RuntimeSnapshot(
        run_id='thr-materials-gate',
        status='running',
        completed_artifacts=_completed(A.DATASET_BUILD_CHUNKS_MANIFEST),
    ))

    assert statuses['dataset.material_preparation'] == 'awaiting_approval'
    assert statuses['dataset.topic_discovery'] == 'pending'
