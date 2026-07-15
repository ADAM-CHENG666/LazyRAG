from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .errors import DefinitionError


def _text(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be str')
    if not value.strip():
        raise DefinitionError(f'{name} must be non-empty')


@dataclass(frozen=True, order=True)
class ArtifactKey:
    artifact_id: str
    item_key: str = ''

    def __post_init__(self) -> None:
        _text(self.artifact_id, 'artifact_id')
        if not isinstance(self.item_key, str):
            raise TypeError('item_key must be str')
        if self.item_key and not self.item_key.strip():
            raise DefinitionError('item_key must be non-empty when set')

    @classmethod
    def scalar(cls, artifact_id: str) -> ArtifactKey:
        return cls(artifact_id)

    @classmethod
    def item(cls, artifact_id: str, item_key: str) -> ArtifactKey:
        _text(item_key, 'item_key')
        return cls(artifact_id, item_key)


@dataclass(frozen=True, order=True)
class ArtifactRef:
    key: ArtifactKey
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.key, ArtifactKey):
            raise TypeError('key must be ArtifactKey')
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise TypeError('version must be int')
        if self.version < 1:
            raise DefinitionError('version must be >= 1')


@dataclass(frozen=True)
class ArtifactRecord:
    ref: ArtifactRef
    kind: Literal['external', 'operation']
    producer_operation: str = ''
    input_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ArtifactRef):
            raise TypeError('ref must be ArtifactRef')
        if self.kind not in {'external', 'operation'}:
            raise DefinitionError(f'unknown artifact record kind: {self.kind}')

        inputs = tuple(sorted(self.input_refs))
        if not all(isinstance(ref, ArtifactRef) for ref in inputs):
            raise TypeError('input_refs must contain ArtifactRef values')
        if len({ref.key for ref in inputs}) != len(inputs):
            raise DefinitionError('input_refs must contain at most one ref per artifact key')
        if self.kind == 'external':
            if self.producer_operation or inputs:
                raise DefinitionError('external artifact cannot declare producer or input refs')
        else:
            _text(self.producer_operation, 'producer_operation')

        object.__setattr__(self, 'input_refs', inputs)


@dataclass(frozen=True)
class CollectionItem:
    key: str
    ref: ArtifactRef

    def __post_init__(self) -> None:
        _text(self.key, 'collection item key')
        if not isinstance(self.ref, ArtifactRef):
            raise TypeError('collection item ref must be ArtifactRef')
        if self.ref.key.item_key != self.key:
            raise DefinitionError('collection item key must match artifact ref item_key')


@dataclass(frozen=True)
class CollectionSnapshot:
    ref: ArtifactRef
    item_artifact_id: str
    items: tuple[CollectionItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ArtifactRef):
            raise TypeError('collection ref must be ArtifactRef')
        if self.ref.key.item_key:
            raise DefinitionError('collection ref must be a scalar artifact ref')
        _text(self.item_artifact_id, 'collection item_artifact_id')

        items = tuple(self.items)
        if not all(isinstance(item, CollectionItem) for item in items):
            raise TypeError('collection items must be CollectionItem values')
        if len({item.key for item in items}) != len(items):
            raise DefinitionError('collection item keys must be unique')
        if any(item.ref.key.artifact_id != self.item_artifact_id for item in items):
            raise DefinitionError('collection items must share the declared item artifact id')

        object.__setattr__(self, 'items', items)


@dataclass(frozen=True)
class CollectionResult:
    items: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        items = dict(self.items)
        for key in items:
            _text(key, 'collection result item key')
        object.__setattr__(self, 'items', MappingProxyType(items))


@dataclass(frozen=True)
class CollectionItemGuard:
    collection_key: ArtifactKey
    item: CollectionItem

    def __post_init__(self) -> None:
        if not isinstance(self.collection_key, ArtifactKey) or self.collection_key.item_key:
            raise DefinitionError('collection guard key must be a scalar ArtifactKey')
        if not isinstance(self.item, CollectionItem):
            raise TypeError('collection guard item must be CollectionItem')


