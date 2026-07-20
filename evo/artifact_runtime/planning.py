from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import networkx as nx

from .artifact import (
    ArtifactCommit,
    ArtifactKey,
    ArtifactRecord,
    ArtifactRef,
    ArtifactSnapshot,
    PartitionSet,
    merge_refs,
)
from .errors import DefinitionError
from .operation import (
    BoundAggregate,
    BoundInput,
    Operation,
    OperationInvocation,
    OperationSpec,
)


@dataclass(frozen=True)
class PlanningDecision:
    view: ArtifactSnapshot
    invocations: tuple[OperationInvocation, ...]
    complete: bool
    blocked_reason: str = ''


@dataclass(frozen=True)
class RuntimeDefinition:
    operations: tuple[Operation, ...]
    artifact_modes: Mapping[str, str]
    partition_set_by_artifact: Mapping[str, str]

    def __post_init__(self) -> None:
        operations = tuple(self.operations)
        if not operations:
            raise DefinitionError('runtime definition requires at least one operation')
        object.__setattr__(self, 'operations', operations)
        object.__setattr__(self, 'artifact_modes', MappingProxyType(dict(self.artifact_modes)))
        object.__setattr__(
            self,
            'partition_set_by_artifact',
            MappingProxyType(dict(self.partition_set_by_artifact)),
        )

    @property
    def partition_set_ids(self) -> frozenset[str]:
        return frozenset(self.partition_set_by_artifact.values())

    def validate_commit(self, commit: ArtifactCommit) -> None:
        if not isinstance(commit, ArtifactCommit):
            raise TypeError('commit must be ArtifactCommit')
        partition_set_ids = self.partition_set_ids
        for write in commit.writes:
            mode = self.artifact_modes.get(write.key.artifact_id)
            if mode is None:
                raise DefinitionError(f'unknown artifact: {write.key.artifact_id}')
            if (mode == 'partitioned') != bool(write.key.partition_key):
                raise DefinitionError(
                    f'{write.key.artifact_id} requires a {mode} artifact key'
                )
            is_partition_set = write.key.artifact_id in partition_set_ids
            if is_partition_set != isinstance(write.value, PartitionSet):
                expected = 'PartitionSet' if is_partition_set else 'ordinary artifact value'
                raise DefinitionError(f'{write.key.artifact_id} requires {expected}')


def compile_operations(operations: Sequence[Operation]) -> RuntimeDefinition:
    declared = tuple(operations)
    if not declared:
        raise DefinitionError('at least one operation is required')

    by_id: dict[str, Operation] = {}
    artifact_modes: dict[str, str] = {}
    writer_by_artifact: dict[str, str] = {}
    partition_set_by_artifact: dict[str, str] = {}

    def declare_mode(artifact_id: str, mode: str) -> None:
        previous = artifact_modes.setdefault(artifact_id, mode)
        if previous != mode:
            raise DefinitionError(
                f'artifact {artifact_id} is used as both {previous} and {mode}'
            )

    def assign_partitions(artifact_id: str, partition_set_id: str) -> None:
        previous = partition_set_by_artifact.setdefault(artifact_id, partition_set_id)
        if previous != partition_set_id:
            raise DefinitionError(
                f'partitioned artifact {artifact_id} uses both {previous} and {partition_set_id}'
            )

    for operation in declared:
        spec = getattr(operation, 'spec', None)
        if not callable(operation) or not isinstance(spec, OperationSpec):
            raise TypeError('operations must contain declared Operation callables')
        if spec.op_id in by_id:
            raise DefinitionError(f'duplicate operation id: {spec.op_id}')
        by_id[spec.op_id] = operation

        for binding in spec.inputs.values():
            mode = 'scalar' if binding.mode == 'one' else 'partitioned'
            declare_mode(binding.artifact_id, mode)
            if binding.mode in {'each', 'all'}:
                declare_mode(binding.partition_set_id, 'scalar')
                assign_partitions(binding.artifact_id, binding.partition_set_id)

        if spec.driver_input is not None:
            partition_set_id = spec.partition_set_id
            for binding in spec.inputs.values():
                if binding.mode == 'keyed':
                    assign_partitions(binding.artifact_id, partition_set_id)

        for output in spec.outputs.values():
            declare_mode(output.artifact_id, output.mode)
            previous_writer = writer_by_artifact.get(output.artifact_id)
            if previous_writer is not None:
                raise DefinitionError(
                    f'artifact {output.artifact_id} has multiple writers: '
                    f'{previous_writer}, {spec.op_id}'
                )
            writer_by_artifact[output.artifact_id] = spec.op_id
            if output.mode == 'partitioned':
                partition_set_id = output.partition_set_id or spec.partition_set_id
                declare_mode(partition_set_id, 'scalar')
                assign_partitions(output.artifact_id, partition_set_id)

    graph = nx.DiGraph()
    graph.add_nodes_from(by_id)
    for operation in declared:
        dependencies = {binding.artifact_id for binding in operation.spec.inputs.values()}
        dependencies.update(
            binding.partition_set_id
            for binding in operation.spec.inputs.values()
            if binding.mode in {'each', 'all'}
        )
        for artifact_id in dependencies:
            writer = writer_by_artifact.get(artifact_id)
            if writer is not None:
                graph.add_edge(writer, operation.spec.op_id)

    try:
        order = tuple(nx.lexicographical_topological_sort(graph, key=str))
    except nx.NetworkXUnfeasible as exc:
        edges = nx.find_cycle(graph)
        cycle = ' -> '.join((edges[0][0], *(target for _, target in edges)))
        raise DefinitionError(
            f'operation dependencies must be acyclic: {cycle}'
        ) from exc
    ordered_operations = tuple(by_id[op_id] for op_id in order)
    return RuntimeDefinition(
        ordered_operations,
        artifact_modes,
        partition_set_by_artifact,
    )


