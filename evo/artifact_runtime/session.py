from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass
from typing import Literal

from .artifact import (
    ArtifactMutation,
    CollectionMutation,
)
from .errors import DefinitionError, PlanningError
from .execution import execute
from .operation import OperationContext, OperationInvocation, OperationResult
from .planning import PlanningDecision, PlanningView, RuntimeDefinition, plan_next
from .state import InvocationSnapshot, RunStatus, RuntimeErrorInfo, RuntimeSnapshot
from .store import ArtifactStore


def _text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be str')
    if not value.strip():
        raise DefinitionError(f'{name} must be non-empty')


@dataclass(frozen=True)
class _Command:
    kind: Literal['start', 'pause', 'resume', 'cancel', 'close']
    reply: asyncio.Future[RuntimeSnapshot]


@dataclass(frozen=True)
class _ExecutionDone:
    invocation: OperationInvocation
    result: OperationResult | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class _ExecutionStarted:
    invocation: OperationInvocation
    reply: asyncio.Future[None]


@dataclass(frozen=True)
class _MutationCommand:
    mutation: ArtifactMutation | CollectionMutation
    idempotency_key: str
    reply: asyncio.Future[RuntimeSnapshot]


class RunSession:
    def __init__(
        self, run_id: str, definition: RuntimeDefinition, store: ArtifactStore, *,
        max_concurrency: int = 4, terminate_timeout: float = 1.0,
    ) -> None:
        _text(run_id, 'run_id')
        if not isinstance(store, ArtifactStore):
            raise TypeError('store must be ArtifactStore')
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool):
            raise TypeError('max_concurrency must be int')
        if max_concurrency < 1:
            raise DefinitionError('max_concurrency must be >= 1')
        if terminate_timeout <= 0:
            raise DefinitionError('terminate_timeout must be positive')
        if not isinstance(definition, RuntimeDefinition):
            raise TypeError('definition must be RuntimeDefinition')

        self.run_id = run_id
        self._definition = definition
        self._store = store
        self._terminate_timeout = terminate_timeout

        self._events: asyncio.Queue[
            _Command | _MutationCommand | _ExecutionStarted | _ExecutionDone
        ] = asyncio.Queue()
        self._condition = asyncio.Condition()
        self._request_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._stopped = asyncio.Event()
        self._startup_error: BaseException | None = None
        self._task_group: asyncio.TaskGroup | None = None
        self._max_concurrency = max_concurrency
        self._pending_invocations: dict[str, deque[OperationInvocation]] = {}
        self._active_invocations: dict[
            str, tuple[OperationInvocation, asyncio.Task[None]]
        ] = {}
        self._running_invocation_ids: set[str] = set()

        self._status: RunStatus = 'created'
        self._error: RuntimeErrorInfo | None = None
        self._accept_results = False
        self._accept_commands = True
        self._closing = False
        self._view = PlanningView({}, {})
        self._snapshot = RuntimeSnapshot(run_id)

    # Session lifecycle and public commands

    async def serve(self) -> None:
        try:
            async with asyncio.TaskGroup() as group:
                self._task_group = group
                await self._initialize()
                self._ready.set()
                while not self._closing:
                    event = await self._events.get()
                    match event:
                        case _Command():
                            await self._command(event)
                        case _MutationCommand():
                            await self._mutation_command(event)
                        case _ExecutionStarted():
                            await self._execution_started(event)
                        case _ExecutionDone():
                            await self._execution_done(event)
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            if not self._closing and self._status not in {'cancelled', 'failed', 'completed'}:
                self._discard_invocations()
                self._error = RuntimeErrorInfo(type(exc).__name__, str(exc) or repr(exc))
                self._status = 'failed'
                try:
                    await self._store.set_run_state(
                        self.run_id,
                        'failed',
                        error_kind=self._error.kind,
                        error_message=self._error.message,
                    )
                    await self._publish()
                except Exception:
                    pass
            raise
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

    async def cancel(self) -> RuntimeSnapshot:
        if self._snapshot.status == 'cancelled':
            return self._snapshot
        return await self._request('cancel')

    async def close(self) -> RuntimeSnapshot:
        if self._stopped.is_set():
            return self._snapshot
        return await self._request('close')

    async def mutate(
        self, mutation: ArtifactMutation | CollectionMutation, *, idempotency_key: str,
    ) -> RuntimeSnapshot:
        if not isinstance(mutation, (ArtifactMutation, CollectionMutation)):
            raise TypeError('mutation must be ArtifactMutation or CollectionMutation')
        _text(idempotency_key, 'idempotency_key')
        await self.wait_ready()
        async with self._request_lock:
            if not self._accept_commands or self._stopped.is_set():
                raise RuntimeError('run session is closed')
            reply = asyncio.get_running_loop().create_future()
            self._events.put_nowait(_MutationCommand(mutation, idempotency_key, reply))
        return await reply

    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot

    async def wait_for_status(
        self, statuses: str | tuple[str, ...], *, timeout: float = 10.0,
    ) -> RuntimeSnapshot:
        expected = {statuses} if isinstance(statuses, str) else set(statuses)
        async with asyncio.timeout(timeout):
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self._snapshot.status in expected or self._stopped.is_set()
                )
                if self._snapshot.status not in expected:
                    raise RuntimeError('run session stopped before reaching requested status')
                return self._snapshot

    # Serialized command handling and state transitions

    async def _request(self, kind: Literal['start', 'pause', 'resume', 'cancel', 'close']) -> RuntimeSnapshot:
        await self.wait_ready()
        async with self._request_lock:
            if not self._accept_commands or self._stopped.is_set():
                if kind == 'cancel' and self._snapshot.status == 'cancelled':
                    return self._snapshot
                if kind == 'close':
                    return self._snapshot
                raise RuntimeError('run session is closed')
            reply = asyncio.get_running_loop().create_future()
            self._events.put_nowait(_Command(kind, reply))
        return await reply

    async def _initialize(self) -> None:
        stored = await self._store.run_state(self.run_id)
        decision = await self._decide_current_state()
        if stored is None:
            await self._store.set_run_state(self.run_id, 'created')
        elif stored.status in {'running', 'pausing', 'cancelling'}:
            self._status = 'paused'
            await self._store.set_run_state(self.run_id, 'paused')
        elif stored.status == 'completed' and not decision.complete:
            self._status = 'paused'
            await self._store.set_run_state(self.run_id, 'paused')
        else:
            self._status = stored.status  # type: ignore[assignment]
            if stored.status == 'failed':
                self._error = RuntimeErrorInfo(
                    stored.error_kind or 'RuntimeError',
                    stored.error_message or 'run failed',
                )
        await self._publish(decision.view)
        if self._status in {'cancelled', 'failed'}:
            self._closing = True

    async def _command(self, command: _Command) -> None:
        try:
            match command.kind:
                case 'start':
                    await self._start()
                case 'pause':
                    await self._pause()
                case 'resume':
                    await self._resume()
                case 'cancel':
                    await self._cancel()
                case 'close':
                    await self._close()

            if not command.reply.done():
                command.reply.set_result(self._snapshot)
        except Exception as exc:
            if not command.reply.done():
                command.reply.set_exception(exc)

    async def _mutation_command(self, command: _MutationCommand) -> None:
        try:
            await self._mutate(command.mutation, command.idempotency_key)
            if not command.reply.done():
                command.reply.set_result(self._snapshot)
        except Exception as exc:
            if not command.reply.done():
                command.reply.set_exception(exc)

    async def _start(self) -> None:
        if self._status != 'created':
            raise DefinitionError(f'cannot start run from {self._status}')
        await self._enter_running()

    async def _pause(self) -> None:
        if self._status != 'running':
            raise DefinitionError(f'cannot pause run from {self._status}')
        self._accept_results = False
        await self._transition('pausing')
        await self._cancel_invocations()
        await self._transition('paused')

    async def _resume(self) -> None:
        if self._status != 'paused':
            raise DefinitionError(f'cannot resume run from {self._status}')
        await self._enter_running()

    async def _enter_running(self) -> None:
        self._accept_results = True
        await self._persist_status('running')
        await self._schedule()

    async def _cancel(self) -> None:
        if self._status == 'cancelled':
            return
        if self._status in {'failed', 'completed'}:
            raise DefinitionError(f'cannot cancel run from {self._status}')
        self._accept_results = False
        await self._transition('cancelling')
        await self._cancel_invocations()
        await self._transition('cancelled')

    async def _close(self) -> None:
        self._accept_results = False
        if self._status == 'running':
            await self._transition('pausing')
            await self._cancel_invocations()
            await self._transition('paused')
        else:
            await self._cancel_invocations()
        self._closing = True

    async def _mutate(
        self, mutation: ArtifactMutation | CollectionMutation, idempotency_key: str,
    ) -> None:
        self._definition.validate_mutation(mutation)
        previous_status = self._status
        match previous_status:
            case 'created' | 'paused' | 'completed':
                pass
            case 'running':
                self._accept_results = False
                await self._transition('pausing')
                await self._cancel_invocations()
            case _:
                raise DefinitionError(f'cannot mutate artifact from {previous_status}')

        try:
            committed = await self._store.commit_mutation(
                self.run_id, mutation, idempotency_key=idempotency_key
            )
            if committed.status == 'stale':
                raise DefinitionError('artifact mutation expected_ref is stale')
        except Exception:
            if previous_status == 'running':
                await self._enter_running()
            raise

        match previous_status:
            case 'running' | 'completed':
                await self._enter_running()
            case 'created' | 'paused':
                decision = await self._decide_current_state()
                await self._publish(decision.view)

    # Planning and invocation execution

    async def _decide_current_state(self) -> PlanningDecision:
        artifacts = await self._store.snapshot(self.run_id)
        return plan_next(self._definition, artifacts)

    async def _schedule(self) -> None:
        try:
            await self._plan_execution()
        except Exception as exc:
            await self._fail(exc)

    async def _plan_execution(self) -> None:
        decision = await self._materialize_ready_collections()
        known = set(self._active_invocations)
        known.update(
            invocation.invocation_id
            for queue in self._pending_invocations.values()
            for invocation in queue
        )
        for invocation in decision.invocations:
            if invocation.invocation_id in known:
                continue
            op_id = invocation.operation.spec.op_id
            self._pending_invocations.setdefault(op_id, deque()).append(invocation)
            known.add(invocation.invocation_id)
        self._start_ready_invocations()

        if self._active_invocations or self._pending_invocations:
            await self._publish(decision.view)
            return
        if decision.complete:
            self._accept_results = False
            await self._transition('completed', decision.view)
            return
        if decision.blocked_reason:
            await self._publish(decision.view)
            raise PlanningError(decision.blocked_reason)

        await self._publish(decision.view)

    async def _materialize_ready_collections(self) -> PlanningDecision:
        while True:
            decision = await self._decide_current_state()
            if not decision.projections:
                return decision

            committed_any = False
            for projection in decision.projections:
                committed = await self._store.commit_projection(self.run_id, projection)
                committed_any |= committed.status == 'ok'

            if committed_any:
                continue

            refreshed_decision = await self._decide_current_state()
            if any(
                projection in refreshed_decision.projections
                for projection in decision.projections
            ):
                raise PlanningError('collection projection made no progress')

    def _start_ready_invocations(self) -> None:
        if self._task_group is None:
            raise RuntimeError('run session task group is not active')
        active_by_operation = Counter(
            invocation.operation.spec.op_id
            for invocation, _ in self._active_invocations.values()
        )
        for op_id in tuple(self._pending_invocations):
            queue = self._pending_invocations.pop(op_id)
            while queue and len(self._active_invocations) < self._max_concurrency:
                invocation = queue[0]
                if active_by_operation[op_id] >= invocation.operation.spec.max_concurrency:
                    break

                queue.popleft()
                task = self._task_group.create_task(
                    self._run_invocation(invocation),
                    name=invocation.invocation_id,
                )
                self._active_invocations[invocation.invocation_id] = (invocation, task)
                active_by_operation[op_id] += 1

            if queue:
                self._pending_invocations[op_id] = queue
            if len(self._active_invocations) >= self._max_concurrency:
                break

    async def _cancel_invocations(self) -> None:
        tasks = tuple(task for _, task in self._active_invocations.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._discard_invocations()

    def _discard_invocations(self) -> None:
        self._active_invocations.clear()
        self._pending_invocations.clear()
        self._running_invocation_ids.clear()

    async def _run_invocation(self, invocation: OperationInvocation) -> None:
        try:
            started = asyncio.get_running_loop().create_future()
            await self._events.put(_ExecutionStarted(invocation, started))
            await started
            values = await self._store.read_many(self.run_id, invocation.value_refs())
            inputs = invocation.bind_values(values)
            ctx = OperationContext(self.run_id, invocation.invocation_id, invocation.item_key)
            result = await execute(
                invocation,
                ctx,
                inputs,
                terminate_timeout=self._terminate_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._events.put(_ExecutionDone(invocation, error=exc))
        else:
            await self._events.put(_ExecutionDone(invocation, result=result))

    async def _execution_started(self, event: _ExecutionStarted) -> None:
        invocation_id = event.invocation.invocation_id
        if (
            not self._accept_results
            or self._status != 'running'
            or invocation_id not in self._active_invocations
        ):
            if not event.reply.done():
                event.reply.set_exception(RuntimeError('invocation is no longer active'))
            return
        self._running_invocation_ids.add(invocation_id)
        await self._publish()
        if not event.reply.done():
            event.reply.set_result(None)

    async def _execution_done(self, event: _ExecutionDone) -> None:
        invocation_id = event.invocation.invocation_id
        was_active = self._active_invocations.pop(invocation_id, None) is not None
        self._running_invocation_ids.discard(invocation_id)
        if not was_active or not self._accept_results or self._status != 'running':
            return

        if event.error is not None:
            await self._fail(event.error)
            return
        if event.result is None:
            await self._fail(RuntimeError('operation completed without result'))
            return

        try:
            committed = await self._store.commit_operation(
                self.run_id,
                event.invocation.operation_writes(event.result),
            )
        except Exception as exc:
            await self._fail(exc)
            return

        if committed.status == 'stale':
            decision = await self._decide_current_state()
            if any(
                item.invocation_id == event.invocation.invocation_id
                for item in decision.invocations
            ):
                await self._fail(PlanningError(
                    f'{event.invocation.invocation_id} produced no committable artifact'
                ))
                return

        self._start_ready_invocations()
        if self._active_invocations or self._pending_invocations:
            await self._publish()
            return
        await self._schedule()

    # Shutdown, failure and observable snapshot

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
                        elif event.kind == 'close':
                            event.reply.set_result(self._snapshot)
                        else:
                            event.reply.set_exception(RuntimeError('run session is closed'))
                    case _MutationCommand() | _ExecutionStarted() if not event.reply.done():
                        event.reply.set_exception(RuntimeError('run session is closed'))

    async def _fail(self, error: Exception) -> None:
        self._accept_results = False
        await self._cancel_invocations()
        self._error = RuntimeErrorInfo(type(error).__name__, str(error) or repr(error))
        await self._transition('failed')

    async def _transition(
        self, status: RunStatus, view: PlanningView | None = None,
    ) -> None:
        await self._persist_status(status)
        await self._publish(view)
        if status in {'cancelled', 'failed'}:
            self._closing = True

    async def _persist_status(self, status: RunStatus) -> None:
        self._status = status
        await self._store.set_run_state(
            self.run_id,
            status,
            error_kind='' if self._error is None else self._error.kind,
            error_message='' if self._error is None else self._error.message,
        )

    async def _publish(self, view: PlanningView | None = None) -> None:
        if view is not None:
            self._view = view
        effective = self._view.records
        running = tuple(sorted(
            (
                InvocationSnapshot(
                    invocation.invocation_id,
                    invocation.operation.spec.op_id,
                    invocation.item_key,
                )
                for invocation_id, (invocation, _) in self._active_invocations.items()
                if invocation_id in self._running_invocation_ids
            ),
            key=lambda item: item.invocation_id,
        ))
        snapshot = RuntimeSnapshot(
            self.run_id,
            self._status,
            running,
            sum(len(queue) for queue in self._pending_invocations.values())
            + len(self._active_invocations)
            - len(self._running_invocation_ids),
            {key: record.ref for key, record in effective.items()},
            self._view.collections,
            self._error,
        )
        async with self._condition:
            self._snapshot = snapshot
            self._condition.notify_all()


__all__ = [
    'InvocationSnapshot',
    'RunStatus',
    'RuntimeErrorInfo',
    'RuntimeSnapshot',
    'RunSession',
]
