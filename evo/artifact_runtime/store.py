from __future__ import annotations

import asyncio
import hashlib
import json
import pickle
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

from .artifact import (
    ArtifactKey,
    ArtifactMutation,
    ArtifactRecord,
    ArtifactRef,
    ArtifactSnapshot,
    CollectionItem,
    CollectionMutation,
    CollectionSnapshot,
    OperationWriteSet,
    merge_refs,
)
from .errors import DefinitionError
from .planning import CollectionProjection
from .utils import _text


@dataclass(frozen=True)
class StoredRunState:
    status: str
    error_kind: str = ''
    error_message: str = ''


@dataclass(frozen=True)
class CommitResult:
    status: Literal['ok', 'stale']
    refs: tuple[ArtifactRef, ...] = ()

    def to_json(self) -> str:
        value = {'status': self.status, 'refs': [_ref_data(ref) for ref in self.refs]}
        return json.dumps(value, separators=(',', ':'))

    @classmethod
    def from_json(cls, value: str) -> CommitResult:
        data = json.loads(value)
        refs = tuple(
            ArtifactRef(ArtifactKey(str(item[0]), str(item[1])), int(item[2]))
            for item in data['refs']
        )
        return cls(data['status'], refs)


@dataclass(frozen=True)
class CommitIdentity:
    run_id: str
    replay_key: str
    request_hash: str


@dataclass(frozen=True)
class ExternalOrigin:
    pass


@dataclass(frozen=True)
class OperationOrigin:
    producer_operation: str

    def __post_init__(self) -> None:
        _text(self.producer_operation, 'producer_operation')


WriteOrigin = ExternalOrigin | OperationOrigin


@dataclass(frozen=True)
class ValueWrite:
    key: ArtifactKey
    payload: bytes
    input_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.key, ArtifactKey):
            raise TypeError('value write key must be ArtifactKey')
        if not isinstance(self.payload, bytes):
            raise TypeError('value write payload must be bytes')
        object.__setattr__(self, 'input_refs', merge_refs(self.input_refs))


@dataclass(frozen=True)
class CollectionMemberWrite:
    key: str
    source: ArtifactRef | ArtifactKey

    def __post_init__(self) -> None:
        _text(self.key, 'collection member key')
        source_key = self.source.key if isinstance(self.source, ArtifactRef) else self.source
        if not isinstance(source_key, ArtifactKey):
            raise TypeError('collection member source must be ArtifactRef or ArtifactKey')
        if source_key.item_key != self.key:
            raise DefinitionError('collection member source must match its item key')


@dataclass(frozen=True)
class CollectionManifestWrite:
    key: ArtifactKey
    item_artifact_id: str
    members: tuple[CollectionMemberWrite, ...]
    input_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.key, ArtifactKey) or self.key.item_key:
            raise DefinitionError('collection manifest key must be a scalar ArtifactKey')
        _text(self.item_artifact_id, 'collection manifest item_artifact_id')
        members = tuple(self.members)
        if len({member.key for member in members}) != len(members):
            raise DefinitionError('collection manifest item keys must be unique')
        for member in members:
            source_key = member.source.key if isinstance(member.source, ArtifactRef) else member.source
            if source_key.artifact_id != self.item_artifact_id:
                raise DefinitionError('collection members do not match item_artifact_id')
        object.__setattr__(self, 'members', members)
        object.__setattr__(self, 'input_refs', merge_refs(self.input_refs))


@dataclass(frozen=True)
class ResolvedWriteSet:
    origin: WriteOrigin
    values: tuple[ValueWrite, ...]
    collections: tuple[CollectionManifestWrite, ...]
    result_keys: tuple[ArtifactKey, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.origin, (ExternalOrigin, OperationOrigin)):
            raise TypeError('write origin must be ExternalOrigin or OperationOrigin')
        values = tuple(self.values)
        collections = tuple(self.collections)
        written_keys = tuple(write.key for write in values) + tuple(
            write.key for write in collections
        )
        if len(set(written_keys)) != len(written_keys):
            raise DefinitionError('write set artifact keys must be unique')
        value_keys = {write.key for write in values}
        if any(
            isinstance(member.source, ArtifactKey) and member.source not in value_keys
            for collection in collections
            for member in collection.members
        ):
            raise DefinitionError('collection member write must reference a value in the write set')
        result_keys = tuple(self.result_keys)
        if len(result_keys) != len(written_keys) or set(result_keys) != set(written_keys):
            raise DefinitionError('write set result keys must contain every written artifact once')
        object.__setattr__(self, 'values', values)
        object.__setattr__(self, 'collections', collections)
        object.__setattr__(self, 'result_keys', result_keys)

    @property
    def written_keys(self) -> tuple[ArtifactKey, ...]:
        return tuple(write.key for write in self.values) + tuple(
            write.key for write in self.collections
        )


