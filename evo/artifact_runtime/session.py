from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from typing import Literal, get_args
from uuid import uuid4

from .artifact import ArtifactCommit, ArtifactSnapshot
from .errors import DefinitionError, OperationExecutionError, PlanningError
from .execution import ExecutionHandle, start_execution
from .operation import OperationContext, OperationInvocation, OperationResult
from .planning import PlanningDecision, RuntimeDefinition, plan_next
from .state import (
    AttemptSnapshot,
    InvocationSnapshot,
    ProgressUpdate,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeSnapshot,
)
from .store import ArtifactStore, CommitResult
from .utils import _as_exception, _positive_int, _positive_number, _text


_RUN_STATUSES = frozenset(get_args(RunStatus))


class _SessionFailure(ExceptionGroup):
    """An internal run failure that must enter the actor cleanup path."""


class _TerminationFailure(ExceptionGroup):
    """A retryable failure to terminate one or more physical executions."""


@dataclass(frozen=True, slots=True)
class _Command:
    kind: Literal['start', 'pause', 'resume', 'retry', 'cancel', 'release', 'close']
    reply: asyncio.Future[RuntimeSnapshot]


@dataclass(frozen=True, slots=True)
class _CommitCommand:
    commit: ArtifactCommit
    reply: asyncio.Future[RuntimeSnapshot]


@dataclass(frozen=True, slots=True)
class _ExecutionProgress:
    attempt_id: str
    update: ProgressUpdate


@dataclass(frozen=True, slots=True)
class _ExecutionDone:
    attempt_id: str
    outcome: OperationResult | Exception


@dataclass(slots=True)
class _ActiveExecution:
    invocation: OperationInvocation
    attempt: AttemptSnapshot
    handle: ExecutionHandle
    task: asyncio.Task[None]