def plan_next(definition: RuntimeDefinition, artifacts: ArtifactSnapshot) -> PlanningDecision:
    if not isinstance(definition, RuntimeDefinition):
        raise TypeError('definition must be RuntimeDefinition')
    if not isinstance(artifacts, ArtifactSnapshot):
        raise TypeError('artifacts must be ArtifactSnapshot')

    effective = _operation_effective_records(definition, artifacts)
    partition_sets = _effective_partition_sets(artifacts, effective)
    invocations = _plan_invocations(
        definition.operations,
        effective,
        partition_sets,
        artifacts,
    )
    complete = all(
        _outputs_complete(operation, None, effective, partition_sets)
        for operation in definition.operations
    )
    blocked_reason = ''
    if not complete and not invocations:
        blocked_reason = 'artifact planning stalled with missing outputs'
    return PlanningDecision(
        ArtifactSnapshot(effective, partition_sets),
        invocations,
        complete,
        blocked_reason,
    )


def _operation_effective_records(definition: RuntimeDefinition, artifacts: ArtifactSnapshot
                                 ) -> dict[ArtifactKey, ArtifactRecord]:
    effective = dict(artifacts.effective_records())
    changed = True
    while changed:
        changed = _remove_inactive_partitions(definition, artifacts, effective)
        partition_sets = _effective_partition_sets(artifacts, effective)
        for operation in definition.operations:
            if operation.spec.driver_input is None:
                changed |= _validate_batch_outputs(operation, effective, partition_sets)
            else:
                changed |= _validate_partitioned_outputs(operation, effective, partition_sets)
    return effective


def _remove_inactive_partitions(definition: RuntimeDefinition, artifacts: ArtifactSnapshot,
                                effective: dict[ArtifactKey, ArtifactRecord]
                                ) -> bool:
    changed = False
    partition_sets = _effective_partition_sets(artifacts, effective)
    for key in tuple(effective):
        if not key.partition_key:
            continue
        partition_set_id = definition.partition_set_by_artifact.get(key.artifact_id)
        if partition_set_id is None:
            continue
        partitions = partition_sets.get(ArtifactKey.scalar(partition_set_id))
        if partitions is None or key.partition_key not in partitions:
            del effective[key]
            changed = True
    return changed


def _validate_batch_outputs(operation: Operation, effective: dict[ArtifactKey, ArtifactRecord],
                            partition_sets: Mapping[ArtifactKey, PartitionSet]
                            ) -> bool:
    inputs = _bind_inputs(operation, effective, partition_sets, None)
    expected_inputs = None if inputs is None else _lineage_refs(inputs)
    changed = False
    for output in operation.spec.outputs.values():
        if output.mode == 'scalar':
            key = ArtifactKey.scalar(output.artifact_id)
            records = ((key, effective.get(key)),)
        else:
            records = tuple(
                (key, record)
                for key, record in effective.items()
                if key.artifact_id == output.artifact_id
            )
        for key, record in records:
            if record is None or not record.producer.startswith('operation:'):
                continue
            if (
                expected_inputs is None
                or record.producer != f'operation:{operation.spec.op_id}'
                or record.input_refs != expected_inputs
            ):
                del effective[key]
                changed = True
    return changed


def _validate_partitioned_outputs(operation: Operation, effective: dict[ArtifactKey, ArtifactRecord],
                                  partition_sets: Mapping[ArtifactKey, PartitionSet]
                                  ) -> bool:
    partition_keys = _partition_keys(operation, partition_sets)
    active_keys = set(() if partition_keys is None else partition_keys)
    changed = False
    for output in operation.spec.outputs.values():
        for key, record in tuple(effective.items()):
            if (
                key.artifact_id == output.artifact_id
                and key.partition_key not in active_keys
                and record.producer == f'operation:{operation.spec.op_id}'
            ):
                del effective[key]
                changed = True

    if partition_keys is None:
        return changed
    for partition_key in partition_keys:
        inputs = _bind_inputs(operation, effective, partition_sets, partition_key)
        expected_inputs = None if inputs is None else _lineage_refs(inputs)
        for output in operation.spec.outputs.values():
            key = ArtifactKey.partition(output.artifact_id, partition_key)
            record = effective.get(key)
            if record is None or not record.producer.startswith('operation:'):
                continue
            if (
                expected_inputs is None
                or record.producer != f'operation:{operation.spec.op_id}'
                or record.input_refs != expected_inputs
            ):
                del effective[key]
                changed = True
    return changed


