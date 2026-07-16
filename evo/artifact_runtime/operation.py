from __future__ import annotations

import builtins
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, TypeVar, cast

from .artifact import (
    ArtifactKey,
    ArtifactRef,
    CollectionItem,
    CollectionItemGuard,
    CollectionResult,
    CollectionSnapshot,
    CollectionWrite,
    OperationWriteSet,
)
from .errors import DefinitionError
from .utils import _positive_int, _string, _text


BindingMode = Literal['one', 'each', 'keyed', 'all']
OutputMode = Literal['scalar', 'collection', 'per_item']
ExecutionMode = Literal['async', 'isolated']


@dataclass(frozen=True)
class BoundCollectionItem:
    collection_ref: ArtifactRef
    item_artifact_id: str
    item: CollectionItem

    def __post_init__(self) -> None:
        if not isinstance(self.collection_ref, ArtifactRef):
            raise TypeError('collection_ref must be ArtifactRef')
        if self.collection_ref.key.item_key:
            raise DefinitionError('collection_ref must be a scalar artifact ref')
        _text(self.item_artifact_id, 'item_artifact_id')
        if not isinstance(self.item, CollectionItem):
            raise TypeError('item must be CollectionItem')
        if self.item.ref.key.artifact_id != self.item_artifact_id:
            raise DefinitionError('bound item must match item_artifact_id')


BoundInput = ArtifactRef | BoundCollectionItem | CollectionSnapshot


@dataclass(frozen=True)
class InputBinding:
    artifact_id: str
    mode: BindingMode

    def __post_init__(self) -> None:
        _text(self.artifact_id, 'input artifact_id')
        if self.mode not in {'one', 'each', 'keyed', 'all'}:
            raise DefinitionError(f'unknown input binding mode: {self.mode}')

    def validate_value(self, name: str, value: BoundInput, item_key: str) -> None:
        collection_key = ArtifactKey.scalar(self.artifact_id)
        if self.mode == 'one':
            if not isinstance(value, ArtifactRef):
                raise TypeError(f'{name} one binding must contain ArtifactRef')
            if value.key != collection_key:
                raise DefinitionError(f'{name} ref does not match its one binding')
            return

        if self.mode == 'all':
            if not isinstance(value, CollectionSnapshot):
                raise TypeError(f'{name} all binding must contain CollectionSnapshot')
            if value.ref.key != collection_key:
                raise DefinitionError(f'{name} collection does not match its all binding')
            return

        if not isinstance(value, BoundCollectionItem):
            raise TypeError(f'{name} {self.mode} binding must contain BoundCollectionItem')
        if value.collection_ref.key != collection_key:
            raise DefinitionError(f'{name} collection does not match its {self.mode} binding')
        if value.item.key != item_key:
            raise DefinitionError(f'{name} item key does not match invocation item_key')


def one(artifact_id: str) -> InputBinding:
    return InputBinding(artifact_id, 'one')


def each(artifact_id: str) -> InputBinding:
    return InputBinding(artifact_id, 'each')


def keyed(artifact_id: str) -> InputBinding:
    return InputBinding(artifact_id, 'keyed')


def all(artifact_id: str) -> InputBinding:  # noqa: A001
    return InputBinding(artifact_id, 'all')


@dataclass(frozen=True)
class OutputSpec:
    artifact_id: str
    mode: OutputMode = 'scalar'
    item_artifact_id: str = ''

    def __post_init__(self) -> None:
        _text(self.artifact_id, 'output artifact_id')
        if self.mode not in {'scalar', 'collection', 'per_item'}:
            raise DefinitionError(f'unknown output mode: {self.mode}')
        if self.mode == 'scalar':
            if self.item_artifact_id:
                raise DefinitionError('scalar output cannot declare item_artifact_id')
        else:
            _text(self.item_artifact_id, 'item_artifact_id')

    def key_for(self, item_key: str) -> ArtifactKey:
        if self.mode == 'per_item':
            return ArtifactKey.item(self.item_artifact_id, item_key)
        return ArtifactKey.scalar(self.artifact_id)


def scalar(artifact_id: str) -> OutputSpec:
    return OutputSpec(artifact_id)


def collection(artifact_id: str, item_artifact_id: str) -> OutputSpec:
    return OutputSpec(artifact_id, 'collection', item_artifact_id)


def per_item(artifact_id: str, item_artifact_id: str) -> OutputSpec:
    return OutputSpec(artifact_id, 'per_item', item_artifact_id)


