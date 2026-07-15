from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .artifact import ArtifactMutation, ArtifactRecord, ArtifactRef, CollectionMutation
from .errors import DefinitionError
from .operation import Operation
from .planning import RuntimeDefinition, compile_operations
from .session import RunSession
from .state import RuntimeSnapshot
from .store import ArtifactStore


def _text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be str')
    if not value.strip():
        raise DefinitionError(f'{name} must be non-empty')


@dataclass(frozen=True)
class _SessionEntry:
    session: RunSession
    task: asyncio.Task[None]


class ArtifactRuntime:
    def __init__(
        self, store: ArtifactStore, definition: RuntimeDefinition, *,
        max_concurrency: int, terminate_timeout: float,
    ) -> None:
        self._store = store
        self._definition = definition
        self._max_concurrency = max_concurrency
        self._terminate_timeout = terminate_timeout
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls, root: str | Path, operations: Sequence[Operation], *,
        max_concurrency: int = 4, terminate_timeout: float = 1.0,
    ) -> ArtifactRuntime:
        if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool):
            raise TypeError('max_concurrency must be int')
        if max_concurrency < 1:
            raise DefinitionError('max_concurrency must be >= 1')
        if terminate_timeout <= 0:
            raise DefinitionError('terminate_timeout must be positive')
        definition = compile_operations(operations)
        return cls(
            await ArtifactStore.open(root),
            definition,
            max_concurrency=max_concurrency,
            terminate_timeout=terminate_timeout,
        )

    async def start(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.start()

    async def pause(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.pause()

    async def resume(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.resume()

    async def cancel(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.cancel()

    async def mutate(
        self, run_id: str, mutation: ArtifactMutation | CollectionMutation, *,
        idempotency_key: str,
    ) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.mutate(mutation, idempotency_key=idempotency_key)

    async def snapshot(self, run_id: str) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return session.snapshot()

    async def wait_for_status(
        self, run_id: str, statuses: str | tuple[str, ...], *, timeout: float = 10.0,
    ) -> RuntimeSnapshot:
        session = await self._session(run_id)
        return await session.wait_for_status(statuses, timeout=timeout)

    async def read(self, run_id: str, ref: ArtifactRef) -> object | None:
        async with self._lock:
            if self._closed:
                raise RuntimeError('artifact runtime is closed')
            return await self._store.read(run_id, ref)

    async def record(self, run_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        async with self._lock:
            if self._closed:
                raise RuntimeError('artifact runtime is closed')
            return await self._store.record(run_id, ref)

    async def run_ids(self) -> tuple[str, ...]:
        async with self._lock:
            if self._closed:
                raise RuntimeError('artifact runtime is closed')
            return await self._store.run_ids()

    async def delete_run(self, run_id: str) -> None:
        _text(run_id, 'run_id')
        async with self._lock:
            if self._closed:
                raise RuntimeError('artifact runtime is closed')
            if run_id in self._sessions:
                raise RuntimeError('cannot delete a run with an active session')
            await self._store.delete_run(run_id)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._sessions.values())

        if entries:
            await asyncio.gather(*(entry.session.close() for entry in entries), return_exceptions=True)
            await asyncio.gather(*(entry.task for entry in entries), return_exceptions=True)
        await self._store.close()

    async def _session(self, run_id: str) -> RunSession:
        if self._closed:
            raise RuntimeError('artifact runtime is closed')
        _text(run_id, 'run_id')
        async with self._lock:
            if self._closed:
                raise RuntimeError('artifact runtime is closed')
            entry = self._sessions.get(run_id)
            if entry is None:
                session = RunSession(
                    run_id,
                    self._definition,
                    self._store,
                    max_concurrency=self._max_concurrency,
                    terminate_timeout=self._terminate_timeout,
                )
                task = asyncio.create_task(session.serve(), name=f'artifact-run:{run_id}')
                entry = _SessionEntry(session, task)
                self._sessions[run_id] = entry
                task.add_done_callback(
                    lambda _completed, current=entry, key=run_id: self._discard_session(key, current)
                )

        await entry.session.wait_ready()
        return entry.session

    def _discard_session(self, run_id: str, entry: _SessionEntry) -> None:
        if not entry.task.cancelled():
            entry.task.exception()
        if self._sessions.get(run_id) is entry:
            del self._sessions[run_id]


__all__ = ['ArtifactRuntime']