def _effective_partition_sets(artifacts: ArtifactSnapshot, effective: Mapping[ArtifactKey, ArtifactRecord]
                              ) -> dict[ArtifactKey, PartitionSet]:
    return {
        key: partitions
        for key, partitions in artifacts.partition_sets.items()
        if key in effective
    }


def _plan_invocations(operations: tuple[Operation, ...], effective: Mapping[ArtifactKey, ArtifactRecord],
                      partition_sets: Mapping[ArtifactKey, PartitionSet], artifacts: ArtifactSnapshot
                      ) -> tuple[OperationInvocation, ...]:
    ready: list[OperationInvocation] = []
    for operation in operations:
        partition_keys = _partition_keys(operation, partition_sets)
        if partition_keys is None:
            continue
        invocation_keys: tuple[str | None, ...] = (
            tuple(partition_keys)
            if operation.spec.driver_input is not None
            else (None,)
        )
        for partition_key in invocation_keys:
            inputs = _bind_inputs(operation, effective, partition_sets, partition_key)
            if inputs is None:
                continue
            if _outputs_complete(
                operation,
                partition_key,
                effective,
                partition_sets,
            ):
                continue
            current_partition = '' if partition_key is None else partition_key
            expected_heads = _expected_heads(operation, current_partition, artifacts)
            ready.append(OperationInvocation(
                operation,
                inputs,
                expected_heads,
                current_partition,
            ))
    return tuple(ready)


def _expected_heads(operation: Operation, partition_key: str, artifacts: ArtifactSnapshot
                    ) -> dict[ArtifactKey, ArtifactRef | None]:
    expected: dict[ArtifactKey, ArtifactRef | None] = {}
    for output in operation.spec.outputs.values():
        if output.mode == 'partitioned' and not partition_key:
            expected.update(
                (key, record.ref)
                for key, record in artifacts.records.items()
                if key.artifact_id == output.artifact_id
            )
            continue

        key = output.key_for(partition_key)
        record = artifacts.records.get(key)
        expected[key] = None if record is None else record.ref
    return expected


def _outputs_complete(operation: Operation, partition_key: str | None,
                      effective: Mapping[ArtifactKey, ArtifactRecord],
                      partition_sets: Mapping[ArtifactKey, PartitionSet]
                      ) -> bool:
    for output in operation.spec.outputs.values():
        if output.mode == 'scalar':
            if ArtifactKey.scalar(output.artifact_id) not in effective:
                return False
            continue
        if partition_key is not None:
            if ArtifactKey.partition(output.artifact_id, partition_key) not in effective:
                return False
            continue
        partition_set_id = output.partition_set_id or operation.spec.partition_set_id
        partitions = partition_sets.get(ArtifactKey.scalar(partition_set_id))
        if partitions is None or any(
            ArtifactKey.partition(output.artifact_id, key) not in effective
            for key in partitions.keys
        ):
            return False
    return True


def _partition_keys(operation: Operation, partition_sets: Mapping[ArtifactKey, PartitionSet]
                    ) -> tuple[str, ...] | None:
    if operation.spec.driver_input is None:
        return ()
    key = ArtifactKey.scalar(operation.spec.partition_set_id)
    partitions = partition_sets.get(key)
    return None if partitions is None else partitions.keys


def _bind_inputs(operation: Operation, effective: Mapping[ArtifactKey, ArtifactRecord],
                 partition_sets: Mapping[ArtifactKey, PartitionSet], partition_key: str | None
                 ) -> dict[str, BoundInput] | None:
    inputs: dict[str, BoundInput] = {}
    for name, binding in operation.spec.inputs.items():
        if binding.mode == 'one':
            record = effective.get(ArtifactKey.scalar(binding.artifact_id))
            if record is None:
                return None
            inputs[name] = record.ref
            continue

        if binding.mode == 'all':
            partition_set_key = ArtifactKey.scalar(binding.partition_set_id)
            partition_record = effective.get(partition_set_key)
            partitions = partition_sets.get(partition_set_key)
            if partition_record is None or partitions is None:
                return None
            refs = []
            for current_partition in partitions.keys:
                record = effective.get(ArtifactKey.partition(
                    binding.artifact_id,
                    current_partition,
                ))
                if record is None:
                    return None
                refs.append(record.ref)
            inputs[name] = BoundAggregate(partition_record.ref, tuple(refs))
            continue

        if partition_key is None:
            return None
        record = effective.get(ArtifactKey.partition(binding.artifact_id, partition_key))
        if record is None:
            return None
        inputs[name] = record.ref
    return inputs


def _lineage_refs(inputs: Mapping[str, BoundInput]) -> tuple[ArtifactRef, ...]:
    refs: list[ArtifactRef] = []
    for value in inputs.values():
        if isinstance(value, ArtifactRef):
            refs.append(value)
        else:
            refs.append(value.partition_set_ref)
            refs.extend(value.member_refs)
    return merge_refs(refs)


__all__ = [
    'PlanningDecision', 'RuntimeDefinition', 'compile_operations', 'plan_next',
]
