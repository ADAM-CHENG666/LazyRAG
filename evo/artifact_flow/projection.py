from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from evo.artifact_runtime import (
    ArtifactKey,
    ArtifactRef,
    ArtifactRetryRequest,
    AttemptSnapshot,
    DefinitionError,
    RuntimeProgress,
    RuntimeSnapshot,
)

from .definition import FlowDefinition
from .state import FlowSnapshot, StageProgress, StageStatus


def project_flow(definition: FlowDefinition, runtime: RuntimeSnapshot, retries: Iterable[ArtifactRetryRequest] = (),
                 attempts: Iterable[AttemptSnapshot] = ()) -> FlowSnapshot:
    if not isinstance(definition, FlowDefinition):
        raise TypeError('definition must be FlowDefinition')
    if not isinstance(runtime, RuntimeSnapshot):
        raise TypeError('runtime must be RuntimeSnapshot')

    requests = tuple(retries)
    if not all(isinstance(request, ArtifactRetryRequest) for request in requests):
        raise TypeError('retries must contain ArtifactRetryRequest values')
    attempt_history = tuple(attempts)
    if not all(isinstance(attempt, AttemptSnapshot) for attempt in attempt_history):
        raise TypeError('attempts must contain AttemptSnapshot values')

    refs = tuple(
        (
            runtime.completed_artifacts.get(stage.result_key),
            None if stage.approval_key is None else runtime.completed_artifacts.get(
                stage.approval_key
            ),
        )
        for stage in definition.stages
    )
    approval_index = _approval_index(definition, runtime, refs)
    incomplete_index = _incomplete_index(definition, refs)
    active_index = _active_index(definition, runtime, requests)
    stage_progress = tuple(
        _progress(definition, index, runtime, attempt_history)
        for index in range(len(definition.stages))
    )
    failure_index = next(
        (
            index
            for index, progress in enumerate(stage_progress)
            if progress.case_total > 0 and progress.case_failed == progress.case_total
        ),
        None,
    )
    frontier = min(
        (index for index in (approval_index, incomplete_index, active_index, failure_index) if index is not None),
        default=None,
    )
    if failure_index is None and frontier is not None and refs[frontier][0] is None and runtime.status == 'failed':
        failure_index = frontier

    return FlowSnapshot(
        runtime,
        tuple(
            StageProgress(
                stage.name,
                stage.result_key,
                refs[index][0],
                stage.approval_key,
                refs[index][1],
                _stage_status(
                    index,
                    frontier,
                    active_index,
                    approval_index,
                    failure_index,
                    runtime,
                ),
                tuple(
                    operation.spec.op_id
                    for operation in definition.stage_operations(index)
                ),
                stage_progress[index],
                tuple(
                    failure
                    for failure in runtime.case_failures
                    if definition.stage_index_for_operation(failure.operation_id) == index
                ),
            )
            for index, stage in enumerate(definition.stages)
        ),
    )


def _approval_index(definition: FlowDefinition, runtime: RuntimeSnapshot,
                    refs: tuple[tuple[ArtifactRef | None, ArtifactRef | None], ...]) -> int | None:
    return next(
        (
            index
            for index, stage in enumerate(definition.stages)
            if (
                refs[index][0] is not None
                and refs[index][1] is None
                and stage.approval_key in runtime.awaiting_artifacts
            )
        ),
        None,
    )


def _incomplete_index(definition: FlowDefinition,
                      refs: tuple[tuple[ArtifactRef | None, ArtifactRef | None], ...]) -> int | None:
    missing = next(
        (
            index
            for index in range(len(definition.stages))
            if refs[index][0] is None
        ),
        None,
    )
    if missing is None or missing == 0:
        return missing
    previous = missing - 1
    if (
        definition.stages[previous].approval_key is not None
        and refs[previous][1] is None
    ):
        return previous
    return missing


