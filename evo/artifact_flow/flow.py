from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from pathlib import Path

from evo.artifact_runtime import (
    ArtifactCommit,
    ArtifactDraft,
    ArtifactKey,
    ArtifactRecord,
    ArtifactRef,
    ArtifactRetryRequest,
    ArtifactRuntime,
    AttemptSnapshot,
    DefinitionError,
    OperationResult,
    ProgressEvent,
    RunHistory,
    RuntimeSnapshot,
)

from .definition import FlowDefinition
from .projection import project_flow
from .state import FlowCaseSnapshot, FlowRunHistory, FlowSnapshot, StageProgress, StageSnapshot


class ArtifactFlow:
    def __init__(self, runtime: ArtifactRuntime, definition: FlowDefinition) -> None:
        if not isinstance(runtime, ArtifactRuntime):
            raise TypeError('runtime must be ArtifactRuntime')
        if not isinstance(definition, FlowDefinition):
            raise TypeError('definition must be FlowDefinition')
        self._runtime = runtime
        self._definition = definition
        self._approval_keys = frozenset(
            stage.approval_key
            for stage in definition.stages
            if stage.approval_key is not None
        )

    @classmethod
    async def open(cls, root: str | Path, definition: FlowDefinition, *, max_concurrency: int = 4,
                   terminate_timeout: float = 1.0) -> ArtifactFlow:
        if not isinstance(definition, FlowDefinition):
            raise TypeError('definition must be FlowDefinition')
        runtime = await ArtifactRuntime.open(
            root,
            definition.operations,
            max_concurrency=max_concurrency,
            terminate_timeout=terminate_timeout,
        )
        return cls(runtime, definition)

    async def create(self, run_id: str, initial_commit: ArtifactCommit | None = None) -> FlowSnapshot:
        if initial_commit is not None:
            self._validate_user_commit(initial_commit)
        return await self._project(await self._runtime.create(run_id, initial_commit))

    async def start(self, run_id: str) -> FlowSnapshot:
        return await self._project(await self._runtime.start(run_id))

    async def approve(self, run_id: str, stage: str) -> FlowSnapshot:
        current = await self.snapshot(run_id)
        progress = self._approval_target(current, stage)
        if progress.approved:
            return current
        result_ref = progress.result_ref
        approval_key = progress.approval_key
        if result_ref is None or approval_key is None:
            raise RuntimeError('approval target is incomplete')

        approval = await self._runtime.head(run_id, approval_key)
        commit = ArtifactCommit(
            _approval_commit_id(progress.stage, result_ref),
            'user:approval',
            (ArtifactDraft(
                approval_key,
                {
                    'stage': progress.stage,
                    'result': {
                        'artifact_id': result_ref.key.artifact_id,
                        'version': result_ref.version,
                    },
                },
                (result_ref,),
            ),),
            {
                result_ref.key: result_ref,
                approval_key: None if approval is None else approval.ref,
            },
        )
        return await self._project(await self._runtime.commit(run_id, commit))

    async def commit(self, run_id: str, commit: ArtifactCommit) -> FlowSnapshot:
        self._validate_user_commit(commit)
        return await self._project(await self._runtime.commit(run_id, commit))

    async def rerun_artifact(self, run_id: str, artifact_key: ArtifactKey, *, request_id: str) -> FlowSnapshot:
        return await self._rerun_keys(run_id, (artifact_key,), request_id, 'artifact')

    async def rerun_stage(self, run_id: str, stage: str, *, request_id: str) -> FlowSnapshot:
        current = await self.snapshot(run_id)
        stage_index = self._definition.stage_index(stage)
        keys = self._stage_entry_keys(current, stage_index)
        if not keys:
            raise DefinitionError(f'flow stage has no effective rerun entry: {stage}')
        return await self._rerun_keys(run_id, keys, request_id, f'stage:{stage}')

    async def rerun_case(self, run_id: str, case_id: str, *, request_id: str, from_stage: str = '',
                         from_artifact: ArtifactKey | None = None) -> FlowSnapshot:
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError('case_id must be non-empty')
        if bool(from_stage.strip()) == (from_artifact is not None):
            raise DefinitionError('rerun_case requires exactly one of from_stage or from_artifact')
        if from_artifact is not None:
            if not isinstance(from_artifact, ArtifactKey) or from_artifact.partition_key != case_id:
                raise DefinitionError('from_artifact must identify the requested case')
            keys = (from_artifact,)
            namespace = f'case:{case_id}:artifact'
        else:
            current = await self.snapshot(run_id)
            keys = self._stage_entry_keys(current, self._definition.stage_index(from_stage), case_id=case_id)
            if not keys:
                raise DefinitionError(f'flow stage has no effective case rerun entry: {from_stage}[{case_id}]')
            namespace = f'case:{case_id}:stage:{from_stage}'
        return await self._rerun_keys(run_id, keys, request_id, namespace)

    async def retry_failed_case(self, run_id: str, case_id: str, *, request_id: str) -> FlowSnapshot:
        child_id = f'flow-case-retry:{_request_id(request_id)}:{case_id}'
        history = await self._runtime.run_history(run_id)
        if any(record.producer == f'runtime:retry:{child_id}' for record in history.artifacts):
            return await self._project(history.snapshot, history.attempts, history.retry_requests)
        return await self._project(await self._runtime.retry_case(run_id, case_id, request_id=child_id))

    async def comment_case(self, run_id: str, case_id: str, *, intervention_id: str, target_key: ArtifactKey,
                           message: str, target_ref: ArtifactRef | None = None, field: str = '', quote: str = '',
                           start: int | None = None, end: int | None = None) -> FlowSnapshot:
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError('case_id must be non-empty')
        if not isinstance(target_key, ArtifactKey) or target_key.partition_key != case_id:
            raise DefinitionError('comment target must identify the requested case')
        return await self._project(await self._runtime.submit_intervention(
            run_id,
            intervention_id=intervention_id,
            target_key=target_key,
            target_ref=target_ref,
            message=message,
            field=field,
            quote=quote,
            start=start,
            end=end,
        ))

    async def pause(self, run_id: str) -> FlowSnapshot:
        return await self._project(await self._runtime.pause(run_id))

    async def resume(self, run_id: str) -> FlowSnapshot:
        return await self._project(await self._runtime.resume(run_id))

    async def retry_stage(self, run_id: str, *, stage: str = '', request_id: str = '') -> FlowSnapshot:
        command_id = _request_id(request_id)
        history = await self._runtime.run_history(run_id)
        current = project_flow(self._definition, history.snapshot, history.retry_requests, history.attempts)
        stage_prefix = f'flow-stage-retry:{command_id}:'
        case_prefix = f'runtime:retry:flow-case-retry:{command_id}:'
        if (
            any(request.request_id.startswith(stage_prefix) for request in history.retry_requests)
            or any(record.producer.startswith(case_prefix) for record in history.artifacts)
        ):
            return current
        if current.status != 'failed':
            raise DefinitionError(f'cannot retry flow from {current.status}')

        target_index = self._definition.stage_index(current.current_stage if not stage.strip() else stage)
        target = current.stages[target_index]
        if target.failures:
            result = current
            for case_id in dict.fromkeys(failure.case_id for failure in target.failures):
                result = await self.retry_failed_case(run_id, case_id, request_id=request_id)
            return result
        if current.runtime.status != 'failed':
            raise DefinitionError(f'flow stage is not retryable: {target.stage}')
        keys = self._stage_entry_keys(current, target_index)
        retry_id = f'{stage_prefix}{target.stage}'
        return await self._project(await self._runtime.retry(run_id, keys, request_id=retry_id))

    async def cancel(self, run_id: str) -> FlowSnapshot:
        return await self._project(await self._runtime.cancel(run_id))

    async def wait_until_boundary(self, run_id: str, *, timeout: float = 10.0) -> FlowSnapshot:
        snapshot = await self._runtime.wait_until_settled(run_id, timeout=timeout)
        return await self._project(snapshot)

    async def snapshot(self, run_id: str) -> FlowSnapshot:
        return await self._project(await self._runtime.snapshot(run_id))

    async def stage_snapshot(self, run_id: str, stage: str) -> StageSnapshot:
        history = await self.run_history(run_id)
        stage_index = self._definition.stage_index(stage)
        return history.stages[stage_index]

    async def case_snapshot(self, run_id: str, case_id: str) -> FlowCaseSnapshot:
        case = await self._runtime.case_snapshot(run_id, case_id)
        indices = tuple(dict.fromkeys(
            index
            for operation in case.operations
            if (index := self._definition.stage_index_for_operation(operation.operation_id)) is not None
        ))
        stages = tuple(self._definition.stages[index].name for index in indices)
        active = next(
            (
                self._definition.stages[index].name
                for operation in case.operations
                if operation.status != 'succeeded'
                if (index := self._definition.stage_index_for_operation(operation.operation_id)) is not None
            ),
            stages[-1] if stages else (await self.snapshot(run_id)).current_stage,
        )
        return FlowCaseSnapshot(case, stages, active)

    async def run_history(self, run_id: str) -> FlowRunHistory:
        history = await self._runtime.run_history(run_id)
        snapshot = project_flow(
            self._definition,
            history.snapshot,
            history.retry_requests,
            history.attempts,
        )
        return FlowRunHistory(
            snapshot,
            history,
            tuple(
                _stage_snapshot(self._definition, index, snapshot.stages[index], history)
                for index in range(len(self._definition.stages))
            ),
        )

    async def submit_external_result(self, run_id: str, attempt_id: str, result: OperationResult) -> FlowSnapshot:
        return await self._project(await self._runtime.submit_attempt_result(run_id, attempt_id, result))

    async def read(self, run_id: str, ref: ArtifactRef) -> object:
        return await self._runtime.read(run_id, ref)

    async def read_many(self, run_id: str, refs: Iterable[ArtifactRef]) -> Mapping[ArtifactRef, object]:
        return await self._runtime.read_many(run_id, refs)

    async def record(self, run_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        return await self._runtime.record(run_id, ref)

    async def head(self, run_id: str, key: ArtifactKey) -> ArtifactRecord | None:
        return await self._runtime.head(run_id, key)

    async def history(self, run_id: str, key: ArtifactKey) -> tuple[ArtifactRecord, ...]:
        return await self._runtime.history(run_id, key)

    async def attempts(self, run_id: str) -> tuple[AttemptSnapshot, ...]:
        return await self._runtime.attempts(run_id)

    async def progress_events(self, run_id: str, attempt_id: str | None = None) -> tuple[ProgressEvent, ...]:
        return await self._runtime.progress_events(run_id, attempt_id)

    async def retry_requests(self, run_id: str) -> tuple[ArtifactRetryRequest, ...]:
        return await self._runtime.retry_requests(run_id)

    async def run_ids(self) -> tuple[str, ...]:
        return await self._runtime.run_ids()

    async def has_run(self, run_id: str) -> bool:
        return await self._runtime.has_run(run_id)

    async def release(self, run_id: str) -> None:
        await self._runtime.release(run_id)

    async def delete_run(self, run_id: str) -> None:
        await self._runtime.delete_run(run_id)

    async def close(self) -> None:
        await self._runtime.close()

    async def _project(self, runtime: RuntimeSnapshot, attempts: tuple[AttemptSnapshot, ...] | None = None,
                       retries: tuple[ArtifactRetryRequest, ...] | None = None) -> FlowSnapshot:
        if attempts is None or retries is None:
            attempts, retries = await asyncio.gather(
                self._runtime.attempts(runtime.run_id),
                self._runtime.retry_requests(runtime.run_id),
            )
        return project_flow(self._definition, runtime, retries, attempts)

    def _validate_user_commit(self, commit: ArtifactCommit) -> None:
        if not isinstance(commit, ArtifactCommit):
            raise TypeError('commit must be ArtifactCommit')
        forbidden = sorted(
            (write.key for write in commit.writes if write.key in self._approval_keys),
            key=lambda key: (key.artifact_id, key.partition_key),
        )
        if forbidden:
            names = ', '.join(key.artifact_id for key in forbidden)
            raise DefinitionError(f'approval artifacts require approve(): {names}')

    def _approval_target(self, snapshot: FlowSnapshot, stage: str) -> StageProgress:
        target = snapshot.stages[self._definition.stage_index(stage)]
        if target.approval_key is None:
            raise DefinitionError(f'flow stage does not require approval: {stage}')
        if not target.has_result:
            raise DefinitionError(f'flow stage is not complete: {stage}')
        if target.approved:
            return target
        pending = snapshot.pending_approval
        if pending is None or pending.stage != stage:
            raise DefinitionError(f'flow is not awaiting approval for: {stage}')
        return target

    async def _rerun_keys(self, run_id: str, keys: tuple[ArtifactKey, ...], request_id: str,
                          namespace: str) -> FlowSnapshot:
        command_id = _request_id(request_id)
        retry_id = f'flow-rerun:{namespace}:{command_id}'
        requests = await self._runtime.retry_requests(run_id)
        if any(request.request_id.startswith(f'{retry_id}:') for request in requests):
            return await self.snapshot(run_id)
        return await self._project(await self._runtime.rerun_artifacts(run_id, keys, request_id=retry_id))

    def _stage_entry_keys(self, snapshot: FlowSnapshot, stage_index: int, *,
                          case_id: str = '') -> tuple[ArtifactKey, ...]:
        completed = snapshot.runtime.completed_artifacts
        keys: list[ArtifactKey] = []
        for operation in self._definition.stage_entry_operations(stage_index):
            output_ids = {
                output.artifact_id
                for output in operation.spec.outputs.values()
            }
            by_invocation: dict[str, ArtifactKey] = {}
            for key in sorted(
                completed,
                key=lambda item: (item.artifact_id, item.partition_key),
            ):
                if key.artifact_id not in output_ids:
                    continue
                if case_id and key.partition_key != case_id:
                    continue
                invocation = (
                    key.partition_key
                    if operation.spec.driver_input is not None
                    else ''
                )
                by_invocation.setdefault(invocation, key)
            keys.extend(by_invocation.values())
        return tuple(keys)

    retry_artifact = rerun_artifact
    retry = retry_stage
    submit_intervention = comment_case
    submit_attempt_result = submit_external_result


def _approval_commit_id(stage: str, result_ref: ArtifactRef) -> str:
    return f'approval:{stage}:{result_ref.key.artifact_id}:{result_ref.version}'


def _request_id(request_id: str) -> str:
    if not isinstance(request_id, str) or not request_id.strip():
        raise DefinitionError('request_id must be non-empty')
    return request_id.strip()


def _stage_snapshot(definition: FlowDefinition, stage_index: int, progress: StageProgress,
                    history: RunHistory) -> StageSnapshot:
    stage = definition.stages[stage_index]
    operation_ids = set(progress.operation_ids)
    attempts = tuple(attempt for attempt in history.attempts if attempt.operation_id in operation_ids)
    attempt_ids = {attempt.attempt_id for attempt in attempts}
    artifacts = tuple(
        record
        for record in history.artifacts
        if (
            definition.stage_index_for_artifact(record.ref.key.artifact_id) == stage_index
            or record.ref.key in {stage.result_key, stage.approval_key}
        )
    )
    return StageSnapshot(
        progress,
        tuple(operation for operation in history.operations if operation.operation_id in operation_ids),
        attempts,
        artifacts,
        tuple(event for event in history.progress_events if event.attempt_id in attempt_ids),
        tuple(
            request
            for request in history.retry_requests
            if definition.stage_index_for_artifact(request.artifact_key.artifact_id) == stage_index
        ),
        tuple(record for record in history.artifacts if record.ref.key == stage.result_key),
        () if stage.approval_key is None else tuple(
            record for record in history.artifacts if record.ref.key == stage.approval_key
        ),
    )


__all__ = ['ArtifactFlow']
