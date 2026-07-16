from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import networkx as nx

from .artifact import (
    ArtifactKey,
    ArtifactMutation,
    ArtifactRecord,
    ArtifactRef,
    ArtifactSnapshot,
    CollectionItem,
    CollectionMutation,
    CollectionSnapshot,
    merge_refs,
)
from .errors import DefinitionError
from .operation import (
    BoundCollectionItem,
    BoundInput,
    Operation,
    OperationInvocation,
    OperationSpec,
)
from .utils import _text


@dataclass(frozen=True)
class CollectionProjection:
    collection_key: ArtifactKey
    item_artifact_id: str
    items: tuple[CollectionItem, ...]
    producer_operation: str
    input_refs: tuple[ArtifactRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.collection_key, ArtifactKey) or self.collection_key.item_key:
            raise DefinitionError('collection projection key must be a scalar ArtifactKey')
        _text(self.item_artifact_id, 'collection projection item_artifact_id')
        _text(self.producer_operation, 'collection projection producer_operation')
        items = tuple(self.items)
        if any(item.ref.key.artifact_id != self.item_artifact_id for item in items):
            raise DefinitionError('collection projection items do not match item_artifact_id')
        object.__setattr__(self, 'items', items)
        object.__setattr__(self, 'input_refs', merge_refs(self.input_refs))


@dataclass(frozen=True)
class PlanningView:
    records: Mapping[ArtifactKey, ArtifactRecord]
    collections: Mapping[ArtifactKey, CollectionSnapshot]

    def __post_init__(self) -> None:
        records = dict(self.records)
        collections = dict(self.collections)
        if any(
            records.get(key) is None or records[key].ref != collection.ref
            for key, collection in collections.items()
        ):
            raise DefinitionError('planning view collections must reference visible records')
        object.__setattr__(self, 'records', MappingProxyType(records))
        object.__setattr__(self, 'collections', MappingProxyType(collections))


@dataclass(frozen=True)
class PlanningDecision:
    view: PlanningView
    projections: tuple[CollectionProjection, ...]
    invocations: tuple[OperationInvocation, ...]
    complete: bool
    blocked_reason: str = ''


@dataclass(frozen=True)
class RuntimeDefinition:
    operations: tuple[Operation, ...]
    artifact_shapes: Mapping[str, str]
    writer_by_artifact: Mapping[str, str]
    item_writer_by_artifact: Mapping[str, str]
    collection_items: Mapping[str, str]

    def __post_init__(self) -> None:
        operations = tuple(self.operations)
        if not operations:
            raise DefinitionError('runtime definition requires at least one operation')
        object.__setattr__(self, 'operations', operations)
        object.__setattr__(self, 'artifact_shapes', MappingProxyType(dict(self.artifact_shapes)))
        object.__setattr__(
            self, 'writer_by_artifact', MappingProxyType(dict(self.writer_by_artifact))
        )
        object.__setattr__(
            self, 'item_writer_by_artifact', MappingProxyType(dict(self.item_writer_by_artifact))
        )
        object.__setattr__(self, 'collection_items', MappingProxyType(dict(self.collection_items)))

    def __iter__(self) -> Iterator[Operation]:
        return iter(self.operations)

    def __len__(self) -> int:
        return len(self.operations)

    def __getitem__(self, index: int | slice) -> Operation | tuple[Operation, ...]:
        return self.operations[index]

    def validate_mutation(self, mutation: ArtifactMutation | CollectionMutation) -> None:
        if isinstance(mutation, ArtifactMutation):
            if self.artifact_shapes.get(mutation.key.artifact_id) != 'scalar':
                raise DefinitionError(
                    f'{mutation.key.artifact_id} is not a declared scalar artifact'
                )
            return

        if not isinstance(mutation, CollectionMutation):
            raise TypeError('mutation must be ArtifactMutation or CollectionMutation')
        if self.artifact_shapes.get(mutation.collection_id) != 'collection':
            raise DefinitionError(
                f'{mutation.collection_id} is not a declared collection artifact'
            )
        expected_item_id = self.collection_items.get(mutation.collection_id)
        if expected_item_id is not None and mutation.item_artifact_id != expected_item_id:
            raise DefinitionError(
                f'{mutation.collection_id} requires item artifact {expected_item_id}'
            )