class RunSession:
    def __init__(self, run_id: str, definition: RuntimeDefinition, store: ArtifactStore, *,
                 max_concurrency: int = 4, terminate_timeout: float = 1.0
                 ) -> None:
        _text(run_id, 'run_id')
        if not isinstance(definition, RuntimeDefinition):
            raise TypeError('definition must be RuntimeDefinition')
        if not isinstance(store, ArtifactStore):
            raise TypeError('store must be ArtifactStore')
        _positive_int(max_concurrency, 'max_concurrency')
        _positive_number(terminate_timeout, 'terminate_timeout')

        self.run_id = run_id
        self._definition = definition
        self._store = store
        self._max_concurrency = max_concurrency
        self._terminate_timeout = terminate_timeout

        self._events: asyncio.Queue[
            _Command | _CommitCommand | _ExecutionProgress | _ExecutionDone
        ] = asyncio.Queue()
        self._condition = asyncio.Condition()
        self._request_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._stopped = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._task_group: asyncio.TaskGroup | None = None

        self._pending_invocations: dict[str, deque[OperationInvocation]] = {}
        self._active_attempts: dict[str, _ActiveExecution] = {}
        self._remaining_planned: dict[str, int] = {}

        self._status: RunStatus = 'created'
        self._error: RuntimeErrorInfo | None = None
        self._pending_failure: RuntimeErrorInfo | None = None
        self._accept_commands = True
        self._closing = False
        self._artifacts = ArtifactSnapshot()
        self._view = ArtifactSnapshot()
        self._snapshot = RuntimeSnapshot(run_id)

    # Actor lifecycle and public commands

    async def serve(self) -> None:
        try:
            async with asyncio.TaskGroup() as group:
                self._task_group = group
                await self._initialize()
                self._ready.set()
                while not self._closing:
                    event = await self._events.get()
                    try:
                        match event:
                            case _Command():
                                await self._command(event)
                            case _CommitCommand():
                                await self._commit_command(event)
                            case _ExecutionProgress():
                                await self._execution_progress(event)
                            case _ExecutionDone():
                                await self._execution_done(event)
                    except Exception as exc:
                        failure = await self._handle_session_failure(exc)
                        if (
                            isinstance(event, (_Command, _CommitCommand))
                            and not event.reply.done()
                        ):
                            event.reply.set_exception(
                                exc if failure is None else failure
                            )
                        if failure is not None:
                            raise failure
        except BaseException as exc:
            if not self._ready.is_set():
                self._startup_error = exc
            self._ready.set()
            failure = await self._handle_session_failure(exc)
            if failure is None:
                return
            if failure is exc:
                raise
            raise failure
        finally:
            await self._finish_pending_commands()
            self._stopped.set()
            async with self._condition:
                self._condition.notify_all()

    async def wait_ready(self) -> None:
        await self._ready.wait()
        if self._startup_error is not None:
            raise RuntimeError('run session failed to start') from self._startup_error

    async def start(self) -> RuntimeSnapshot:
        return await self._request('start')

    async def pause(self) -> RuntimeSnapshot:
        return await self._request('pause')

    async def resume(self) -> RuntimeSnapshot:
        return await self._request('resume')

    async def retry(self) -> RuntimeSnapshot:
        return await self._request('retry')

    async def cancel(self) -> RuntimeSnapshot:
        if self._snapshot.status == 'cancelled' or (
            self._snapshot.status == 'failed'
            and not self._snapshot.active_attempts
        ):
            return self._snapshot
        return await self._request('cancel')

    async def close(self) -> RuntimeSnapshot:
        if self._stopped.is_set():
            return self._snapshot
        return await self._request('close')

    async def release(self) -> RuntimeSnapshot:
        return await self._request('release')

    async def commit(self, commit: ArtifactCommit) -> RuntimeSnapshot:
        if not isinstance(commit, ArtifactCommit):
            raise TypeError('commit must be ArtifactCommit')
        await self.wait_ready()
        async with self._request_lock:
            if not self._accept_commands or self._stopped.is_set():
                raise RuntimeError('run session is closed')
            reply = asyncio.get_running_loop().create_future()
            self._events.put_nowait(_CommitCommand(commit, reply))
        return await reply

    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot

    async def wait_for_status(self, statuses: str | tuple[str, ...], *, timeout: float = 10.0
                              ) -> RuntimeSnapshot:
        _positive_number(timeout, 'timeout')
        requested = (statuses,) if isinstance(statuses, str) else tuple(statuses)
        if not requested or any(status not in _RUN_STATUSES for status in requested):
            raise DefinitionError('statuses must contain valid run status values')
        expected = frozenset(requested)
        async with asyncio.timeout(timeout):
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self._snapshot.status in expected or self._stopped.is_set()
                )
                if self._snapshot.status not in expected:
                    raise RuntimeError('run session stopped before reaching requested status')
                return self._snapshot

    async def _request(self, kind: Literal['start', 'pause', 'resume', 'retry', 'cancel', 'release', 'close']
                       ) -> RuntimeSnapshot:
        await self.wait_ready()
        async with self._request_lock:
            if not self._accept_commands or self._stopped.is_set():
                if kind == 'cancel' and self._snapshot.status == 'cancelled':
                    return self._snapshot
                if kind in {'release', 'close'}:
                    return self._snapshot
                raise RuntimeError('run session is closed')
            reply = asyncio.get_running_loop().create_future()
            self._events.put_nowait(_Command(kind, reply))
        return await reply

    # Recovery and serialized command handling

    async def _initialize(self) -> None:
        stored = await self._store.run_state(self.run_id)
        if stored is None:
            stored = await self._store.create_run(self.run_id)

        recovery = 'cancelled' if stored.status in {'cancelling', 'cancelled'} else 'interrupted'
        await self._store.recover_attempts(self.run_id, recovery)

        status = stored.status
        error = stored.error
        if status == 'cancelling':
            await self._store.set_run_state(self.run_id, 'cancelled')
            status = 'cancelled'
            error = None
        elif status in {'running', 'pausing'}:
            await self._store.set_run_state(self.run_id, 'paused')
            status = 'paused'
            error = None

        self._status = status
        self._error = error
        self._artifacts = await self._store.snapshot(
            self.run_id,
            self._definition.partition_set_ids,
        )
        decision = self._decide_current_state()
        if status == 'completed' and not decision.complete:
            await self._store.set_run_state(self.run_id, 'paused')
            self._status = 'paused'
        await self._publish(decision.view)
        if self._status == 'cancelled':
            self._closing = True

    async def _command(self, command: _Command) -> None:
        try:
            if (
                self._pending_failure is not None
                and command.kind not in {'cancel', 'close'}
            ):
                raise RuntimeError('run session is settling a failure')
            match command.kind:
                case 'start':
                    await self._start()
                case 'pause':
                    await self._pause()
                case 'resume':
                    await self._resume()
                case 'retry':
                    await self._retry()
                case 'cancel':
                    await self._cancel()
                case 'release':
                    await self._release()
                case 'close':
                    await self._close()
            if not command.reply.done():
                command.reply.set_result(self._snapshot)
        except _SessionFailure:
            raise
        except Exception as exc:
            if not command.reply.done():
                command.reply.set_exception(exc)

    async def _commit_command(self, command: _CommitCommand) -> None:
        try:
            await self._commit_artifacts(command.commit)
            if not command.reply.done():
                command.reply.set_result(self._snapshot)
        except _SessionFailure:
            raise
        except Exception as exc:
            if not command.reply.done():
                command.reply.set_exception(exc)

    # Run state machine

    async def _start(self) -> None:
        if self._status != 'created':
            raise DefinitionError(f'cannot start run from {self._status}')
        await self._enter_running()

    async def _pause(self) -> None:
        if self._status == 'running':
            await self._transition('pausing')
        elif self._status != 'pausing':
            raise DefinitionError(f'cannot pause run from {self._status}')
        await self._cancel_invocations()
        await self._transition('paused')

    async def _resume(self) -> None:
        if self._status != 'paused':
            raise DefinitionError(f'cannot resume run from {self._status}')
        await self._enter_running()

    async def _retry(self) -> None:
        if self._status != 'failed':
            raise DefinitionError(f'cannot retry run from {self._status}')
        if self._pending_failure is not None or self._active_attempts:
            raise RuntimeError('cannot retry run while failure cleanup is incomplete')
        await self._enter_running()

    async def _enter_running(self, decision: PlanningDecision | None = None) -> None:
        if decision is None:
            self._artifacts = await self._store.snapshot(
                self.run_id,
                self._definition.partition_set_ids,
            )
            decision = self._decide_current_state()
        await self._persist_status('running')
        await self._schedule(decision)

    async def _cancel(self) -> None:
        if self._pending_failure is not None:
            await self._finish_pending_failure()
            return
        if self._status == 'cancelled':
            return
        if self._status == 'failed':
            await self._cancel_invocations()
            self._closing = not self._active_attempts
            await self._publish()
            return
        if self._status == 'completed':
            raise DefinitionError(f'cannot cancel run from {self._status}')
        if self._status != 'cancelling':
            await self._transition('cancelling')
        await self._cancel_invocations()
        await self._transition('cancelled')

    async def _close(self) -> None:
        if self._pending_failure is not None:
            await self._finish_pending_failure()
            return
        if self._status in {'running', 'pausing'}:
            if self._status == 'running':
                await self._transition('pausing')
            await self._cancel_invocations()
            await self._transition('paused')
        elif self._status == 'cancelling':
            await self._cancel_invocations()
            await self._transition('cancelled')
        else:
            await self._cancel_invocations()
        self._closing = True

    async def _release(self) -> None:
        if self._status in {'running', 'pausing', 'cancelling'} or self._active_attempts:
            raise RuntimeError('cannot release a run while it is executing')
        self._closing = True

    async def _commit_artifacts(self, commit: ArtifactCommit) -> None:
        if self._pending_failure is not None:
            raise RuntimeError('run session is settling a failure')
        self._definition.validate_commit(commit)
        previous_status = self._status
        if previous_status not in {'created', 'paused', 'completed', 'running'}:
            raise DefinitionError(f'cannot commit artifact from {previous_status}')

        committed = await self._store.commit(self.run_id, commit)
        if committed.status == 'stale':
            raise DefinitionError('artifact commit precondition is stale')
        await self._apply_commit(committed)

        decision = self._decide_current_state()
        try:
            await self._cancel_invalidated_invocations(decision.view)
        except _TerminationFailure:
            await self._publish(decision.view)
            return
        if previous_status == 'completed':
            if decision.complete:
                await self._publish(decision.view)
            else:
                await self._enter_running(decision)
        elif previous_status == 'running':
            await self._schedule(decision)
        else:
            await self._publish(decision.view)

    # Planning and bounded scheduling

    def _decide_current_state(self) -> PlanningDecision:
        decision = plan_next(self._definition, self._artifacts)
        self._view = decision.view
        return decision

    async def _refresh_decision(self) -> PlanningDecision:
        self._artifacts = await self._store.snapshot(
            self.run_id,
            self._definition.partition_set_ids,
        )
        return self._decide_current_state()

    async def _apply_commit(self, result: CommitResult) -> None:
        if result.status != 'ok':
            return
        if result.replayed:
            self._artifacts = await self._store.snapshot(
                self.run_id,
                self._definition.partition_set_ids,
            )
        else:
            self._artifacts = result.changes.apply(self._artifacts)

    async def _schedule(self, decision: PlanningDecision | None = None) -> None:
        try:
            await self._plan_execution(
                self._decide_current_state() if decision is None else decision
            )
        except Exception as exc:
            await self._fail(exc)

    async def _plan_execution(self, decision: PlanningDecision) -> None:
        known = {
            execution.invocation.invocation_id
            for execution in self._active_attempts.values()
        }
        known.update(
            invocation.invocation_id
            for queue in self._pending_invocations.values()
            for invocation in queue
        )
        active_by_operation = Counter(
            execution.invocation.operation.spec.op_id
            for execution in self._active_attempts.values()
        )
        pending_by_operation = {
            op_id: len(queue)
            for op_id, queue in self._pending_invocations.items()
        }

        self._remaining_planned.clear()
        for invocation in decision.invocations:
            if invocation.invocation_id in known:
                continue
            op_id = invocation.operation.spec.op_id
            window = invocation.operation.spec.max_concurrency + 1
            if active_by_operation[op_id] + pending_by_operation.get(op_id, 0) >= window:
                self._remaining_planned[op_id] = self._remaining_planned.get(op_id, 0) + 1
                continue
            self._pending_invocations.setdefault(op_id, deque()).append(invocation)
            pending_by_operation[op_id] = pending_by_operation.get(op_id, 0) + 1
            known.add(invocation.invocation_id)

        await self._start_ready_invocations()
        if self._active_attempts or self._pending_invocations:
            await self._publish(decision.view)
        elif decision.complete:
            await self._transition('completed', decision.view)
        elif decision.blocked_reason:
            await self._publish(decision.view)
            raise PlanningError(decision.blocked_reason)
        else:
            await self._publish(decision.view)

    async def _start_ready_invocations(self) -> None:
        active_by_operation = Counter(
            execution.invocation.operation.spec.op_id
            for execution in self._active_attempts.values()
        )
        for op_id in tuple(self._pending_invocations):
            queue = self._pending_invocations.pop(op_id)
            while queue and len(self._active_attempts) < self._max_concurrency:
                invocation = queue[0]
                if active_by_operation[op_id] >= invocation.operation.spec.max_concurrency:
                    break
                queue.popleft()
                await self._launch_invocation(invocation)
                active_by_operation[op_id] += 1

            if queue:
                self._pending_invocations[op_id] = queue
            if len(self._active_attempts) >= self._max_concurrency:
                break

    async def _launch_invocation(self, invocation: OperationInvocation) -> None:
        if self._task_group is None:
            raise RuntimeError('run session task group is not active')

        values = await self._store.read_many(self.run_id, invocation.value_refs())
        inputs = invocation.bind_values(values)
        attempt_id = uuid4().hex

        async def report(update: ProgressUpdate) -> None:
            await self._events.put(_ExecutionProgress(attempt_id, update))

        context = OperationContext(
            self.run_id,
            invocation.invocation_id,
            invocation.partition_key,
            report,
        )
        attempt = await self._store.create_attempt(
            self.run_id,
            attempt_id,
            invocation.invocation_id,
            invocation.operation.spec.op_id,
            invocation.partition_key,
            invocation.lineage_refs(),
            (key for key in invocation.output_keys.values() if key is not None),
        )
        try:
            attempt = await self._store.set_attempt_status(
                self.run_id,
                attempt_id,
                'running',
            )
        except BaseException as exc:
            try:
                await self._store.set_attempt_status(
                    self.run_id,
                    attempt_id,
                    'interrupted',
                )
            except Exception as cleanup_error:
                raise BaseExceptionGroup(
                    'attempt failed to enter running state',
                    (exc, cleanup_error),
                ) from exc
            raise
        try:
            handle = await start_execution(
                invocation,
                context,
                inputs,
                terminate_timeout=self._terminate_timeout,
            )
        except BaseException as exc:
            try:
                if isinstance(exc, Exception):
                    await self._store.set_attempt_status(
                        self.run_id,
                        attempt_id,
                        'failed',
                        error=_error_info(exc),
                    )
                else:
                    await self._store.set_attempt_status(
                        self.run_id,
                        attempt_id,
                        'interrupted',
                    )
            except Exception as persistence_error:
                failures: list[BaseException] = [exc, persistence_error]
                try:
                    await self._store.set_attempt_status(
                        self.run_id,
                        attempt_id,
                        'interrupted',
                    )
                except Exception as cleanup_error:
                    failures.append(cleanup_error)
                raise BaseExceptionGroup(
                    'execution failed to start and its attempt could not be finalized',
                    failures,
                ) from exc
            raise

        task = self._task_group.create_task(
            self._wait_execution(attempt_id, invocation.operation.spec.op_id, handle),
            name=attempt_id,
        )
        self._active_attempts[attempt_id] = _ActiveExecution(
            invocation,
            attempt,
            handle,
            task,
        )

    # Attempt completion, progress and cancellation

    async def _wait_execution(self, attempt_id: str, operation_id: str, handle: ExecutionHandle) -> None:
        try:
            outcome: OperationResult | Exception = await handle.wait()
        except asyncio.CancelledError:
            outcome = OperationExecutionError(f'{operation_id} execution was cancelled')
        except Exception as exc:
            outcome = exc
        await self._events.put(_ExecutionDone(attempt_id, outcome))

    async def _execution_progress(self, event: _ExecutionProgress) -> None:
        execution = self._active_attempts.get(event.attempt_id)
        if execution is not None and execution.attempt.status == 'running':
            await self._store.append_progress(self.run_id, event.attempt_id, event.update)

    async def _execution_done(self, event: _ExecutionDone) -> None:
        execution = self._active_attempts.get(event.attempt_id)
        if execution is None:
            return
        if (
            self._pending_failure is not None
            or execution.attempt.status == 'cancelling'
            or self._status != 'running'
        ):
            await self._settle_cancelled_execution(execution)
            await self._continue_after_cancellation()
            return

        if isinstance(event.outcome, Exception):
            try:
                await self._store.set_attempt_status(
                    self.run_id,
                    event.attempt_id,
                    'failed',
                    error=_error_info(event.outcome),
                )
            except Exception as persistence_error:
                raise ExceptionGroup(
                    'operation failed and its attempt could not be finalized',
                    (event.outcome, persistence_error),
                ) from event.outcome
            self._remove_active(execution)
            await self._fail(event.outcome)
            return

        invocation = execution.invocation
        try:
            commit = invocation.artifact_commit(event.outcome)
            self._definition.validate_commit(commit)
            committed = await self._store.commit(
                self.run_id,
                commit,
                attempt_id=event.attempt_id,
            )
        except Exception as exc:
            try:
                await self._store.set_attempt_status(
                    self.run_id,
                    event.attempt_id,
                    'failed',
                    error=_error_info(exc),
                )
            except Exception as persistence_error:
                raise ExceptionGroup(
                    'operation result failed and its attempt could not be finalized',
                    (exc, persistence_error),
                ) from exc
            self._remove_active(execution)
            await self._fail(exc)
            return

        self._remove_active(execution)
        if committed.status == 'stale':
            decision = await self._refresh_decision()
            if any(
                item.invocation_id == invocation.invocation_id
                for item in decision.invocations
            ):
                await self._fail(PlanningError(
                    f'{invocation.invocation_id} produced no committable artifact'
                ))
                return
        else:
            await self._apply_commit(committed)
            decision = None
        await self._schedule(decision)

    async def _cancel_invalidated_invocations(self, view: ArtifactSnapshot) -> None:
        for op_id, queue in tuple(self._pending_invocations.items()):
            current = deque(
                invocation
                for invocation in queue
                if invocation.is_current(
                    self._artifacts.records,
                    view.records,
                    view.partition_sets,
                )
            )
            if current:
                self._pending_invocations[op_id] = current
            else:
                self._pending_invocations.pop(op_id, None)

        invalid = tuple(
            execution
            for execution in self._active_attempts.values()
            if not execution.invocation.is_current(
                self._artifacts.records,
                view.records,
                view.partition_sets,
            )
        )
        if invalid:
            await self._cancel_executions(invalid)

    async def _cancel_invocations(self) -> None:
        self._pending_invocations.clear()
        self._remaining_planned.clear()
        if self._active_attempts:
            await self._cancel_executions(tuple(self._active_attempts.values()))

    async def _cancel_executions(self, executions: tuple[_ActiveExecution, ...]) -> None:
        targets = tuple(
            execution
            for execution in executions
            if self._active_attempts.get(execution.attempt.attempt_id) is execution
        )
        if not targets:
            return

        marking_errors: dict[str, Exception] = {}
        for execution in targets:
            if execution.attempt.status == 'cancelling':
                continue
            try:
                execution.attempt = await self._store.set_attempt_status(
                    self.run_id,
                    execution.attempt.attempt_id,
                    'cancelling',
                )
            except Exception as exc:
                marking_errors[execution.attempt.attempt_id] = exc
        await self._publish()

        termination_results = await asyncio.gather(
            *(execution.handle.terminate() for execution in targets),
            return_exceptions=True,
        )
        failures: list[Exception] = []
        for execution, result in zip(targets, termination_results, strict=True):
            attempt_id = execution.attempt.attempt_id
            if isinstance(result, BaseException):
                marking_error = marking_errors.get(attempt_id)
                if marking_error is not None:
                    failures.append(marking_error)
                failures.append(_as_exception(result))
                continue

            await asyncio.gather(execution.task, return_exceptions=True)
            try:
                await self._settle_cancelled_execution(execution)
            except Exception as exc:
                marking_error = marking_errors.get(attempt_id)
                if marking_error is not None:
                    failures.append(marking_error)
                failures.append(exc)

        await self._publish()
        if failures:
            raise _TerminationFailure(
                'failed to terminate active executions',
                failures,
            )

    async def _settle_cancelled_execution(self, execution: _ActiveExecution) -> None:
        attempt_id = execution.attempt.attempt_id
        execution.attempt = await self._store.set_attempt_status(
            self.run_id,
            attempt_id,
            'cancelled',
        )
        self._remove_active(execution)

    def _remove_active(self, execution: _ActiveExecution) -> None:
        attempt_id = execution.attempt.attempt_id
        if self._active_attempts.get(attempt_id) is execution:
            del self._active_attempts[attempt_id]

    async def _continue_after_cancellation(self) -> None:
        if self._pending_failure is not None:
            await self._finish_pending_failure()
        elif self._status == 'running':
            await self._schedule()
        elif self._active_attempts:
            await self._publish()
        elif self._status == 'pausing':
            await self._transition('paused')
        elif self._status == 'cancelling':
            await self._transition('cancelled')
        elif self._status == 'failed':
            await self._publish()
        else:
            await self._publish()

    # Failure, shutdown and observable state

    async def _fail(self, error: Exception) -> None:
        try:
            await self._cancel_invocations()
            await self._transition('failed', error=_error_info(error))
        except Exception as cleanup_error:
            raise _SessionFailure(
                'run failed while its state and executions were being settled',
                (error, cleanup_error),
            ) from error

    async def _handle_session_failure(self, cause: BaseException) -> BaseException | None:
        failures: list[BaseException] = [cause]
        if (
            self._pending_failure is None
            and self._status not in {'cancelled', 'failed', 'completed'}
        ):
            self._pending_failure = _error_info(cause)
        try:
            await self._cancel_invocations()
        except Exception as cleanup_error:
            failures.append(cleanup_error)

        if self._pending_failure is not None:
            try:
                await self._persist_status('failed', error=self._pending_failure)
                self._pending_failure = None
            except Exception as persistence_error:
                failures.append(persistence_error)

        self._closing = not self._active_attempts and self._pending_failure is None
        await self._publish()
        if self._active_attempts:
            return None
        if self._pending_failure is not None:
            return BaseExceptionGroup('run session failed during cleanup', failures)
        if len(failures) == 1:
            return None
        return BaseExceptionGroup('run session failed during cleanup', failures)

    async def _finish_pending_failure(self) -> None:
        await self._cancel_invocations()
        error = self._pending_failure
        if error is None:
            return
        await self._persist_status('failed', error=error)
        self._pending_failure = None
        self._closing = True
        await self._publish()

    async def _transition(self, status: RunStatus, view: ArtifactSnapshot | None = None, *,
                          error: RuntimeErrorInfo | None = None
                          ) -> None:
        await self._persist_status(status, error=error)
        await self._publish(view)
        if status == 'cancelled':
            self._closing = True

    async def _persist_status(self, status: RunStatus, *, error: RuntimeErrorInfo | None = None) -> None:
        await self._store.set_run_state(self.run_id, status, error=error)
        self._status = status
        self._error = error

    async def _publish(self, view: ArtifactSnapshot | None = None) -> None:
        if view is not None:
            self._view = view
        running = ()
        if self._status in {'running', 'pausing', 'cancelling'}:
            running = tuple(sorted(
                (
                    InvocationSnapshot(
                        execution.invocation.invocation_id,
                        execution.invocation.operation.spec.op_id,
                        execution.invocation.partition_key,
                    )
                    for execution in self._active_attempts.values()
                ),
                key=lambda item: item.invocation_id,
            ))
        attempts = tuple(sorted(
            (execution.attempt for execution in self._active_attempts.values()),
            key=lambda attempt: attempt.attempt_id,
        ))
        snapshot = RuntimeSnapshot(
            self.run_id,
            self._status,
            running,
            sum(len(queue) for queue in self._pending_invocations.values())
            + sum(self._remaining_planned.values()),
            {key: record.ref for key, record in self._view.records.items()},
            self._view.partition_sets,
            self._error,
            attempts,
        )
        async with self._condition:
            self._snapshot = snapshot
            self._condition.notify_all()

    async def _finish_pending_commands(self) -> None:
        async with self._request_lock:
            self._accept_commands = False
            while True:
                try:
                    event = self._events.get_nowait()
                except asyncio.QueueEmpty:
                    break
                match event:
                    case _Command() if not event.reply.done():
                        if event.kind == 'cancel' and self._status == 'cancelled':
                            event.reply.set_result(self._snapshot)
                        elif event.kind in {'release', 'close'}:
                            event.reply.set_result(self._snapshot)
                        else:
                            event.reply.set_exception(RuntimeError('run session is closed'))
                    case _CommitCommand() if not event.reply.done():
                        event.reply.set_exception(RuntimeError('run session is closed'))


def _error_info(error: BaseException) -> RuntimeErrorInfo:
    primary = error
    while isinstance(primary, BaseExceptionGroup):
        primary = primary.exceptions[0]
    return RuntimeErrorInfo(type(primary).__name__, str(primary) or repr(primary))


__all__ = ['RunSession']