def _active_index(definition: FlowDefinition, runtime: RuntimeSnapshot,
                  retries: tuple[ArtifactRetryRequest, ...]) -> int | None:
    indices = [
        _required_stage(
            definition.stage_index_for_operation(attempt.operation_id),
            f'operation {attempt.operation_id}',
        )
        for attempt in runtime.active_attempts
    ]
    indices.extend(
        _required_stage(
            definition.stage_index_for_artifact(request.artifact_key.artifact_id),
            f'artifact {request.artifact_key.artifact_id}',
        )
        for request in retries
        if request.status == 'pending'
    )
    if runtime.status == 'cancelled':
        indices.extend(
            _required_stage(
                definition.stage_index_for_artifact(request.artifact_key.artifact_id),
                f'artifact {request.artifact_key.artifact_id}',
            )
            for request in retries
            if (
                request.status == 'cancelled'
                and runtime.completed_artifacts.get(request.artifact_key) == request.base_ref
            )
        )
    return min(indices, default=None)


def _required_stage(index: int | None, subject: str) -> int:
    if index is None:
        raise DefinitionError(f'{subject} does not belong to a flow stage')
    return index


def _stage_status(index: int, frontier: int | None, active: int | None, approval: int | None, failure: int | None,
                  runtime: RuntimeSnapshot) -> StageStatus:
    if frontier is None or index < frontier:
        return 'completed'
    if index > frontier:
        return 'pending'
    if active == frontier:
        if runtime.status == 'created':
            return 'pending'
        return 'running' if runtime.status == 'completed' else runtime.status
    if runtime.status in {'cancelling', 'cancelled'}:
        return runtime.status
    if failure == frontier:
        return 'failed'
    if approval == frontier:
        return 'awaiting_approval'
    if runtime.status in {'pausing', 'paused'}:
        return runtime.status
    return 'pending'


def _progress(definition: FlowDefinition, stage_index: int, runtime: RuntimeSnapshot,
              attempts: tuple[AttemptSnapshot, ...]) -> RuntimeProgress:
    operations = definition.stage_operations(stage_index)
    operation_ids = {operation.spec.op_id for operation in operations}
    failures = {
        (failure.operation_id, failure.case_id)
        for failure in runtime.case_failures
        if failure.operation_id in operation_ids
    }
    active = {
        (attempt.operation_id, attempt.partition_key)
        for attempt in runtime.active_attempts
        if attempt.operation_id in operation_ids
    }
    latest: dict[tuple[str, str], AttemptSnapshot] = {}
    for attempt in attempts:
        if attempt.operation_id in operation_ids:
            identity = (attempt.operation_id, attempt.partition_key)
            previous = latest.get(identity)
            if previous is None or attempt.created_at > previous.created_at:
                latest[identity] = attempt

    operation_states: Counter[str] = Counter()
    case_states: dict[str, list[str]] = {}
    for operation in operations:
        if operation.spec.driver_input is None:
            partition_keys = ('',)
        else:
            partitions = runtime.partition_sets.get(ArtifactKey.scalar(operation.spec.partition_set_id))
            partition_keys = () if partitions is None else partitions.keys
        for partition_key in partition_keys:
            identity = (operation.spec.op_id, partition_key)
            output_keys = tuple(output.key_for(partition_key) for output in operation.spec.outputs.values())
            if identity in active:
                status = 'running'
            elif output_keys and all(key in runtime.completed_artifacts for key in output_keys):
                status = 'completed'
            elif identity in failures or (
                not partition_key
                and identity in latest
                and latest[identity].status == 'failed'
            ):
                status = 'failed'
            else:
                status = 'pending'
            operation_states[status] += 1
            if partition_key:
                case_states.setdefault(partition_key, []).append(status)

    cases: Counter[str] = Counter()
    for states in case_states.values():
        if 'running' in states:
            cases['running'] += 1
        elif 'failed' in states:
            cases['failed'] += 1
        elif states and all(status == 'completed' for status in states):
            cases['completed'] += 1
        else:
            cases['pending'] += 1
    total = sum(operation_states.values())
    finished = operation_states['completed'] + operation_states['failed']
    return RuntimeProgress(
        total,
        operation_states['completed'],
        operation_states['running'],
        operation_states['failed'],
        operation_states['pending'],
        0.0 if total == 0 else round(finished * 100 / total, 2),
        len(case_states),
        cases['completed'],
        cases['running'],
        cases['failed'],
        cases['pending'],
    )


__all__ = ['project_flow']