def compile_operations(operations: Sequence[Operation]) -> RuntimeDefinition:
    declared = tuple(operations)
    if not declared:
        raise DefinitionError('at least one operation is required')

    by_id: dict[str, Operation] = {}
    writer_by_artifact: dict[str, str] = {}
    item_writer_by_artifact: dict[str, str] = {}
    shape_by_artifact: dict[str, str] = {}
    collection_items: dict[str, str] = {}
    for operation in declared:
        spec = getattr(operation, 'spec', None)
        if not callable(operation) or not isinstance(spec, OperationSpec):
            raise TypeError('operations must contain declared Operation callables')
        if spec.op_id in by_id:
            raise DefinitionError(f'duplicate operation id: {spec.op_id}')
        by_id[spec.op_id] = operation
        for binding in spec.inputs.values():
            shape = 'scalar' if binding.mode == 'one' else 'collection'
            previous_shape = shape_by_artifact.setdefault(binding.artifact_id, shape)
            if previous_shape != shape:
                raise DefinitionError(
                    f'artifact {binding.artifact_id} is used as both scalar and collection'
                )
        for output in spec.outputs.values():
            shape = 'scalar' if output.mode == 'scalar' else 'collection'
            previous_shape = shape_by_artifact.setdefault(output.artifact_id, shape)
            if previous_shape != shape:
                raise DefinitionError(
                    f'artifact {output.artifact_id} is used as both scalar and collection'
                )
            previous = writer_by_artifact.get(output.artifact_id)
            if previous is not None:
                raise DefinitionError(
                    f'artifact {output.artifact_id} has multiple writers: {previous}, {spec.op_id}'
                )
            writer_by_artifact[output.artifact_id] = spec.op_id
            if output.item_artifact_id:
                collection_items[output.artifact_id] = output.item_artifact_id
                previous_item_writer = item_writer_by_artifact.get(output.item_artifact_id)
                if previous_item_writer is not None:
                    raise DefinitionError(
                        f'item artifact {output.item_artifact_id} has multiple writers: '
                        f'{previous_item_writer}, {spec.op_id}'
                    )
                item_writer_by_artifact[output.item_artifact_id] = spec.op_id

    graph = nx.DiGraph()
    graph.add_nodes_from(by_id)
    for operation in declared:
        for binding in operation.spec.inputs.values():
            writer = writer_by_artifact.get(binding.artifact_id)
            if writer is not None:
                producer = by_id[writer]
                output = next(
                    output for output in producer.spec.outputs.values()
                    if output.artifact_id == binding.artifact_id
                )
                if (output.mode == 'scalar') != (binding.mode == 'one'):
                    raise DefinitionError(
                        f'{operation.spec.op_id} {binding.mode} input is incompatible with '
                        f'{writer} {output.mode} output {binding.artifact_id}'
                    )
                graph.add_edge(writer, operation.spec.op_id)
    if not nx.is_directed_acyclic_graph(graph):
        cycle = ' -> '.join(node for node, _ in nx.find_cycle(graph))
        raise DefinitionError(f'operation dependencies must be acyclic: {cycle}')

    order = nx.lexicographical_topological_sort(graph, key=str)
    return RuntimeDefinition(
        tuple(by_id[op_id] for op_id in order),
        shape_by_artifact,
        writer_by_artifact,
        item_writer_by_artifact,
        collection_items,
    )


def plan_next(definition: RuntimeDefinition, artifacts: ArtifactSnapshot) -> PlanningDecision:
    if not isinstance(definition, RuntimeDefinition):
        raise TypeError('definition must be RuntimeDefinition')
    if not isinstance(artifacts, ArtifactSnapshot):
        raise TypeError('artifacts must be ArtifactSnapshot')
    effective = _operation_effective_records(definition.operations, artifacts)
    collections = _effective_collections(artifacts, effective)
    projections = _plan_projections(definition.operations, effective, collections)
    invocations = _plan_invocations(definition.operations, effective, collections)
    complete = all(
        ArtifactKey.scalar(output.artifact_id) in effective
        for operation in definition.operations
        for output in operation.spec.outputs.values()
    )
    blocked_reason = ''
    if not complete and not projections and not invocations:
        blocked_reason = 'artifact planning stalled with missing outputs'
    return PlanningDecision(
        PlanningView(effective, collections),
        projections,
        invocations,
        complete,
        blocked_reason,
    )