@dataclass(frozen=True)
class CollectionWrite:
    key: ArtifactKey
    item_artifact_id: str
    items: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, ArtifactKey) or self.key.item_key:
            raise DefinitionError('collection write key must be a scalar ArtifactKey')
        _text(self.item_artifact_id, 'collection write item_artifact_id')
        items = dict(self.items)
        for item_key in items:
            _text(item_key, 'collection write item key')
        object.__setattr__(self, 'items', MappingProxyType(items))


@dataclass(frozen=True)
class OperationWriteSet:
    commit_id: str
    producer_operation: str
    input_refs: tuple[ArtifactRef, ...]
    scalar_values: Mapping[ArtifactKey, object] = field(default_factory=dict)
    collection_writes: tuple[CollectionWrite, ...] = ()
    item_guards: tuple[CollectionItemGuard, ...] = ()
    collection_guards: tuple[CollectionSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _text(self.commit_id, 'artifact commit id')
        _text(self.producer_operation, 'artifact commit producer_operation')

        input_refs = tuple(sorted(self.input_refs))
        if not all(isinstance(ref, ArtifactRef) for ref in input_refs):
            raise TypeError('artifact commit input_refs must contain ArtifactRef values')
        if len({ref.key for ref in input_refs}) != len(input_refs):
            raise DefinitionError('artifact commit input_refs must contain one ref per key')
        scalar_values = dict(self.scalar_values)
        if not all(isinstance(key, ArtifactKey) for key in scalar_values):
            raise TypeError('artifact commit scalar_values keys must be ArtifactKey values')

        collection_writes = tuple(self.collection_writes)
        item_guards = tuple(self.item_guards)
        collection_guards = tuple(self.collection_guards)
        if not all(isinstance(write, CollectionWrite) for write in collection_writes):
            raise TypeError('artifact commit collection_writes must contain CollectionWrite values')
        if not all(isinstance(guard, CollectionItemGuard) for guard in item_guards):
            raise TypeError('artifact commit item_guards must contain CollectionItemGuard values')
        if not all(isinstance(guard, CollectionSnapshot) for guard in collection_guards):
            raise TypeError('artifact commit collection_guards must contain CollectionSnapshot values')

        input_by_key = {ref.key: ref for ref in input_refs}
        guarded_refs = [guard.item.ref for guard in item_guards]
        guarded_refs.extend(
            ref
            for guard in collection_guards
            for ref in (guard.ref, *(item.ref for item in guard.items))
        )
        if any(input_by_key.get(ref.key) != ref for ref in guarded_refs):
            raise DefinitionError('artifact commit guards must reference exact input refs')

        output_keys = self.output_keys()
        if len(set(output_keys)) != len(output_keys):
            raise DefinitionError('artifact commit output keys must be unique')
        if not output_keys:
            raise DefinitionError('artifact commit must contain at least one output')

        object.__setattr__(self, 'input_refs', input_refs)
        object.__setattr__(self, 'scalar_values', MappingProxyType(scalar_values))
        object.__setattr__(self, 'collection_writes', collection_writes)
        object.__setattr__(self, 'item_guards', item_guards)
        object.__setattr__(self, 'collection_guards', collection_guards)

    def output_keys(self) -> tuple[ArtifactKey, ...]:
        keys = [*self.scalar_values]
        keys.extend(write.key for write in self.collection_writes)
        keys.extend(
            ArtifactKey.item(write.item_artifact_id, item_key)
            for write in self.collection_writes
            for item_key in write.items
        )
        return tuple(keys)


@dataclass(frozen=True)
class ArtifactMutation:
    key: ArtifactKey
    value: object
    expected_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ArtifactKey):
            raise TypeError('artifact mutation key must be ArtifactKey')
        if self.key.item_key:
            raise DefinitionError('item artifacts must be edited through CollectionMutation')
        if self.expected_ref is not None:
            if not isinstance(self.expected_ref, ArtifactRef):
                raise TypeError('expected_ref must be ArtifactRef or None')
            if self.expected_ref.key != self.key:
                raise DefinitionError('expected_ref must identify the mutated artifact')