@dataclass(frozen=True)
class OperationSpec:
    op_id: str
    inputs: Mapping[str, InputBinding]
    outputs: Mapping[str, OutputSpec]
    execution: ExecutionMode = 'isolated'
    max_concurrency: int = 1
    driver_input: str | None = field(init=False)

    def __post_init__(self) -> None:
        _text(self.op_id, 'op_id')
        inputs = dict(self.inputs)
        outputs = dict(self.outputs)
        if not outputs:
            raise DefinitionError('operation must declare at least one output')

        for name, binding in inputs.items():
            _text(name, 'input name')
            if not isinstance(binding, InputBinding):
                raise TypeError('operation inputs must contain InputBinding values')
        if len({binding.artifact_id for binding in inputs.values()}) != len(inputs):
            raise DefinitionError('operation input artifact ids must be unique')

        for name, output in outputs.items():
            _text(name, 'output name')
            if not isinstance(output, OutputSpec):
                raise TypeError('operation outputs must contain OutputSpec values')
        if len({output.artifact_id for output in outputs.values()}) != len(outputs):
            raise DefinitionError('operation output artifact ids must be unique')
        item_artifact_ids = [
            output.item_artifact_id for output in outputs.values()
            if output.item_artifact_id
        ]
        if len(set(item_artifact_ids)) != len(item_artifact_ids):
            raise DefinitionError('operation output item artifact ids must be unique')

        each_count = sum(binding.mode == 'each' for binding in inputs.values())
        if each_count > 1:
            raise DefinitionError('operation supports exactly one driving each input')
        has_each = each_count == 1
        if any(binding.mode == 'keyed' for binding in inputs.values()) and not has_each:
            raise DefinitionError('keyed inputs require one driving each input')
        has_per_item = any(output.mode == 'per_item' for output in outputs.values())
        if has_each and not builtins.all(output.mode == 'per_item' for output in outputs.values()):
            raise DefinitionError('operation with each input must use only per_item outputs')
        if has_each != has_per_item:
            raise DefinitionError('per_item outputs require an each input')

        driver_input = next(
            (name for name, binding in inputs.items() if binding.mode == 'each'),
            None,
        )

        if self.execution not in {'async', 'isolated'}:
            raise DefinitionError(f'unknown execution mode: {self.execution}')
        _positive_int(self.max_concurrency, 'max_concurrency')

        object.__setattr__(self, 'inputs', MappingProxyType(inputs))
        object.__setattr__(self, 'outputs', MappingProxyType(outputs))
        object.__setattr__(self, 'driver_input', driver_input)


@dataclass(frozen=True)
class OperationContext:
    run_id: str
    invocation_id: str
    item_key: str = ''

    def __post_init__(self) -> None:
        _text(self.run_id, 'run_id')
        _text(self.invocation_id, 'invocation_id')
        _string(self.item_key, 'item_key')


@dataclass(frozen=True)
class OperationResult:
    scalars: Mapping[str, object] = field(default_factory=dict)
    collections: Mapping[str, CollectionResult] = field(default_factory=dict)

    def __post_init__(self) -> None:
        scalars = dict(self.scalars)
        collections = dict(self.collections)
        for name in (*scalars, *collections):
            _text(name, 'operation result name')
        if set(scalars) & set(collections):
            raise DefinitionError('scalar and collection result names must not overlap')
        if not builtins.all(isinstance(value, CollectionResult) for value in collections.values()):
            raise TypeError('collections must contain CollectionResult values')
        object.__setattr__(self, 'scalars', MappingProxyType(scalars))
        object.__setattr__(self, 'collections', MappingProxyType(collections))

    def validate_for(self, spec: OperationSpec) -> OperationResult:
        if not isinstance(spec, OperationSpec):
            raise TypeError('spec must be OperationSpec')
        scalar_names = {
            name for name, output in spec.outputs.items()
            if output.mode in {'scalar', 'per_item'}
        }
        collection_names = {
            name for name, output in spec.outputs.items()
            if output.mode == 'collection'
        }
        if set(self.scalars) != scalar_names or set(self.collections) != collection_names:
            raise DefinitionError(f'{spec.op_id} result names and kinds must match declared outputs')
        return self


class Operation(Protocol):
    spec: OperationSpec
    __module__: str
    __qualname__: str

    async def __call__(self, ctx: OperationContext, **inputs: object) -> OperationResult:
        ...


OperationFunction = Callable[..., Awaitable[OperationResult]]
F = TypeVar('F', bound=OperationFunction)


def operation(
    *, op_id: str, inputs: Mapping[str, InputBinding], outputs: Mapping[str, OutputSpec],
    execution: ExecutionMode = 'isolated', max_concurrency: int = 1,
) -> Callable[[F], F]:
    spec = OperationSpec(op_id, inputs, outputs, execution, max_concurrency)

    def decorate(function: F) -> F:
        if not inspect.iscoroutinefunction(function):
            raise DefinitionError(f'{op_id} must be declared with async def')
        if '<locals>' in function.__qualname__:
            raise DefinitionError(f'{op_id} must be declared at module scope')
        if hasattr(function, 'spec'):
            raise DefinitionError(f'{op_id} function already declares an operation spec')
        _validate_signature(function, spec)
        function.spec = spec  # type: ignore[attr-defined]
        return cast(F, function)

    return decorate