def _plan_invocations(
    operations: tuple[Operation, ...],
    effective: Mapping[ArtifactKey, ArtifactRecord],
    collections: Mapping[ArtifactKey, CollectionSnapshot],
) -> tuple[OperationInvocation, ...]:
    ready: list[OperationInvocation] = []
    for operation in operations:
        item_keys = _each_item_keys(operation, collections)
        if item_keys is None:
            continue
        invocation_keys = item_keys if _has_each(operation) else (None,)
        for item_key in invocation_keys:
            inputs = _bind_inputs(operation, effective, collections, item_key)
            if inputs is None:
                continue
            output_keys = {
                name: output.key_for('' if item_key is None else item_key)
                for name, output in operation.spec.outputs.items()
            }
            if all(key in effective for key in output_keys.values()):
                continue
            ready.append(OperationInvocation(
                invocation_id=_invocation_id(operation.spec.op_id, inputs, output_keys),
                operation=operation,
                inputs=inputs,
                output_keys=output_keys,
                item_key=item_key or '',
            ))
    return tuple(ready)


def _plan_projections(
    operations: tuple[Operation, ...],
    effective: Mapping[ArtifactKey, ArtifactRecord],
    collections: Mapping[ArtifactKey, CollectionSnapshot],
) -> tuple[CollectionProjection, ...]:
    projections: list[CollectionProjection] = []
    for operation in operations:
        if not _has_each(operation):
            continue
        item_keys = _each_item_keys(operation, collections)
        base_refs = _manifest_input_refs(operation, effective, collections)
        if item_keys is None or base_refs is None:
            continue
        if any(
            _bind_inputs(operation, effective, collections, item_key) is None
            for item_key in item_keys
        ):
            continue
        for output in operation.spec.outputs.values():
            collection_key = ArtifactKey.scalar(output.artifact_id)
            if collection_key in effective:
                continue
            items: list[CollectionItem] = []
            for item_key in item_keys:
                record = effective.get(ArtifactKey.item(output.item_artifact_id, item_key))
                if record is None:
                    break
                items.append(CollectionItem(item_key, record.ref))
            else:
                item_tuple = tuple(items)
                projections.append(CollectionProjection(
                    collection_key,
                    output.item_artifact_id,
                    item_tuple,
                    operation.spec.op_id,
                    merge_refs(base_refs, (item.ref for item in item_tuple)),
                ))
    return tuple(projections)


def _operation_effective_records(
    operations: Sequence[Operation], artifacts: ArtifactSnapshot,
) -> dict[ArtifactKey, ArtifactRecord]:
    effective = dict(artifacts.effective_records())
    changed = True
    while changed:
        changed = False
        collections = _effective_collections(artifacts, effective)
        member_keys = {
            item.ref.key
            for collection in collections.values()
            for item in collection.items
        }
        for key, record in tuple(effective.items()):
            if key.item_key and record.kind == 'external' and key not in member_keys:
                del effective[key]
                changed = True
        for operation in operations:
            if _has_each(operation):
                changed |= _validate_each_outputs(operation, effective, collections, artifacts)
            else:
                changed |= _validate_single_outputs(operation, effective, collections, artifacts)
    return effective


