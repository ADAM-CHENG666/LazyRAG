from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from .artifact import ArtifactCommit, ArtifactRecord, ArtifactRef
from .errors import DefinitionError
from .operation import Operation
from .planning import RuntimeDefinition, compile_operations
from .session import RunSession
from .state import AttemptSnapshot, ProgressEvent, RuntimeSnapshot
from .store import ArtifactStore
from .utils import _as_exception, _positive_int, _positive_number, _text


_ACTIVE_STATUSES = frozenset({'running', 'pausing', 'cancelling'})


@dataclass(frozen=True, slots=True)
class _SessionEntry:
    session: RunSession
    task: asyncio.Task[None]


class ArtifactRuntime:
    """Own the Store and the in-memory Session actor for each accessed run."""

    def __init__(self, store: ArtifactStore, definition: RuntimeDefinition, *, max_concurrency: int,
                 terminate_timeout: float
                 ) -> None:
        self._store = store
        self._definition = definition
        self._max_run_concurrency = max_concurrency
        self._terminate_timeout = terminate_timeout
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, root: str | Path, operations: Sequence[Operation], *, max_concurrency: int = 4,
                   terminate_timeout: float = 1.0
                   ) -> ArtifactRuntime:
        """Open a runtime whose concurrency limit applies independently to each run."""
        _positive_int(max_concurrency, 'max_concurrency')
        _positive_number(terminate_timeout, 'terminate_timeout')
        definition = compile_operations(operations)
        return cls(
            await ArtifactStore.open(root),
            definition,
            max_concurrency=max_concurrency,
            terminate_timeout=terminate_timeout,
        )

    async def __aenter__(self) -> Self:
        self._require_open()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        traceback: TracebackType | None
                        ) -> None:
        await self.close()

    async def start(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.start()

    async def pause(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id, create_run=False)
        return await session.pause()

    async def resume(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id, create_run=False)
        return await session.resume()

    async def retry(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id, create_run=False)
        return await session.retry()

    async def cancel(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id, create_run=False)
        return await session.cancel()

    async def commit(self, run_id: str, commit: ArtifactCommit) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.commit(commit)

    async def snapshot(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id, create_run=False)
        return session.snapshot()

    async def wait_for_status(self, run_id: str, statuses: str | tuple[str, ...], *, timeout: float = 10.0
                              ) -> RuntimeSnapshot:
        session = await self._session(run_id, create_run=False)
        return await session.wait_for_status(statuses, timeout=timeout)

    async def attempts(self, run_id: str) -> tuple[AttemptSnapshot, ...]:
        async with self._lock:
            await self._require_run(run_id)
            return await self._store.attempts(run_id)

    async def progress_events(self, run_id: str, attempt_id: str | None = None
                              ) -> tuple[ProgressEvent, ...]:
        async with self._lock:
            await self._require_run(run_id)
            return await self._store.progress_events(run_id, attempt_id)

    async def read(self, run_id: str, ref: ArtifactRef) -> object:
        async with self._lock:
            self._require_open()
            return await self._store.read(run_id, ref)

    async def record(self, run_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        async with self._lock:
            self._require_open()
            return await self._store.record(run_id, ref)

    async def run_ids(self) -> tuple[str, ...]:
        async with self._lock:
            self._require_open()
            return await self._store.run_ids()

    async def release(self, run_id: str) -> None:
        """Release a quiescent Session without deleting its persisted run."""
        _text(run_id, 'run_id')
        async with self._lock:
            self._require_open()
            entry = self._sessions.get(run_id)
            if entry is None:
                await self._require_run(run_id)
                return
            await self._release_entry(run_id, entry)

    async def delete_run(self, run_id: str) -> None:
        _text(run_id, 'run_id')
        async with self._lock:
            self._require_open()
            entry = self._sessions.get(run_id)
            if entry is None:
                state = await self._store.run_state(run_id)
                if state is None:
                    raise DefinitionError(f'run not found: {run_id}')
                if state.status in _ACTIVE_STATUSES:
                    raise RuntimeError('cannot delete a run with active persisted state')
            else:
                await self._release_entry(run_id, entry)
            await self._store.delete_run(run_id)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            entries = tuple(self._sessions.values())
            results = await asyncio.gather(
                *(entry.session.close() for entry in entries),
                return_exceptions=True,
            )
            closed = tuple(
                entry for entry, result in zip(entries, results, strict=True)
                if not isinstance(result, BaseException)
            )
            failures = [
                _as_exception(result)
                for result in results
                if isinstance(result, BaseException)
            ]
            task_results = await asyncio.gather(
                *(entry.task for entry in closed),
                return_exceptions=True,
            )
            failures.extend(
                _as_exception(result)
                for result in task_results
                if isinstance(result, BaseException)
            )
            if failures:
                raise ExceptionGroup(
                    'artifact runtime failed to close cleanly',
                    failures,
                )
            await self._store.close()
            self._sessions.clear()
            self._closed = True

    async def _session(self, run_id: str, *, create_run: bool = True) -> RunSession:
        _text(run_id, 'run_id')
        async with self._lock:
            self._require_open()
            entry = self._sessions.get(run_id)
            if entry is not None and entry.task.done():
                self._discard_session(run_id, entry)
                entry = None
            if entry is None:
                if not create_run and await self._store.run_state(run_id) is None:
                    raise DefinitionError(f'run not found: {run_id}')
                session = RunSession(
                    run_id,
                    self._definition,
                    self._store,
                    max_concurrency=self._max_run_concurrency,
                    terminate_timeout=self._terminate_timeout,
                )
                task = asyncio.create_task(
                    session.serve(),
                    name=f'artifact-run:{run_id}',
                )
                entry = _SessionEntry(session, task)
                self._sessions[run_id] = entry
                task.add_done_callback(
                    lambda _completed, current=entry, key=run_id:
                    self._discard_session(key, current)
                )

        await entry.session.wait_ready()
        return entry.session

    async def _require_run(self, run_id: str) -> None:
        self._require_open()
        _text(run_id, 'run_id')
        if await self._store.run_state(run_id) is None:
            raise DefinitionError(f'run not found: {run_id}')

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError('artifact runtime is closed')

    async def _release_entry(self, run_id: str, entry: _SessionEntry) -> None:
        await entry.session.release()
        await entry.task
        if self._sessions.get(run_id) is entry:
            del self._sessions[run_id]

    def _discard_session(self, run_id: str, entry: _SessionEntry) -> None:
        if self._sessions.get(run_id) is not entry:
            return
        error = None if entry.task.cancelled() else entry.task.exception()
        if error is not None:
            entry.task.get_loop().call_exception_handler({
                'message': f'artifact run session failed: {run_id}',
                'exception': error,
                'task': entry.task,
            })
        del self._sessions[run_id]


__all__ = ['ArtifactRuntime']
