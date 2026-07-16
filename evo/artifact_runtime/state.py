from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .artifact import ArtifactKey, ArtifactRef, CollectionSnapshot
from .errors import DefinitionError
from .utils import _string, _text


RunStatus = Literal[
    'created',
    'running',
    'pausing',
    'paused',
    'cancelling',
    'cancelled',
    'failed',
    'completed',
]


@dataclass(frozen=True)
class InvocationSnapshot:
    invocation_id: str
    operation_id: str
    item_key: str = ''

    def __post_init__(self) -> None:
        _text(self.invocation_id, 'invocation_id')
        _text(self.operation_id, 'operation_id')
        _string(self.item_key, 'item_key')


@dataclass(frozen=True)
class RuntimeErrorInfo:
    kind: str
    message: str

    def __post_init__(self) -> None:
        _text(self.kind, 'runtime error kind')
        _text(self.message, 'runtime error message')


@dataclass(frozen=True)
class RuntimeSnapshot:
    run_id: str
    status: RunStatus = 'created'
    running: tuple[InvocationSnapshot, ...] = ()
    ready_count: int = 0
    completed_artifacts: Mapping[ArtifactKey, ArtifactRef] = field(default_factory=dict)
    collections: Mapping[ArtifactKey, CollectionSnapshot] = field(default_factory=dict)
    error: RuntimeErrorInfo | None = None

    def __post_init__(self) -> None:
        _text(self.run_id, 'run_id')
        if self.status not in {
            'created', 'running', 'pausing', 'paused', 'cancelling',
            'cancelled', 'failed', 'completed',
        }:
            raise DefinitionError(f'unknown run status: {self.status}')

        running = tuple(self.running)
        if not all(isinstance(item, InvocationSnapshot) for item in running):
            raise TypeError('running must contain InvocationSnapshot values')
        if len({item.invocation_id for item in running}) != len(running):
            raise DefinitionError('running invocation ids must be unique')

        if not isinstance(self.ready_count, int) or isinstance(self.ready_count, bool):
            raise TypeError('ready_count must be int')
        if self.ready_count < 0:
            raise DefinitionError('ready_count must be >= 0')

        completed = dict(self.completed_artifacts)
        for key, ref in completed.items():
            if not isinstance(key, ArtifactKey) or not isinstance(ref, ArtifactRef):
                raise TypeError('completed_artifacts must map ArtifactKey to ArtifactRef')
            if key != ref.key:
                raise DefinitionError('completed artifact key must match its ref')

        collections = dict(self.collections)
        for key, collection in collections.items():
            if not isinstance(key, ArtifactKey) or not isinstance(collection, CollectionSnapshot):
                raise TypeError('collections must map ArtifactKey to CollectionSnapshot')
            if key != collection.ref.key:
                raise DefinitionError('collection key must match its ref')

        if self.error is not None and not isinstance(self.error, RuntimeErrorInfo):
            raise TypeError('error must be RuntimeErrorInfo or None')
        if self.status == 'failed' and self.error is None:
            raise DefinitionError('failed runtime snapshot requires error details')
        if self.status != 'failed' and self.error is not None:
            raise DefinitionError('runtime error details are only valid for failed status')
        if self.status in {'created', 'paused', 'cancelled', 'failed', 'completed'} and running:
            raise DefinitionError(f'{self.status} runtime snapshot cannot contain running invocations')
        if self.status in {'cancelled', 'failed', 'completed'} and self.ready_count:
            raise DefinitionError(f'{self.status} runtime snapshot cannot contain ready invocations')

        object.__setattr__(self, 'running', running)
        object.__setattr__(self, 'completed_artifacts', MappingProxyType(completed))
        object.__setattr__(self, 'collections', MappingProxyType(collections))


__all__ = ['InvocationSnapshot', 'RunStatus', 'RuntimeErrorInfo', 'RuntimeSnapshot']