@dataclass(frozen=True)
class CollectionMutation:
    collection_id: str
    item_artifact_id: str
    upserts: Mapping[str, object] = field(default_factory=dict)
    deletes: tuple[str, ...] = ()
    expected_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        _text(self.collection_id, 'collection_id')
        _text(self.item_artifact_id, 'item_artifact_id')
        upserts = dict(self.upserts)
        deletes = tuple(self.deletes)

        for key in upserts:
            _text(key, 'collection upsert key')
        for key in deletes:
            _text(key, 'collection delete key')
        if len(set(deletes)) != len(deletes):
            raise DefinitionError('collection delete keys must be unique')
        if set(upserts) & set(deletes):
            raise DefinitionError('collection mutation cannot upsert and delete the same key')
        if not upserts and not deletes:
            raise DefinitionError('collection mutation must change at least one item')

        if self.expected_ref is not None:
            if not isinstance(self.expected_ref, ArtifactRef):
                raise TypeError('expected_ref must be ArtifactRef or None')
            if self.expected_ref.key != ArtifactKey.scalar(self.collection_id):
                raise DefinitionError('expected_ref must identify the mutated collection')

        object.__setattr__(self, 'upserts', MappingProxyType(upserts))
        object.__setattr__(self, 'deletes', tuple(sorted(deletes)))


@dataclass(frozen=True)
class ArtifactSnapshot:
    records: Mapping[ArtifactKey, ArtifactRecord] = field(default_factory=dict)
    collections: Mapping[ArtifactKey, CollectionSnapshot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        records = dict(self.records)
        collections = dict(self.collections)

        for key, record in records.items():
            if not isinstance(key, ArtifactKey) or not isinstance(record, ArtifactRecord):
                raise TypeError('records must map ArtifactKey to ArtifactRecord')
            if record.ref.key != key:
                raise DefinitionError('artifact record key must match its ref')
        for key, collection in collections.items():
            if not isinstance(key, ArtifactKey) or not isinstance(collection, CollectionSnapshot):
                raise TypeError('collections must map ArtifactKey to CollectionSnapshot')
            if collection.ref.key != key:
                raise DefinitionError('collection key must match its ref')
            record = records.get(key)
            if record is None or record.ref != collection.ref:
                raise DefinitionError('collection ref must be the current artifact record')

        object.__setattr__(self, 'records', MappingProxyType(records))
        object.__setattr__(self, 'collections', MappingProxyType(collections))

    def effective_records(self) -> Mapping[ArtifactKey, ArtifactRecord]:
        effective = dict(self.records)
        changed = True
        while changed:
            changed = False
            for key, record in tuple(effective.items()):
                if record.kind != 'operation':
                    continue
                if any(effective.get(ref.key, None) is None or effective[ref.key].ref != ref
                       for ref in record.input_refs):
                    del effective[key]
                    changed = True
            for key, collection in self.collections.items():
                if key not in effective:
                    continue
                if any(
                    effective.get(item.ref.key) is None
                    or effective[item.ref.key].ref != item.ref
                    for item in collection.items
                ):
                    del effective[key]
                    changed = True
        return MappingProxyType(effective)

    def effective_collections(self) -> Mapping[ArtifactKey, CollectionSnapshot]:
        effective = self.effective_records()
        return MappingProxyType({
            key: collection
            for key, collection in self.collections.items()
            if effective.get(key) is not None and effective[key].ref == collection.ref
        })


def merge_refs(*groups: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    refs: dict[ArtifactKey, ArtifactRef] = {}
    for group in groups:
        for ref in group:
            previous = refs.get(ref.key)
            if previous is not None and previous != ref:
                raise DefinitionError(f'conflicting refs for artifact key {ref.key}')
            refs[ref.key] = ref
    return tuple(sorted(refs.values()))


__all__ = [
    'OperationWriteSet',
    'ArtifactKey',
    'ArtifactMutation',
    'ArtifactRecord',
    'ArtifactRef',
    'ArtifactSnapshot',
    'CollectionItem',
    'CollectionItemGuard',
    'CollectionMutation',
    'CollectionResult',
    'CollectionSnapshot',
    'CollectionWrite',
]