def _validate_single_outputs(
    operation: Operation, effective: dict[ArtifactKey, ArtifactRecord],
    collections: Mapping[ArtifactKey, CollectionSnapshot], artifacts: ArtifactSnapshot,
) -> bool:
    inputs = _bind_inputs(operation, effective, collections, None)
    input_refs = None if inputs is None else _lineage_refs(inputs)
    changed = False
    for output in operation.spec.outputs.values():
        key = ArtifactKey.scalar(output.artifact_id)
        record = effective.get(key)
        if record is None or record.kind == 'external':
            continue
        valid = input_refs is not None and record.producer_operation == operation.spec.op_id
        if valid and output.mode == 'scalar':
            valid = record.input_refs == input_refs
        elif valid:
            collection = artifacts.collections.get(key)
            valid = _valid_produced_collection(
                operation,
                collection,
                output.item_artifact_id,
                input_refs,
                effective,
            ) and record.input_refs == merge_refs(
                input_refs,
                (() if collection is None else (item.ref for item in collection.items)),
            )
        if not valid:
            del effective[key]
            changed = True
    return changed


def _validate_each_outputs(
    operation: Operation, effective: dict[ArtifactKey, ArtifactRecord],
    collections: Mapping[ArtifactKey, CollectionSnapshot], artifacts: ArtifactSnapshot,
) -> bool:
    item_keys = _each_item_keys(operation, collections)
    changed = False
    inputs_by_key = {
        item_key: _bind_inputs(operation, effective, collections, item_key)
        for item_key in (() if item_keys is None else item_keys)
    }
    current_keys = set(inputs_by_key)
    for output in operation.spec.outputs.values():
        for key, record in tuple(effective.items()):
            if (
                key.artifact_id == output.item_artifact_id
                and key.item_key not in current_keys
                and record.kind == 'operation'
                and record.producer_operation == operation.spec.op_id
            ):
                del effective[key]
                changed = True
    if item_keys is not None:
        for item_key in item_keys:
            for output in operation.spec.outputs.values():
                key = ArtifactKey.item(output.item_artifact_id, item_key)
                record = effective.get(key)
                if record is None or record.kind != 'operation':
                    continue
                inputs = inputs_by_key[item_key]
                valid = (
                    inputs is not None
                    and record.producer_operation == operation.spec.op_id
                    and record.input_refs == _lineage_refs(inputs)
                )
                if not valid:
                    del effective[key]
                    changed = True

    base_refs = _manifest_input_refs(operation, effective, collections)
    complete_item_keys = (
        item_keys
        if item_keys is not None and all(inputs is not None for inputs in inputs_by_key.values())
        else None
    )
    for output in operation.spec.outputs.values():
        key = ArtifactKey.scalar(output.artifact_id)
        record = effective.get(key)
        if record is None or record.kind == 'external':
            continue
        collection = artifacts.collections.get(key)
        expected_items = _output_items(output.item_artifact_id, complete_item_keys, effective)
        valid = (
            base_refs is not None
            and expected_items is not None
            and collection is not None
            and collection.item_artifact_id == output.item_artifact_id
            and collection.items == expected_items
            and record.producer_operation == operation.spec.op_id
            and record.input_refs == merge_refs(
                base_refs,
                (item.ref for item in expected_items),
            )
        )
        if not valid:
            del effective[key]
            changed = True
    return changed


def _valid_produced_collection(
    operation: Operation, collection: CollectionSnapshot | None, item_artifact_id: str,
    input_refs: tuple[ArtifactRef, ...], effective: Mapping[ArtifactKey, ArtifactRecord],
) -> bool:
    if collection is None or collection.item_artifact_id != item_artifact_id:
        return False
    for item in collection.items:
        record = effective.get(item.ref.key)
        if (
            record is None
            or record.ref != item.ref
            or record.kind != 'operation'
            or record.producer_operation != operation.spec.op_id
            or record.input_refs != input_refs
        ):
            return False
    return True


def _effective_collections(
    artifacts: ArtifactSnapshot, effective: Mapping[ArtifactKey, ArtifactRecord],
) -> dict[ArtifactKey, CollectionSnapshot]:
    return {
        key: collection
        for key, collection in artifacts.collections.items()
        if effective.get(key) is not None
        and effective[key].ref == collection.ref
        and all(
            effective.get(item.ref.key) is not None
            and effective[item.ref.key].ref == item.ref
            for item in collection.items
        )
    }


def _has_each(operation: Operation) -> bool:
    return operation.spec.driver_input is not None