CommitResolver = Callable[[ArtifactSnapshot], ResolvedWriteSet | None]


class ArtifactStore:
    def __init__(self, root: Path, connection: aiosqlite.Connection) -> None:
        self.root = root
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, root: str | Path) -> ArtifactStore:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path / 'artifact-runtime.sqlite3')
        connection.row_factory = aiosqlite.Row
        await connection.execute('PRAGMA foreign_keys = ON')
        await connection.execute('PRAGMA journal_mode = WAL')
        await connection.execute('PRAGMA synchronous = FULL')
        store = cls(path, connection)
        await store._create_schema()
        return store

    async def close(self) -> None:
        await self._connection.close()

    # Artifact commits

    async def commit_external(
        self, run_id: str, key: ArtifactKey, value: object, *,
        idempotency_key: str, expected_ref: ArtifactRef | None = None,
    ) -> CommitResult:
        _text(run_id, 'run_id')
        _text(idempotency_key, 'idempotency_key')
        if not isinstance(key, ArtifactKey):
            raise TypeError('key must be ArtifactKey')
        if expected_ref is not None and expected_ref.key != key:
            raise DefinitionError('expected_ref must identify the committed artifact key')

        payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        identity = CommitIdentity(
            run_id,
            f'external:{idempotency_key}',
            _fingerprint(
                'external', run_id, _key_data(key), _ref_data(expected_ref), _digest(payload)
            ),
        )
        writes = ResolvedWriteSet(
            ExternalOrigin(),
            (ValueWrite(key, payload),),
            (),
            (key,),
        )

        def resolve(snapshot: ArtifactSnapshot) -> ResolvedWriteSet | None:
            current = snapshot.records.get(key)
            if expected_ref is not None and (
                current is None or current.ref != expected_ref
            ):
                return None
            return writes

        return await self._commit(identity, resolve)

    async def commit_operation(
        self, run_id: str, operation: OperationWriteSet,
    ) -> CommitResult:
        _text(run_id, 'run_id')
        if not isinstance(operation, OperationWriteSet):
            raise TypeError('operation must be OperationWriteSet')

        origin = OperationOrigin(operation.producer_operation)
        values: list[ValueWrite] = []
        collections: list[CollectionManifestWrite] = []
        result_keys: list[ArtifactKey] = []
        for key, value in operation.scalar_values.items():
            values.append(ValueWrite(
                key,
                pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL),
                operation.input_refs,
            ))
            result_keys.append(key)
        for collection in operation.collection_writes:
            members = []
            for item_key, value in collection.items.items():
                key = ArtifactKey.item(collection.item_artifact_id, item_key)
                values.append(ValueWrite(
                    key,
                    pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL),
                    operation.input_refs,
                ))
                members.append(CollectionMemberWrite(item_key, key))
                result_keys.append(key)
            collections.append(CollectionManifestWrite(
                collection.key,
                collection.item_artifact_id,
                tuple(members),
                operation.input_refs,
            ))
            result_keys.append(collection.key)
        writes = ResolvedWriteSet(
            origin,
            tuple(values),
            tuple(collections),
            tuple(result_keys),
        )
        identity = CommitIdentity(
            run_id,
            f'operation:{operation.commit_id}',
            _fingerprint(
                'operation',
                run_id,
                operation.commit_id,
                _origin_data(origin),
                [_ref_data(ref) for ref in operation.input_refs],
                [
                    [*_key_data(guard.collection_key), guard.item.key, _ref_data(guard.item.ref)]
                    for guard in operation.item_guards
                ],
                [
                    [
                        _ref_data(guard.ref),
                        guard.item_artifact_id,
                        [[item.key, _ref_data(item.ref)] for item in guard.items],
                    ]
                    for guard in operation.collection_guards
                ],
                _writes_data(writes),
            ),
        )

        def resolve(snapshot: ArtifactSnapshot) -> ResolvedWriteSet | None:
            effective = snapshot.effective_records()
            if any(
                effective.get(ref.key) is None or effective[ref.key].ref != ref
                for ref in operation.input_refs
            ):
                return None
            for guard in operation.item_guards:
                current = snapshot.collections.get(guard.collection_key)
                if (
                    current is None
                    or effective.get(current.ref.key) is None
                    or effective.get(guard.item.ref.key) is None
                    or effective[guard.item.ref.key].ref != guard.item.ref
                    or not any(
                        item.key == guard.item.key and item.ref == guard.item.ref
                        for item in current.items
                    )
                ):
                    return None
            for guard in operation.collection_guards:
                current = snapshot.collections.get(guard.ref.key)
                if current != guard or effective.get(guard.ref.key) is None:
                    return None
            if any(key in effective for key in writes.written_keys):
                return None
            return writes

        return await self._commit(identity, resolve)

    async def commit_projection(
        self, run_id: str, projection: CollectionProjection,
    ) -> CommitResult:
        _text(run_id, 'run_id')
        if not isinstance(projection, CollectionProjection):
            raise TypeError('projection must be CollectionProjection')

        origin = OperationOrigin(projection.producer_operation)
        manifest = CollectionManifestWrite(
            projection.collection_key,
            projection.item_artifact_id,
            tuple(CollectionMemberWrite(item.key, item.ref) for item in projection.items),
            projection.input_refs,
        )
        writes = ResolvedWriteSet(
            origin,
            (),
            (manifest,),
            (projection.collection_key,),
        )
        request_hash = _fingerprint(
            'collection', run_id, _origin_data(origin), _writes_data(writes)
        )
        identity = CommitIdentity(
            run_id,
            f'collection:{request_hash}',
            request_hash,
        )

        def resolve(snapshot: ArtifactSnapshot) -> ResolvedWriteSet | None:
            effective = snapshot.effective_records()
            inputs_are_current = all(
                effective.get(ref.key) is not None and effective[ref.key].ref == ref
                for ref in manifest.input_refs
            )
            members_are_current = all(
                isinstance(member.source, ArtifactRef)
                and effective.get(member.source.key) is not None
                and effective[member.source.key].ref == member.source
                for member in manifest.members
            )
            if not inputs_are_current or not members_are_current or manifest.key in effective:
                return None
            return writes

        return await self._commit(identity, resolve)

    async def mutate_collection(
        self, run_id: str, mutation: CollectionMutation, *, idempotency_key: str,
    ) -> CommitResult:
        _text(run_id, 'run_id')
        _text(idempotency_key, 'idempotency_key')
        if not isinstance(mutation, CollectionMutation):
            raise TypeError('mutation must be CollectionMutation')

        item_writes = tuple(
            ValueWrite(
                ArtifactKey.item(mutation.item_artifact_id, key),
                pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL),
            )
            for key, value in mutation.upserts.items()
        )
        identity = CommitIdentity(
            run_id,
            f'mutation:{idempotency_key}',
            _fingerprint(
                'mutation', run_id, mutation.collection_id, mutation.item_artifact_id,
                _ref_data(mutation.expected_ref), list(mutation.deletes),
                [[*_key_data(write.key), _digest(write.payload)] for write in item_writes],
            ),
        )

        def resolve(snapshot: ArtifactSnapshot) -> ResolvedWriteSet | None:
            collection_key = ArtifactKey.scalar(mutation.collection_id)
            current = snapshot.collections.get(collection_key)
            if mutation.expected_ref is not None and (
                current is None or current.ref != mutation.expected_ref
            ):
                return None
            if current is not None and collection_key not in snapshot.effective_records():
                raise DefinitionError('current collection is not effective')
            if current is not None and current.item_artifact_id != mutation.item_artifact_id:
                raise DefinitionError('collection mutation item artifact type does not match')

            current_items = () if current is None else current.items
            current_by_key = {item.key: item for item in current_items}
            missing = set(mutation.deletes) - set(current_by_key)
            if missing:
                raise DefinitionError(
                    f'collection delete keys do not exist: {sorted(missing)}'
                )

            item_write_by_key = {write.key.item_key: write for write in item_writes}
            members = [
                CollectionMemberWrite(
                    item.key,
                    item_write_by_key[item.key].key
                    if item.key in item_write_by_key
                    else item.ref,
                )
                for item in current_items
                if item.key not in mutation.deletes
            ]
            members.extend(
                CollectionMemberWrite(key, item_write_by_key[key].key)
                for key in mutation.upserts
                if key not in current_by_key
            )
            return ResolvedWriteSet(
                ExternalOrigin(),
                item_writes,
                (CollectionManifestWrite(
                    collection_key,
                    mutation.item_artifact_id,
                    tuple(members),
                ),),
                (collection_key, *(write.key for write in item_writes)),
            )

        return await self._commit(identity, resolve)

    async def commit_mutation(
        self, run_id: str, mutation: ArtifactMutation | CollectionMutation, *,
        idempotency_key: str,
    ) -> CommitResult:
        if isinstance(mutation, ArtifactMutation):
            return await self.commit_external(
                run_id,
                mutation.key,
                mutation.value,
                idempotency_key=idempotency_key,
                expected_ref=mutation.expected_ref,
            )
        if isinstance(mutation, CollectionMutation):
            return await self.mutate_collection(
                run_id,
                mutation,
                idempotency_key=idempotency_key,
            )
        raise TypeError('mutation must be ArtifactMutation or CollectionMutation')

    # Artifact reads

    async def snapshot(self, run_id: str) -> ArtifactSnapshot:
        _text(run_id, 'run_id')
        async with self._lock:
            return await self._snapshot(run_id)

    async def read(self, run_id: str, ref: ArtifactRef) -> object | None:
        _text(run_id, 'run_id')
        if not isinstance(ref, ArtifactRef):
            raise TypeError('ref must be ArtifactRef')
        async with self._lock:
            found, value = await self._read(run_id, ref)
        return value if found else None

    async def record(self, run_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        _text(run_id, 'run_id')
        if not isinstance(ref, ArtifactRef):
            raise TypeError('ref must be ArtifactRef')
        async with self._lock:
            return await self._record(run_id, ref)

    async def read_many(
        self, run_id: str, refs: Iterable[ArtifactRef],
    ) -> Mapping[ArtifactRef, object]:
        _text(run_id, 'run_id')
        requested = tuple(refs)
        if not all(isinstance(ref, ArtifactRef) for ref in requested):
            raise TypeError('refs must contain ArtifactRef values')
        values: dict[ArtifactRef, object] = {}
        async with self._lock:
            for ref in requested:
                found, value = await self._read(run_id, ref)
                if not found:
                    raise DefinitionError(f'input artifact is missing: {ref}')
                values[ref] = value
        return values

    # Run state

    async def set_run_state(
        self, run_id: str, status: str, *, error_kind: str = '', error_message: str = '',
    ) -> None:
        _text(run_id, 'run_id')
        _text(status, 'status')
        async with self._transaction():
            await self._connection.execute(
                """
                INSERT INTO run_states(run_id, status, error_kind, error_message)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status = excluded.status,
                  error_kind = excluded.error_kind,
                  error_message = excluded.error_message
                """,
                (run_id, status, error_kind, error_message),
            )

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
        return StoredRunState(row['status'], row['error_kind'], row['error_message'])

    async def run_ids(self) -> tuple[str, ...]:
        async with self._lock:
            cursor = await self._connection.execute(
                'SELECT run_id FROM run_states ORDER BY run_id'
            )
            return tuple(row['run_id'] for row in await cursor.fetchall())

    async def delete_run(self, run_id: str) -> None:
        _text(run_id, 'run_id')
        statements = (
            'DELETE FROM collection_items WHERE run_id = ?',
            'DELETE FROM collection_manifests WHERE run_id = ?',
            'DELETE FROM artifact_heads WHERE run_id = ?',
            'DELETE FROM artifact_payloads WHERE run_id = ?',
            'DELETE FROM artifact_records WHERE run_id = ?',
            'DELETE FROM artifact_versions WHERE run_id = ?',
            'DELETE FROM idempotency WHERE run_id = ?',
            'DELETE FROM run_states WHERE run_id = ?',
        )
        async with self._transaction():
            for statement in statements:
                await self._connection.execute(statement, (run_id,))

    # Atomic commit protocol

    async def _commit(
        self, identity: CommitIdentity, resolve: CommitResolver,
    ) -> CommitResult:
        async with self._transaction():
            replay = await self._replay(identity)
            if replay is not None:
                return replay

            writes = resolve(await self._snapshot(identity.run_id))
            result = (
                CommitResult('stale')
                if writes is None
                else await self._apply_write_set(identity.run_id, writes)
            )
            await self._connection.execute(
                'INSERT INTO idempotency(run_id, key, request_hash, result_json) '
                'VALUES (?, ?, ?, ?)',
                (
                    identity.run_id,
                    identity.replay_key,
                    identity.request_hash,
                    result.to_json(),
                ),
            )
            return result

    async def _replay(self, identity: CommitIdentity) -> CommitResult | None:
        cursor = await self._connection.execute(
            'SELECT request_hash, result_json FROM idempotency '
            'WHERE run_id = ? AND key = ?',
            (identity.run_id, identity.replay_key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row['request_hash'] != identity.request_hash:
            raise DefinitionError(
                f'idempotency key reused with different request: {identity.replay_key}'
            )
        return CommitResult.from_json(row['result_json'])

    async def _apply_write_set(
        self, run_id: str, writes: ResolvedWriteSet,
    ) -> CommitResult:
        refs_by_key: dict[ArtifactKey, ArtifactRef] = {}
        for value in writes.values:
            refs_by_key[value.key] = await self._write_value(run_id, writes.origin, value)
        for collection in writes.collections:
            items = tuple(
                CollectionItem(
                    member.key,
                    member.source
                    if isinstance(member.source, ArtifactRef)
                    else refs_by_key[member.source],
                )
                for member in collection.members
            )
            refs_by_key[collection.key] = await self._write_collection(
                run_id,
                writes.origin,
                collection,
                items,
            )
        return CommitResult('ok', tuple(refs_by_key[key] for key in writes.result_keys))

    async def _write_value(
        self, run_id: str, origin: WriteOrigin, value: ValueWrite,
    ) -> ArtifactRef:
        ref = await self._next_ref(run_id, value.key)
        record = (
            ArtifactRecord(ref, 'external')
            if isinstance(origin, ExternalOrigin)
            else ArtifactRecord(
                ref,
                'operation',
                origin.producer_operation,
                value.input_refs,
            )
        )
        await self._insert_record(run_id, record, value.payload)
        await self._connection.execute(
            """
            INSERT INTO artifact_heads(run_id, artifact_id, item_key, version)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id, artifact_id, item_key)
            DO UPDATE SET version = excluded.version
            """,
            (run_id, ref.key.artifact_id, ref.key.item_key, ref.version),
        )
        return ref

    async def _write_collection(
        self, run_id: str, origin: WriteOrigin, collection: CollectionManifestWrite,
        items: tuple[CollectionItem, ...],
    ) -> ArtifactRef:
        payload = pickle.dumps(
            {
                'item_artifact_id': collection.item_artifact_id,
                'items': tuple((item.key, item.ref.version) for item in items),
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )
        value = ValueWrite(
            collection.key,
            payload,
            merge_refs(collection.input_refs, (item.ref for item in items)),
        )
        ref = await self._write_value(run_id, origin, value)
        await self._connection.execute(
            """
            INSERT INTO collection_manifests(
              run_id, artifact_id, item_key, version, item_artifact_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, ref.key.artifact_id, ref.key.item_key, ref.version, collection.item_artifact_id),
        )
        await self._connection.executemany(
            """
            INSERT INTO collection_items(
              run_id, collection_artifact_id, collection_item_key,
              collection_version, position, item_key, item_artifact_id, item_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    run_id, ref.key.artifact_id, ref.key.item_key, ref.version,
                    position, item.key, item.ref.key.artifact_id, item.ref.version,
                )
                for position, item in enumerate(items)
            ),
        )
        return ref

    # SQLite representation

    async def _snapshot(self, run_id: str) -> ArtifactSnapshot:
        cursor = await self._connection.execute(
            """
            SELECT r.artifact_id, r.item_key, r.version, r.kind,
                   r.producer_operation, r.input_refs_json
            FROM artifact_heads h
            JOIN artifact_records r
              ON r.run_id = h.run_id
             AND r.artifact_id = h.artifact_id
             AND r.item_key = h.item_key
             AND r.version = h.version
            WHERE h.run_id = ?
            """,
            (run_id,),
        )
        records = {}
        for row in await cursor.fetchall():
            key = ArtifactKey(row['artifact_id'], row['item_key'])
            ref = ArtifactRef(key, row['version'])
            records[key] = ArtifactRecord(
                ref,
                row['kind'],
                row['producer_operation'],
                _refs_from_json(row['input_refs_json']),
            )

        cursor = await self._connection.execute(
            """
            SELECT m.artifact_id, m.version, m.item_artifact_id,
                   i.position, i.item_key AS member_key,
                   i.item_artifact_id AS member_artifact_id, i.item_version
            FROM collection_manifests m
            JOIN artifact_heads h
              ON h.run_id = m.run_id
             AND h.artifact_id = m.artifact_id
             AND h.item_key = m.item_key
             AND h.version = m.version
            LEFT JOIN collection_items i
              ON i.run_id = m.run_id
             AND i.collection_artifact_id = m.artifact_id
             AND i.collection_item_key = m.item_key
             AND i.collection_version = m.version
            WHERE m.run_id = ?
            ORDER BY m.artifact_id, m.version, i.position
            """,
            (run_id,),
        )
        collection_rows: dict[ArtifactKey, list[aiosqlite.Row]] = {}
        for row in await cursor.fetchall():
            key = ArtifactKey.scalar(row['artifact_id'])
            collection_rows.setdefault(key, []).append(row)

        collections = {}
        for key, rows in collection_rows.items():
            first = rows[0]
            ref = ArtifactRef(key, first['version'])
            items = tuple(
                CollectionItem(
                    row['member_key'],
                    ArtifactRef(
                        ArtifactKey.item(row['member_artifact_id'], row['member_key']),
                        row['item_version'],
                    ),
                )
                for row in rows
                if row['position'] is not None
            )
            collections[key] = CollectionSnapshot(ref, first['item_artifact_id'], items)
        return ArtifactSnapshot(records, collections)

    async def _read(self, run_id: str, ref: ArtifactRef) -> tuple[bool, object]:
        cursor = await self._connection.execute(
            """
            SELECT payload FROM artifact_payloads
            WHERE run_id = ? AND artifact_id = ? AND item_key = ? AND version = ?
            """,
            (run_id, ref.key.artifact_id, ref.key.item_key, ref.version),
        )
        row = await cursor.fetchone()
        return (False, None) if row is None else (True, pickle.loads(row['payload']))

    async def _record(self, run_id: str, ref: ArtifactRef) -> ArtifactRecord | None:
        cursor = await self._connection.execute(
            """
            SELECT kind, producer_operation, input_refs_json
            FROM artifact_records
            WHERE run_id = ? AND artifact_id = ? AND item_key = ? AND version = ?
            """,
            (run_id, ref.key.artifact_id, ref.key.item_key, ref.version),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ArtifactRecord(
            ref,
            row['kind'],
            row['producer_operation'],
            _refs_from_json(row['input_refs_json']),
        )

    async def _next_ref(self, run_id: str, key: ArtifactKey) -> ArtifactRef:
        cursor = await self._connection.execute(
            """
            INSERT INTO artifact_versions(run_id, artifact_id, item_key, next_version)
            VALUES (?, ?, ?, 2)
            ON CONFLICT(run_id, artifact_id, item_key)
            DO UPDATE SET next_version = next_version + 1
            RETURNING next_version - 1 AS version
            """,
            (run_id, key.artifact_id, key.item_key),
        )
        row = await cursor.fetchone()
        return ArtifactRef(key, row['version'])

    async def _insert_record(
        self, run_id: str, record: ArtifactRecord, payload: bytes,
    ) -> None:
        ref = record.ref
        await self._connection.execute(
            """
            INSERT INTO artifact_records(
              run_id, artifact_id, item_key, version, kind,
              producer_operation, input_refs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, ref.key.artifact_id, ref.key.item_key, ref.version,
                record.kind, record.producer_operation, _refs_json(record.input_refs),
            ),
        )
        await self._connection.execute(
            """
            INSERT INTO artifact_payloads(run_id, artifact_id, item_key, version, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, ref.key.artifact_id, ref.key.item_key, ref.version, payload),
        )

    async def _create_schema(self) -> None:
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact_versions(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              item_key TEXT NOT NULL,
              next_version INTEGER NOT NULL,
              PRIMARY KEY(run_id, artifact_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS artifact_records(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              item_key TEXT NOT NULL,
              version INTEGER NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('external', 'operation')),
              producer_operation TEXT NOT NULL,
              input_refs_json TEXT NOT NULL,
              PRIMARY KEY(run_id, artifact_id, item_key, version)
            );
            CREATE TABLE IF NOT EXISTS artifact_payloads(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              item_key TEXT NOT NULL,
              version INTEGER NOT NULL,
              payload BLOB NOT NULL,
              PRIMARY KEY(run_id, artifact_id, item_key, version),
              FOREIGN KEY(run_id, artifact_id, item_key, version)
                REFERENCES artifact_records(run_id, artifact_id, item_key, version)
                ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS artifact_heads(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              item_key TEXT NOT NULL,
              version INTEGER NOT NULL,
              PRIMARY KEY(run_id, artifact_id, item_key),
              FOREIGN KEY(run_id, artifact_id, item_key, version)
                REFERENCES artifact_records(run_id, artifact_id, item_key, version)
            );
            CREATE TABLE IF NOT EXISTS collection_manifests(
              run_id TEXT NOT NULL,
              artifact_id TEXT NOT NULL,
              item_key TEXT NOT NULL CHECK(item_key = ''),
              version INTEGER NOT NULL,
              item_artifact_id TEXT NOT NULL,
              PRIMARY KEY(run_id, artifact_id, item_key, version),
              FOREIGN KEY(run_id, artifact_id, item_key, version)
                REFERENCES artifact_records(run_id, artifact_id, item_key, version)
                ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS collection_items(
              run_id TEXT NOT NULL,
              collection_artifact_id TEXT NOT NULL,
              collection_item_key TEXT NOT NULL CHECK(collection_item_key = ''),
              collection_version INTEGER NOT NULL,
              position INTEGER NOT NULL,
              item_key TEXT NOT NULL,
              item_artifact_id TEXT NOT NULL,
              item_version INTEGER NOT NULL,
              PRIMARY KEY(
                run_id, collection_artifact_id, collection_item_key,
                collection_version, position
              ),
              UNIQUE(
                run_id, collection_artifact_id, collection_item_key,
                collection_version, item_key
              ),
              FOREIGN KEY(
                run_id, collection_artifact_id, collection_item_key, collection_version
              ) REFERENCES collection_manifests(run_id, artifact_id, item_key, version)
                ON DELETE CASCADE,
              FOREIGN KEY(run_id, item_artifact_id, item_key, item_version)
                REFERENCES artifact_records(run_id, artifact_id, item_key, version)
            );
            CREATE TABLE IF NOT EXISTS idempotency(
              run_id TEXT NOT NULL,
              key TEXT NOT NULL,
              request_hash TEXT NOT NULL,
              result_json TEXT NOT NULL,
              PRIMARY KEY(run_id, key)
            );
            CREATE TABLE IF NOT EXISTS run_states(
              run_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              error_kind TEXT NOT NULL,
              error_message TEXT NOT NULL
            );
            """
        )
        await self._connection.commit()

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


def _key_data(key: ArtifactKey) -> list[str]:
    return [key.artifact_id, key.item_key]


def _ref_data(ref: ArtifactRef | None) -> list[object] | None:
    return None if ref is None else [ref.key.artifact_id, ref.key.item_key, ref.version]


def _origin_data(origin: WriteOrigin) -> list[str]:
    if isinstance(origin, ExternalOrigin):
        return ['external']
    return ['operation', origin.producer_operation]


def _writes_data(writes: ResolvedWriteSet) -> list[object]:
    return [
        [
            [*_key_data(write.key), _digest(write.payload), [_ref_data(ref) for ref in write.input_refs]]
            for write in writes.values
        ],
        [
            [
                *_key_data(collection.key),
                collection.item_artifact_id,
                [
                    [
                        member.key,
                        'ref' if isinstance(member.source, ArtifactRef) else 'write',
                        _ref_data(member.source)
                        if isinstance(member.source, ArtifactRef)
                        else _key_data(member.source),
                    ]
                    for member in collection.members
                ],
                [_ref_data(ref) for ref in collection.input_refs],
            ]
            for collection in writes.collections
        ],
        [_key_data(key) for key in writes.result_keys],
    ]


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fingerprint(*values: object) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()


def _refs_json(refs: tuple[ArtifactRef, ...]) -> str:
    values = [[ref.key.artifact_id, ref.key.item_key, ref.version] for ref in refs]
    return json.dumps(values, separators=(',', ':'))


def _refs_from_json(value: str) -> tuple[ArtifactRef, ...]:
    return tuple(
        ArtifactRef(ArtifactKey(str(item[0]), str(item[1])), int(item[2]))
        for item in json.loads(value)
    )


__all__ = ['ArtifactStore', 'CommitResult', 'StoredRunState']