def _validate_signature(function: OperationFunction, spec: OperationSpec) -> None:
    parameters = tuple(inspect.signature(function).parameters.values())
    if not parameters or parameters[0].name != 'ctx':
        raise DefinitionError(f'{spec.op_id} first parameter must be named ctx')
    if parameters[0].kind not in {inspect.Parameter.POSITIONAL_ONLY,
                                  inspect.Parameter.POSITIONAL_OR_KEYWORD}:
        raise DefinitionError(f'{spec.op_id} ctx parameter must be positional')
    arguments = parameters[1:]
    if builtins.any(parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
                    for parameter in arguments):
        raise DefinitionError(f'{spec.op_id} must not use variadic input parameters')
    if {parameter.name for parameter in arguments} != set(spec.inputs):
        raise DefinitionError(f'{spec.op_id} parameters must match declared input names')
    if builtins.any(parameter.default is not inspect.Parameter.empty for parameter in arguments):
        raise DefinitionError(f'{spec.op_id} input parameters must not declare defaults')


@dataclass(frozen=True)
class OperationInvocation:
    invocation_id: str
    operation: Operation
    inputs: Mapping[str, BoundInput]
    output_keys: Mapping[str, ArtifactKey]
    item_key: str = ''

    def __post_init__(self) -> None:
        _text(self.invocation_id, 'invocation_id')
        if not callable(self.operation) or not isinstance(getattr(self.operation, 'spec', None), OperationSpec):
            raise TypeError('operation must be a declared Operation')

        inputs = dict(self.inputs)
        outputs = dict(self.output_keys)
        if set(inputs) != set(self.operation.spec.inputs):
            raise DefinitionError('invocation inputs must match operation inputs')
        if set(outputs) != set(self.operation.spec.outputs):
            raise DefinitionError('invocation output keys must match operation outputs')
        if len(set(outputs.values())) != len(outputs):
            raise DefinitionError('invocation output artifact keys must be unique')
        _string(self.item_key, 'item_key')

        for name, binding in self.operation.spec.inputs.items():
            binding.validate_value(name, inputs[name], self.item_key)
        if (self.operation.spec.driver_input is not None) != bool(self.item_key):
            raise DefinitionError('invocation item_key must be set exactly when an each input is bound')

        for name, output in self.operation.spec.outputs.items():
            key = outputs[name]
            if not isinstance(key, ArtifactKey):
                raise TypeError('invocation outputs must be ArtifactKey values')
            if key != output.key_for(self.item_key):
                raise DefinitionError(f'{name} output key does not match its declaration')

        object.__setattr__(self, 'inputs', MappingProxyType(inputs))
        object.__setattr__(self, 'output_keys', MappingProxyType(outputs))

    def value_refs(self) -> tuple[ArtifactRef, ...]:
        refs: list[ArtifactRef] = []
        for value in self.inputs.values():
            if isinstance(value, ArtifactRef):
                refs.append(value)
            elif isinstance(value, BoundCollectionItem):
                refs.append(value.item.ref)
            else:
                refs.extend(item.ref for item in value.items)
        return tuple(refs)

    def bind_values(self, values: Mapping[ArtifactRef, object]) -> Mapping[str, object]:
        bound: dict[str, object] = {}
        for name, value in self.inputs.items():
            if isinstance(value, ArtifactRef):
                bound[name] = values[value]
            elif isinstance(value, BoundCollectionItem):
                bound[name] = values[value.item.ref]
            else:
                bound[name] = {item.key: values[item.ref] for item in value.items}
        return MappingProxyType(bound)

    def operation_writes(self, result: OperationResult) -> OperationWriteSet:
        result.validate_for(self.operation.spec)

        input_refs: list[ArtifactRef] = []
        item_guards: list[CollectionItemGuard] = []
        collection_guards: list[CollectionSnapshot] = []
        for value in self.inputs.values():
            if isinstance(value, ArtifactRef):
                input_refs.append(value)
            elif isinstance(value, BoundCollectionItem):
                input_refs.append(value.item.ref)
                item_guards.append(CollectionItemGuard(value.collection_ref.key, value.item))
            else:
                input_refs.append(value.ref)
                input_refs.extend(item.ref for item in value.items)
                collection_guards.append(value)

        scalar_values = {
            self.output_keys[name]: result.scalars[name]
            for name, output in self.operation.spec.outputs.items()
            if output.mode in {'scalar', 'per_item'}
        }
        collection_writes = tuple(
            CollectionWrite(
                self.output_keys[name],
                output.item_artifact_id,
                result.collections[name].items,
            )
            for name, output in self.operation.spec.outputs.items()
            if output.mode == 'collection'
        )

        return OperationWriteSet(
            self.invocation_id,
            self.operation.spec.op_id,
            tuple(input_refs),
            scalar_values,
            collection_writes,
            tuple(item_guards),
            tuple(collection_guards),
        )


__all__ = [
    'BoundInput',
    'BoundCollectionItem',
    'ExecutionMode',
    'InputBinding',
    'Operation',
    'OperationContext',
    'OperationInvocation',
    'OperationResult',
    'OperationSpec',
    'OutputSpec',
    'all',
    'collection',
    'each',
    'keyed',
    'one',
    'operation',
    'per_item',
    'scalar',
]