def _each_item_keys(
    operation: Operation, collections: Mapping[ArtifactKey, CollectionSnapshot],
) -> tuple[str, ...] | None:
    each_collections = [
        collections.get(ArtifactKey.scalar(binding.artifact_id))
        for binding in operation.spec.inputs.values()
        if binding.mode == 'each'
    ]
    if not each_collections:
        return ()
    if any(collection is None for collection in each_collections):
        return None
    collection = each_collections[0]
    if collection is None:
        return None
    return tuple(item.key for item in collection.items)


def _bind_inputs(
    operation: Operation, effective: Mapping[ArtifactKey, ArtifactRecord],
    collections: Mapping[ArtifactKey, CollectionSnapshot], item_key: str | None,
) -> dict[str, BoundInput] | None:
    inputs: dict[str, BoundInput] = {}
    for name, binding in operation.spec.inputs.items():
        key = ArtifactKey.scalar(binding.artifact_id)
        if binding.mode == 'one':
            record = effective.get(key)
            if record is None:
                return None
            inputs[name] = record.ref
        else:
            collection = collections.get(key)
            if collection is None:
                return None
            if binding.mode == 'all':
                inputs[name] = collection
            else:
                if item_key is None:
                    return None
                item = next((item for item in collection.items if item.key == item_key), None)
                if item is None:
                    return None
                inputs[name] = BoundCollectionItem(
                    collection.ref,
                    collection.item_artifact_id,
                    item,
                )
    return inputs


def _lineage_refs(inputs: Mapping[str, BoundInput]) -> tuple[ArtifactRef, ...]:
    groups: list[ArtifactRef] = []
    for value in inputs.values():
        if isinstance(value, ArtifactRef):
            groups.append(value)
        elif isinstance(value, BoundCollectionItem):
            groups.append(value.item.ref)
        else:
            groups.append(value.ref)
            groups.extend(item.ref for item in value.items)
    return merge_refs(groups)


def _manifest_input_refs(
    operation: Operation, effective: Mapping[ArtifactKey, ArtifactRecord],
    collections: Mapping[ArtifactKey, CollectionSnapshot],
) -> tuple[ArtifactRef, ...] | None:
    refs: list[ArtifactRef] = []
    for binding in operation.spec.inputs.values():
        key = ArtifactKey.scalar(binding.artifact_id)
        if binding.mode == 'one':
            record = effective.get(key)
            if record is None:
                return None
            refs.append(record.ref)
            continue
        collection = collections.get(key)
        if collection is None:
            return None
        refs.append(collection.ref)
        if binding.mode == 'all':
            refs.extend(item.ref for item in collection.items)
    return merge_refs(refs)


def _output_items(
    item_artifact_id: str, item_keys: tuple[str, ...] | None,
    effective: Mapping[ArtifactKey, ArtifactRecord],
) -> tuple[CollectionItem, ...] | None:
    if item_keys is None:
        return None
    items = []
    for item_key in item_keys:
        record = effective.get(ArtifactKey.item(item_artifact_id, item_key))
        if record is None:
            return None
        items.append(CollectionItem(item_key, record.ref))
    return tuple(items)


def _invocation_id(
    op_id: str, inputs: Mapping[str, BoundInput], outputs: Mapping[str, ArtifactKey],
) -> str:
    payload = {
        'operation': op_id,
        'inputs': [
            [name, *_bound_identity(value)]
            for name, value in sorted(inputs.items())
        ],
        'outputs': [
            [name, key.artifact_id, key.item_key]
            for name, key in sorted(outputs.items())
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    return f'{op_id}:{digest}'


def _bound_identity(value: BoundInput) -> list[object]:
    if isinstance(value, ArtifactRef):
        return ['one', value.key.artifact_id, value.key.item_key, value.version]
    if isinstance(value, BoundCollectionItem):
        ref = value.item.ref
        return ['each', ref.key.artifact_id, ref.key.item_key, ref.version]
    return [
        'all',
        value.ref.key.artifact_id,
        value.ref.version,
        [[item.ref.key.artifact_id, item.key, item.ref.version] for item in value.items],
    ]


__all__ = [
    'CollectionProjection',
    'PlanningDecision',
    'PlanningView',
    'RuntimeDefinition',
    'compile_operations',
    'plan_next',
]
