"""SQLite persistence and atomic commit boundary for artifact-runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import pickle
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast, get_args

import aiosqlite

from .artifact import (
    ArtifactChangeSet,
    ArtifactCommit,
    ArtifactKey,
    ArtifactRecord,
    ArtifactRef,
    ArtifactSnapshot,
    PartitionSet,
)
from .errors import DefinitionError
from .state import (
    AttemptSnapshot,
    AttemptStatus,
    ProgressEvent,
    ProgressUpdate,
    RunStatus,
    RuntimeErrorInfo,
)
from .utils import _text


_SCHEMA_VERSION = 1
_RUN_STATUSES = frozenset(get_args(RunStatus))
_PUBLIC_ATTEMPT_TRANSITIONS = {
    'scheduled': frozenset({'running', 'cancelling', 'cancelled', 'interrupted'}),
    'running': frozenset({
        'cancelling', 'cancelled', 'failed', 'interrupted',
    }),
    'cancelling': frozenset({'cancelled', 'interrupted'}),
    'cancelled': frozenset(),
    'succeeded': frozenset(),
    'failed': frozenset(),
    'interrupted': frozenset(),
    'discarded': frozenset(),
}


@dataclass(frozen=True)
class StoredRunState:
    status: RunStatus
    error: RuntimeErrorInfo | None = None

    def __post_init__(self) -> None:
        if self.status not in _RUN_STATUSES:
            raise DefinitionError(f'unknown run status: {self.status}')
        if self.error is not None and not isinstance(self.error, RuntimeErrorInfo):
            raise TypeError('run error must be RuntimeErrorInfo or None')
        if self.status == 'failed' and self.error is None:
            raise DefinitionError('failed run requires error details')
        if self.status != 'failed' and self.error is not None:
            raise DefinitionError('run error is only valid for failed status')


@dataclass(frozen=True)
class CommitResult:
    status: Literal['ok', 'stale']
    refs: tuple[ArtifactRef, ...] = ()
    changes: ArtifactChangeSet = field(default_factory=ArtifactChangeSet, compare=False)
    replayed: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if self.status not in {'ok', 'stale'}:
            raise DefinitionError(f'unknown commit status: {self.status}')
        refs = tuple(self.refs)
        if not all(isinstance(ref, ArtifactRef) for ref in refs):
            raise TypeError('commit result refs must contain ArtifactRef values')
        object.__setattr__(self, 'refs', refs)


@dataclass(frozen=True)
class _PreparedCommit:
    run_id: str
    command: ArtifactCommit
    payloads: tuple[bytes, ...]
    request_hash: str

    def __post_init__(self) -> None:
        if len(self.payloads) != len(self.command.writes):
            raise ValueError('prepared payload count must match commit writes')


class ArtifactStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, root: str | Path) -> ArtifactStore:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path / 'artifact-runtime.sqlite3')
        try:
            connection.row_factory = aiosqlite.Row
            await connection.execute('PRAGMA foreign_keys = ON')
            await connection.execute('PRAGMA journal_mode = WAL')
            await connection.execute('PRAGMA synchronous = FULL')
            store = cls(connection)
            await store._create_schema()
            return store
        except BaseException:
            await connection.close()
            raise

    async def close(self) -> None:
        await self._connection.close()

    # Artifact commits

    async def commit(self, run_id: str, commit: ArtifactCommit, *, attempt_id: str | None = None
                     ) -> CommitResult:
        _text(run_id, 'run_id')
        if not isinstance(commit, ArtifactCommit):
            raise TypeError('commit must be ArtifactCommit')
        if attempt_id is None and commit.producer.startswith('operation:'):
            raise DefinitionError('operation commit requires attempt_id')
        if attempt_id is not None:
            _text(attempt_id, 'attempt_id')
        prepared = await asyncio.to_thread(_prepare_commit, run_id, commit)
        return await self._commit(prepared, attempt_id=attempt_id)

    # Artifact reads

    async def snapshot(self, run_id: str, partition_set_ids: Iterable[str] = ()) -> ArtifactSnapshot:
        _text(run_id, 'run_id')
        ids = frozenset(partition_set_ids)
        for artifact_id in ids:
            _text(artifact_id, 'partition set artifact_id')
        async with self._lock:
            return await self._snapshot(run_id, ids)

    async def read(self, run_id: str, ref: ArtifactRef) -> object:
        _text(run_id, 'run_id')
        if not isinstance(ref, ArtifactRef):
            raise TypeError('ref must be ArtifactRef')
        async with self._lock:
            found, value = await self._read(run_id, ref)
        if not found:
            raise KeyError(ref)
        return value

    async def record(self, run_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        _text(run_id, 'run_id')
        if not isinstance(ref, ArtifactRef):
            raise TypeError('ref must be ArtifactRef')
        async with self._lock:
            return await self._record(run_id, ref)

    async def read_many(self, run_id: str, refs: Iterable[ArtifactRef]) -> Mapping[ArtifactRef, object]:
        _text(run_id, 'run_id')
        requested = tuple(refs)
        if not all(isinstance(ref, ArtifactRef) for ref in requested):
            raise TypeError('refs must contain ArtifactRef values')
        payloads: dict[ArtifactRef, bytes] = {}
        async with self._lock:
            for offset in range(0, len(requested), 250):
                chunk = requested[offset:offset + 250]
                placeholders = ','.join('(?, ?, ?)' for _ in chunk)
                parameters: list[object] = [run_id]
                for ref in chunk:
                    parameters.extend((
                        ref.key.artifact_id,
                        ref.key.partition_key,
                        ref.version,
                    ))
                cursor = await self._connection.execute(
                    f"""
                    SELECT artifact_id, partition_key, version, payload
                    FROM artifact_payloads
                    WHERE run_id = ?
                      AND (artifact_id, partition_key, version) IN ({placeholders})
                    """,
                    parameters,
                )
                for row in await cursor.fetchall():
                    ref = ArtifactRef(
                        ArtifactKey(row['artifact_id'], row['partition_key']),
                        row['version'],
                    )
                    payloads[ref] = row['payload']
        missing = next((ref for ref in requested if ref not in payloads), None)
        if missing is not None:
            raise DefinitionError(f'input artifact is missing: {missing}')
        return await asyncio.to_thread(_deserialize_many, requested, payloads)

    # Run state

    async def create_run(self, run_id: str) -> StoredRunState:
        _text(run_id, 'run_id')
        state = StoredRunState('created')
        async with self._transaction():
            try:
                await self._connection.execute(
                    """
                    INSERT INTO run_states(run_id, status, error_kind, error_message)
                    VALUES (?, 'created', '', '')
                    """,
                    (run_id,),
                )
            except aiosqlite.IntegrityError as exc:
                raise DefinitionError(f'run already exists: {run_id}') from exc
        return state

    async def set_run_state(self, run_id: str, status: RunStatus, *,
                            error: RuntimeErrorInfo | None = None
                            ) -> None:
        _text(run_id, 'run_id')
        state = StoredRunState(status, error)
        error_kind = '' if state.error is None else state.error.kind
        error_message = '' if state.error is None else state.error.message
        async with self._transaction():
            cursor = await self._connection.execute(
                """
                UPDATE run_states
                SET status = ?, error_kind = ?, error_message = ?
                WHERE run_id = ?
                """,
                (status, error_kind, error_message, run_id),
            )
            if cursor.rowcount != 1:
                raise DefinitionError(f'run not found: {run_id}')

    async def run_state(self, run_id: str) -> StoredRunState | None:
        _text(run_id, 'run_id')
        async with self._lock:
            cursor = await self._connection.execute(
                'SELECT status, error_kind, error_message FROM run_states WHERE run_id = ?',
                (run_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        status = cast(RunStatus, row['status'])
        error = (
            None
            if status != 'failed'
            else RuntimeErrorInfo(row['error_kind'], row['error_message'])
        )
        return StoredRunState(status, error)

    async def run_ids(self) -> tuple[str, ...]:
        async with self._lock:
            cursor = await self._connection.execute(
                'SELECT run_id FROM run_states ORDER BY run_id'
            )
            return tuple(row['run_id'] for row in await cursor.fetchall())

    # Physical execution attempts and progress

    async def create_attempt(self, run_id: str, attempt_id: str, invocation_id: str, operation_id: str,
                             partition_key: str, input_refs: Iterable[ArtifactRef] = (),
                             output_keys: Iterable[ArtifactKey] = ()
                             ) -> AttemptSnapshot:
        for value, name in (
            (run_id, 'run_id'),
            (attempt_id, 'attempt_id'),
            (invocation_id, 'invocation_id'),
            (operation_id, 'operation_id'),
        ):
            _text(value, name)
        created_at = time.time()
        snapshot = AttemptSnapshot(
            attempt_id,
            invocation_id,
            operation_id,
            partition_key,
            'scheduled',
            created_at,
            input_refs=tuple(input_refs),
            output_keys=tuple(output_keys),
        )
        async with self._transaction():
            await self._require_run(run_id)
            try:
                await self._connection.execute(
                    """
                    INSERT INTO execution_attempts(
                      run_id, attempt_id, invocation_id, operation_id, partition_key,
                      status, created_at, started_at, finished_at, error_kind, error_message,
                      input_refs_json, output_keys_json
                    ) VALUES (?, ?, ?, ?, ?, 'scheduled', ?, NULL, NULL, '', '', ?, ?)
                    """,
                    (
                        run_id,
                        attempt_id,
                        invocation_id,
                        operation_id,
                        partition_key,
                        created_at,
                        _refs_json(snapshot.input_refs),
                        json.dumps(
                            [_key_data(key) for key in snapshot.output_keys],
                            separators=(',', ':'),
                        ),
                    ),
                )
            except aiosqlite.IntegrityError as exc:
                raise DefinitionError(
                    f'attempt conflicts with existing execution: {attempt_id}'
                ) from exc
        return snapshot

    async def set_attempt_status(self, run_id: str, attempt_id: str, status: AttemptStatus, *,
                                 error: RuntimeErrorInfo | None = None
                                 ) -> AttemptSnapshot:
        _text(run_id, 'run_id')
        _text(attempt_id, 'attempt_id')
        if status in {'succeeded', 'discarded'}:
            raise DefinitionError(
                f'{status} attempt status is owned by artifact commit'
            )
        async with self._transaction():
            current = await self._attempt_row(run_id, attempt_id)
            if current is None:
                raise DefinitionError(f'attempt not found: {attempt_id}')
            _validate_attempt_transition(current['status'], status)
            if current['status'] == status:
                snapshot = _attempt_snapshot(current)
                if error is not None and error != snapshot.error:
                    raise DefinitionError('attempt terminal state cannot change its error')
                return snapshot
            now = time.time()
            started_at = current['started_at']
            finished_at = current['finished_at']
            if status == 'running' and started_at is None:
                started_at = now
            if status in {'cancelled', 'succeeded', 'failed', 'interrupted', 'discarded'}:
                finished_at = now
            error_kind = '' if error is None else error.kind
            error_message = '' if error is None else error.message
            if status == 'failed' and error is None:
                raise DefinitionError('failed attempt requires error details')
            if status != 'failed' and error is not None:
                raise DefinitionError('attempt error is only valid for failed status')
            await self._connection.execute(
                """
                UPDATE execution_attempts
                SET status = ?, started_at = ?, finished_at = ?,
                    error_kind = ?, error_message = ?
                WHERE run_id = ? AND attempt_id = ?
                """,
                (
                    status,
                    started_at,
                    finished_at,
                    error_kind,
                    error_message,
                    run_id,
                    attempt_id,
                ),
            )
            row = dict(current)
            row.update({
                'status': status,
                'started_at': started_at,
                'finished_at': finished_at,
                'error_kind': error_kind,
                'error_message': error_message,
            })
        return _attempt_snapshot(row)

    async def recover_attempts(self, run_id: str, status: Literal['cancelled', 'interrupted']
                               ) -> tuple[AttemptSnapshot, ...]:
        _text(run_id, 'run_id')
        if status not in {'cancelled', 'interrupted'}:
            raise DefinitionError('recovered attempt status must be cancelled or interrupted')
        async with self._transaction():
            cursor = await self._connection.execute(
                """
                SELECT * FROM execution_attempts
                WHERE run_id = ? AND status IN ('scheduled', 'running', 'cancelling')
                ORDER BY created_at, attempt_id
                """,
                (run_id,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            if rows:
                finished_at = time.time()
                await self._connection.execute(
                    """
                    UPDATE execution_attempts
                    SET status = ?, finished_at = ?
                    WHERE run_id = ? AND status IN ('scheduled', 'running', 'cancelling')
                    """,
                    (status, finished_at, run_id),
                )
                for row in rows:
                    row['status'] = status
                    row['finished_at'] = finished_at
        return tuple(_attempt_snapshot(row) for row in rows)

    async def attempts(self, run_id: str) -> tuple[AttemptSnapshot, ...]:
        _text(run_id, 'run_id')
        async with self._lock:
            cursor = await self._connection.execute(
                """
                SELECT * FROM execution_attempts
                WHERE run_id = ? ORDER BY created_at, attempt_id
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
        return tuple(_attempt_snapshot(row) for row in rows)

    async def append_progress(self, run_id: str, attempt_id: str, update: ProgressUpdate) -> ProgressEvent:
        _text(run_id, 'run_id')
        _text(attempt_id, 'attempt_id')
        if not isinstance(update, ProgressUpdate):
            raise TypeError('update must be ProgressUpdate')
        detail_json = json.dumps(dict(update.detail), ensure_ascii=False, sort_keys=True)
        created_at = time.time()
        async with self._transaction():
            current = await self._attempt_row(run_id, attempt_id)
            if current is None:
                raise DefinitionError(f'attempt not found: {attempt_id}')
            if current['status'] != 'running':
                raise DefinitionError('progress can only be appended to a running attempt')
            cursor = await self._connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence
                FROM execution_events WHERE run_id = ? AND attempt_id = ?
                """,
                (run_id, attempt_id),
            )
            row = await cursor.fetchone()
            sequence = int(row['sequence'])
            await self._connection.execute(
                """
                INSERT INTO execution_events(
                  run_id, attempt_id, sequence, phase, message,
                  current_value, total_value, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    attempt_id,
                    sequence,
                    update.phase,
                    update.message,
                    update.current,
                    update.total,
                    detail_json,
                    created_at,
                ),
            )
        return ProgressEvent(attempt_id, sequence, update, created_at)

    async def progress_events(self, run_id: str, attempt_id: str | None = None
                              ) -> tuple[ProgressEvent, ...]:
        _text(run_id, 'run_id')
        parameters: tuple[object, ...]
        statement = 'SELECT * FROM execution_events WHERE run_id = ?'
        parameters = (run_id,)
        if attempt_id is not None:
            _text(attempt_id, 'attempt_id')
            statement += ' AND attempt_id = ?'
            parameters = (run_id, attempt_id)
        statement += ' ORDER BY created_at, attempt_id, sequence'
        async with self._lock:
            cursor = await self._connection.execute(statement, parameters)
            rows = await cursor.fetchall()
        return tuple(_progress_event(row) for row in rows)

    async def _attempt_row(self, run_id: str, attempt_id: str) -> aiosqlite.Row | None:
        cursor = await self._connection.execute(
            'SELECT * FROM execution_attempts WHERE run_id = ? AND attempt_id = ?',
            (run_id, attempt_id),
        )
        return await cursor.fetchone()

    async def delete_run(self, run_id: str) -> None:
        _text(run_id, 'run_id')
        async with self._transaction():
            await self._connection.execute(
                'DELETE FROM run_states WHERE run_id = ?',
                (run_id,),
            )

    # Atomic commit protocol

    async def _commit(self, prepared: _PreparedCommit, *, attempt_id: str | None = None) -> CommitResult:
        async with self._transaction():
            await self._require_run(prepared.run_id)
            attempt = None
            if attempt_id is not None:
                attempt = await self._attempt_row(prepared.run_id, attempt_id)
                if attempt is None:
                    raise DefinitionError(f'attempt not found: {attempt_id}')
                _validate_attempt_commit(attempt, prepared.command)

            replay = await self._replay(prepared)
            if replay is not None:
                if attempt_id is not None and attempt is not None:
                    await self._reconcile_replayed_attempt(
                        prepared.run_id,
                        attempt_id,
                        attempt['status'],
                    )
                return replay

            if attempt is not None:
                if attempt['status'] != 'running':
                    return CommitResult('stale')

            snapshot = await self._snapshot(prepared.run_id)
            result = (
                CommitResult('stale')
                if not await self._commit_is_current(
                    prepared.run_id,
                    prepared.command,
                    snapshot,
                )
                else await self._apply_commit(prepared.run_id, prepared)
            )
            if result.status == 'ok':
                await self._connection.execute(
                    """
                    INSERT INTO commit_receipts(
                      run_id, commit_id, request_hash, refs_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        prepared.run_id,
                        prepared.command.commit_id,
                        prepared.request_hash,
                        _refs_json(result.refs),
                    ),
                )
            if attempt_id is not None:
                await self._finish_attempt_commit(
                    prepared.run_id,
                    attempt_id,
                    result.status,
                )
            return result

    async def _require_run(self, run_id: str) -> None:
        cursor = await self._connection.execute(
            'SELECT 1 FROM run_states WHERE run_id = ?',
            (run_id,),
        )
        if await cursor.fetchone() is None:
            raise DefinitionError(f'run not found: {run_id}')

    async def _reconcile_replayed_attempt(self, run_id: str, attempt_id: str, attempt_status: str) -> None:
        if attempt_status == 'succeeded':
            return
        if attempt_status == 'running':
            await self._finish_attempt_commit(run_id, attempt_id, 'ok')
            return
        raise DefinitionError(
            f'replayed commit conflicts with attempt state: '
            f'{attempt_id} is {attempt_status}, expected succeeded'
        )

    async def _finish_attempt_commit(self, run_id: str, attempt_id: str,
                                     commit_status: Literal['ok', 'stale']
                                     ) -> None:
        status = 'succeeded' if commit_status == 'ok' else 'discarded'
        cursor = await self._connection.execute(
            """
            UPDATE execution_attempts
            SET status = ?, finished_at = ?
            WHERE run_id = ? AND attempt_id = ? AND status = 'running'
            """,
            (status, time.time(), run_id, attempt_id),
        )
        if cursor.rowcount != 1:
            raise DefinitionError(f'attempt is no longer running: {attempt_id}')

    async def _replay(self, prepared: _PreparedCommit) -> CommitResult | None:
        cursor = await self._connection.execute(
            """
            SELECT request_hash, refs_json FROM commit_receipts
            WHERE run_id = ? AND commit_id = ?
            """,
            (prepared.run_id, prepared.command.commit_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row['request_hash'] != prepared.request_hash:
            raise DefinitionError(
                f'commit id reused with different request: {prepared.command.commit_id}'
            )
        return CommitResult(
            'ok',
            _refs_from_json(row['refs_json']),
            replayed=True,
        )

    async def _commit_is_current(self, run_id: str, commit: ArtifactCommit, snapshot: ArtifactSnapshot) -> bool:
        for key, expected in commit.expected_heads.items():
            current = snapshot.records.get(key)
            if expected is None:
                if current is not None:
                    return False
            elif current is None or current.ref != expected:
                return False

        effective = snapshot.effective_records()
        if any(
            effective.get(ref.key) is None or effective[ref.key].ref != ref
            for write in commit.writes
            for ref in write.input_refs
        ):
            return False

        for guard in commit.partition_guards:
            record = effective.get(guard.partition_set_key)
            if record is None:
                return False
            found, value = await self._read(run_id, record.ref)
            if not found or not isinstance(value, PartitionSet):
                return False
            if guard.partition_key not in value:
                return False
        return True

    async def _apply_commit(self, run_id: str, prepared: _PreparedCommit) -> CommitResult:
        commit = prepared.command
        records: list[ArtifactRecord] = []
        partition_sets: dict[ArtifactKey, PartitionSet] = {}
        for write, payload in zip(commit.writes, prepared.payloads, strict=True):
            ref = await self._next_ref(run_id, write.key)
            record = ArtifactRecord(ref, commit.producer, write.input_refs)
            await self._insert_record(run_id, record, payload)
            await self._connection.execute(
                """
                INSERT INTO artifact_heads(run_id, artifact_id, partition_key, version)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, artifact_id, partition_key)
                DO UPDATE SET version = excluded.version
                """,
                (
                    run_id,
                    ref.key.artifact_id,
                    ref.key.partition_key,
                    ref.version,
                ),
            )
            records.append(record)
            if isinstance(write.value, PartitionSet):
                partition_sets[write.key] = write.value
        return CommitResult(
            'ok',
            tuple(record.ref for record in records),
            ArtifactChangeSet(
                tuple(records),
                partition_sets,
            ),
        )

    # SQLite representation

    async def _snapshot(self, run_id: str, partition_set_ids: frozenset[str] = frozenset()
                        ) -> ArtifactSnapshot:
        cursor = await self._connection.execute(
            """
            SELECT r.artifact_id, r.partition_key, r.version,
                   r.producer, r.input_refs_json
            FROM artifact_heads h
            JOIN artifact_records r
              ON r.run_id = h.run_id
             AND r.artifact_id = h.artifact_id
             AND r.partition_key = h.partition_key
             AND r.version = h.version
            WHERE h.run_id = ?
            """,
            (run_id,),
        )
        records = {}
        for row in await cursor.fetchall():
            key = ArtifactKey(row['artifact_id'], row['partition_key'])
            ref = ArtifactRef(key, row['version'])
            records[key] = ArtifactRecord(
                ref,
                row['producer'],
                _refs_from_json(row['input_refs_json']),
            )
        partition_sets: dict[ArtifactKey, PartitionSet] = {}
        for key, record in records.items():
            if key.artifact_id not in partition_set_ids or key.partition_key:
                continue
            found, value = await self._read(run_id, record.ref)
            if not found or not isinstance(value, PartitionSet):
                raise DefinitionError(
                    f'{key.artifact_id} must contain a PartitionSet value'
                )
            partition_sets[key] = value
        return ArtifactSnapshot(records, partition_sets)

    async def _read(self, run_id: str, ref: ArtifactRef) -> tuple[bool, object]:
        cursor = await self._connection.execute(
            """
            SELECT payload FROM artifact_payloads
            WHERE run_id = ? AND artifact_id = ? AND partition_key = ? AND version = ?
            """,
            (run_id, ref.key.artifact_id, ref.key.partition_key, ref.version),
        )
        row = await cursor.fetchone()
        if row is None:
            return False, None
        return True, await asyncio.to_thread(pickle.loads, row['payload'])

    async def _record(self, run_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        cursor = await self._connection.execute(
            """
            SELECT producer, input_refs_json
            FROM artifact_records
            WHERE run_id = ? AND artifact_id = ? AND partition_key = ? AND version = ?
            """,
            (run_id, ref.key.artifact_id, ref.key.partition_key, ref.version),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ArtifactRecord(
            ref,
            row['producer'],
            _refs_from_json(row['input_refs_json']),
        )

    async def _next_ref(self, run_id: str, key: ArtifactKey) -> ArtifactRef:
        cursor = await self._connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS version
            FROM artifact_records
            WHERE run_id = ? AND artifact_id = ? AND partition_key = ?
            """,
            (run_id, key.artifact_id, key.partition_key),
        )
        row = await cursor.fetchone()
        return ArtifactRef(key, row['version'])

    async def _insert_record(self, run_id: str, record: ArtifactRecord, payload: bytes) -> None:
        ref = record.ref
        await self._connection.execute(
            """
            INSERT INTO artifact_records(
              run_id, artifact_id, partition_key, version, producer, input_refs_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, ref.key.artifact_id, ref.key.partition_key, ref.version,
                record.producer,
                _refs_json(record.input_refs),
            ),
        )
        await self._connection.execute(
            """
            INSERT INTO artifact_payloads(run_id, artifact_id, partition_key, version, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, ref.key.artifact_id, ref.key.partition_key, ref.version, payload),
        )

    async def _create_schema(self) -> None:
        cursor = await self._connection.execute('PRAGMA user_version')
        row = await cursor.fetchone()
        version = int(row[0])
        if version == _SCHEMA_VERSION:
            return
        if version != 0:
            raise DefinitionError(
                f'unsupported artifact store schema version: {version}'
            )

        cursor = await self._connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        )
        if await cursor.fetchone() is not None:
            raise DefinitionError(
                'unversioned artifact store is not supported; delete and recreate it'
            )

        await self._connection.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE run_states(
              run_id TEXT PRIMARY KEY,
              status TEXT NOT NULL CHECK(status IN (
                'created', 'running', 'pausing', 'paused',
                'cancelling', 'cancelled', 'failed', 'completed'
              )),
              error_kind TEXT NOT NULL,
              error_message TEXT NOT NULL,
              CHECK(
                (status = 'failed' AND trim(error_kind) != '' AND trim(error_message) != '')
                OR (status != 'failed' AND error_kind = '' AND error_message = '')
              )
            );
            CREATE TABLE artifact_records(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              partition_key TEXT NOT NULL,
              version INTEGER NOT NULL,
              producer TEXT NOT NULL,
              input_refs_json TEXT NOT NULL,
              PRIMARY KEY(run_id, artifact_id, partition_key, version),
              FOREIGN KEY(run_id) REFERENCES run_states(run_id) ON DELETE CASCADE,
              CHECK(version > 0)
            );
            CREATE TABLE artifact_payloads(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              partition_key TEXT NOT NULL,
              version INTEGER NOT NULL,
              payload BLOB NOT NULL,
              PRIMARY KEY(run_id, artifact_id, partition_key, version),
              FOREIGN KEY(run_id, artifact_id, partition_key, version)
                REFERENCES artifact_records(run_id, artifact_id, partition_key, version)
                ON DELETE CASCADE
            );
            CREATE TABLE artifact_heads(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              partition_key TEXT NOT NULL,
              version INTEGER NOT NULL,
              PRIMARY KEY(run_id, artifact_id, partition_key),
              FOREIGN KEY(run_id, artifact_id, partition_key, version)
                REFERENCES artifact_records(run_id, artifact_id, partition_key, version)
                ON DELETE CASCADE
            );
            CREATE TABLE commit_receipts(
              run_id TEXT NOT NULL,
              commit_id TEXT NOT NULL,
              request_hash TEXT NOT NULL,
              refs_json TEXT NOT NULL,
              PRIMARY KEY(run_id, commit_id),
              FOREIGN KEY(run_id) REFERENCES run_states(run_id) ON DELETE CASCADE
            );
            CREATE TABLE execution_attempts(
              run_id TEXT NOT NULL,
              attempt_id TEXT NOT NULL,
              invocation_id TEXT NOT NULL,
              operation_id TEXT NOT NULL,
              partition_key TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN (
                'scheduled', 'running', 'cancelling', 'cancelled',
                'succeeded', 'failed', 'interrupted', 'discarded'
              )),
              created_at REAL NOT NULL,
              started_at REAL,
              finished_at REAL,
              error_kind TEXT NOT NULL,
              error_message TEXT NOT NULL,
              input_refs_json TEXT NOT NULL,
              output_keys_json TEXT NOT NULL,
              PRIMARY KEY(run_id, attempt_id),
              FOREIGN KEY(run_id) REFERENCES run_states(run_id) ON DELETE CASCADE,
              CHECK(
                (status = 'failed' AND trim(error_kind) != '' AND trim(error_message) != '')
                OR (status != 'failed' AND error_kind = '' AND error_message = '')
              ),
              CHECK(
                (status IN ('scheduled', 'running', 'cancelling') AND finished_at IS NULL)
                OR (status IN (
                  'cancelled', 'succeeded', 'failed', 'interrupted', 'discarded'
                ) AND finished_at IS NOT NULL)
              ),
              CHECK(
                status NOT IN ('running', 'succeeded', 'failed', 'discarded')
                OR started_at IS NOT NULL
              )
            );
            CREATE INDEX execution_attempts_by_run_status
              ON execution_attempts(run_id, status, created_at);
            CREATE UNIQUE INDEX active_attempt_by_invocation
              ON execution_attempts(run_id, invocation_id)
              WHERE status IN ('scheduled', 'running', 'cancelling');
            CREATE TABLE execution_events(
              run_id TEXT NOT NULL,
              attempt_id TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              phase TEXT NOT NULL,
              message TEXT NOT NULL,
              current_value INTEGER,
              total_value INTEGER,
              detail_json TEXT NOT NULL,
              created_at REAL NOT NULL,
              PRIMARY KEY(run_id, attempt_id, sequence),
              FOREIGN KEY(run_id, attempt_id)
                REFERENCES execution_attempts(run_id, attempt_id) ON DELETE CASCADE,
              CHECK(sequence > 0),
              CHECK(current_value IS NULL OR current_value >= 0),
              CHECK(total_value IS NULL OR total_value >= 0),
              CHECK(
                current_value IS NULL OR total_value IS NULL OR current_value <= total_value
              )
            );
            PRAGMA user_version = {_SCHEMA_VERSION};
            COMMIT;
            """
        )

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        async with self._lock:
            try:
                await self._connection.execute('BEGIN IMMEDIATE')
                yield
                await self._connection.commit()
            except BaseException:
                rollback = asyncio.create_task(self._connection.rollback())
                while not rollback.done():
                    try:
                        await asyncio.shield(rollback)
                    except asyncio.CancelledError:
                        continue
                await rollback
                raise


def _validate_attempt_transition(current: str, target: AttemptStatus) -> None:
    if current == target:
        return
    if target not in _PUBLIC_ATTEMPT_TRANSITIONS.get(current, frozenset()):
        raise DefinitionError(f'cannot transition attempt from {current} to {target}')


def _validate_attempt_commit(attempt: Mapping[str, object], commit: ArtifactCommit) -> None:
    attempt_id = attempt['attempt_id']
    if attempt['invocation_id'] != commit.commit_id:
        raise DefinitionError(
            f'attempt {attempt_id} does not belong to commit {commit.commit_id}'
        )

    expected_producer = f'operation:{attempt["operation_id"]}'
    if commit.producer != expected_producer:
        raise DefinitionError(
            f'attempt {attempt_id} requires producer {expected_producer}'
        )

    input_refs = _refs_from_json(attempt['input_refs_json'])
    if any(write.input_refs != input_refs for write in commit.writes):
        raise DefinitionError(
            f'attempt {attempt_id} input refs do not match commit lineage'
        )

    declared_outputs = {
        ArtifactKey(item[0], item[1])
        for item in json.loads(attempt['output_keys_json'])
    }
    if not declared_outputs.issubset(commit.output_keys):
        raise DefinitionError(
            f'attempt {attempt_id} declared outputs are missing from commit'
        )

    partition_key = attempt['partition_key']
    if partition_key and not any(
        guard.partition_key == partition_key
        for guard in commit.partition_guards
    ):
        raise DefinitionError(
            f'attempt {attempt_id} partition guard does not match commit'
        )


def _attempt_snapshot(row: Mapping[str, object]) -> AttemptSnapshot:
    status = cast(AttemptStatus, row['status'])
    error = None
    if status == 'failed':
        error = RuntimeErrorInfo(row['error_kind'], row['error_message'])
    return AttemptSnapshot(
        row['attempt_id'],
        row['invocation_id'],
        row['operation_id'],
        row['partition_key'],
        status,
        row['created_at'],
        row['started_at'],
        row['finished_at'],
        error,
        _refs_from_json(row['input_refs_json']),
        tuple(
            ArtifactKey(item[0], item[1])
            for item in json.loads(row['output_keys_json'])
        ),
    )


def _progress_event(row: Mapping[str, object]) -> ProgressEvent:
    update = ProgressUpdate(
        row['phase'],
        row['message'],
        row['current_value'],
        row['total_value'],
        json.loads(row['detail_json']),
    )
    return ProgressEvent(
        row['attempt_id'],
        row['sequence'],
        update,
        row['created_at'],
    )


def _key_data(key: ArtifactKey) -> list[str]:
    return [key.artifact_id, key.partition_key]


def _ref_data(ref: ArtifactRef | None) -> list[object] | None:
    return None if ref is None else [
        ref.key.artifact_id,
        ref.key.partition_key,
        ref.version,
    ]


def _prepare_commit(run_id: str, commit: ArtifactCommit) -> _PreparedCommit:
    payloads = tuple(
        pickle.dumps(write.value, protocol=pickle.HIGHEST_PROTOCOL)
        for write in commit.writes
    )
    return _PreparedCommit(
        run_id,
        commit,
        payloads,
        _commit_fingerprint(run_id, commit, payloads),
    )


def _deserialize_many(refs: tuple[ArtifactRef, ...], payloads: Mapping[ArtifactRef, bytes]
                      ) -> dict[ArtifactRef, object]:
    return {ref: pickle.loads(payloads[ref]) for ref in refs}


def _commit_fingerprint(run_id: str, commit: ArtifactCommit, payloads: tuple[bytes, ...]) -> str:
    writes = [
        [
            *_key_data(write.key),
            hashlib.sha256(payload).hexdigest(),
            [_ref_data(ref) for ref in write.input_refs],
        ]
        for write, payload in zip(commit.writes, payloads, strict=True)
    ]
    expected = [
        [
            *_key_data(key),
            _ref_data(ref),
        ]
        for key, ref in sorted(commit.expected_heads.items())
    ]
    guards = [
        [*_key_data(guard.partition_set_key), guard.partition_key]
        for guard in sorted(commit.partition_guards)
    ]
    encoded = json.dumps((
        'commit',
        run_id,
        commit.commit_id,
        commit.producer,
        writes,
        expected,
        guards,
    ), sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _refs_json(refs: tuple[ArtifactRef, ...]) -> str:
    values = [
        [ref.key.artifact_id, ref.key.partition_key, ref.version]
        for ref in refs
    ]
    return json.dumps(values, separators=(',', ':'))


def _refs_from_json(value: str) -> tuple[ArtifactRef, ...]:
    return tuple(
        ArtifactRef(ArtifactKey(item[0], item[1]), item[2])
        for item in json.loads(value)
    )


__all__ = ['ArtifactStore', 'CommitResult', 'StoredRunState']
