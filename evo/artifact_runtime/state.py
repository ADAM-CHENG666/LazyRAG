from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, get_args

from .artifact import ArtifactKey, ArtifactRecord, ArtifactRef, PartitionSet
from .errors import (
    DefinitionError,
    _integer,
    _known,
    _number,
    _string,
    _text,
    _tuple_of,
    _unique,
)


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

AttemptStatus = Literal[
    'scheduled',
    'running',
    'cancelling',
    'cancelled',
    'succeeded',
    'failed',
    'interrupted',
    'discarded',
]

RetryStatus = Literal['pending', 'fulfilled', 'cancelled']
InterventionStatus = Literal['pending', 'processing', 'consumed']
CaseOperationStatus = Literal['pending', 'running', 'succeeded', 'failed']
CaseStatus = Literal['pending', 'running', 'completed', 'failed']


@dataclass(frozen=True)
class RunConfiguration:
    values: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = dict(self.values)
        for key in values:
            _text(key, 'run configuration key')
        try:
            json.dumps(values, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise DefinitionError('run configuration must be JSON-serializable') from exc
        object.__setattr__(self, 'values', values)


@dataclass(frozen=True)
class InvocationSnapshot:
    invocation_id: str
    operation_id: str
    partition_key: str = ''


@dataclass(frozen=True)
class RuntimeErrorInfo:
    kind: str
    message: str

    def __post_init__(self) -> None:
        _text(self.kind, 'runtime error kind')
        _text(self.message, 'runtime error message')


@dataclass(frozen=True)
class ProgressUpdate:
    phase: str
    message: str = ''
    current: int | None = None
    total: int | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.phase, 'progress phase')
        _string(self.message, 'progress message')
        for name, value in (('current', self.current), ('total', self.total)):
            if value is not None:
                _integer(value, f'progress {name}')
        if self.current is not None and self.total is not None and self.current > self.total:
            raise DefinitionError('progress current cannot exceed total')
        detail = dict(self.detail)
        try:
            json.dumps(detail, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise DefinitionError('progress detail must be JSON-serializable') from exc
        object.__setattr__(self, 'detail', MappingProxyType(detail))


@dataclass(frozen=True)
class AttemptSnapshot:
    attempt_id: str
    invocation_id: str
    operation_id: str
    partition_key: str
    status: AttemptStatus
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: RuntimeErrorInfo | None = None
    input_refs: tuple[ArtifactRef, ...] = ()
    output_keys: tuple[ArtifactKey, ...] = ()
    retry_request_id: str = ''

    def __post_init__(self) -> None:
        _text(self.attempt_id, 'attempt_id')
        _text(self.invocation_id, 'invocation_id')
        _text(self.operation_id, 'operation_id')
        _string(self.partition_key, 'partition_key')
        _known(self.status, get_args(AttemptStatus), 'attempt status')
        for name, value in (
            ('created_at', self.created_at),
            ('started_at', self.started_at),
            ('finished_at', self.finished_at),
        ):
            _number(value, name, optional=True)
        if self.status == 'failed' and self.error is None:
            raise DefinitionError('failed attempt requires error details')
        if self.status != 'failed' and self.error is not None:
            raise DefinitionError('attempt error details are only valid for failed status')
        input_refs = _tuple_of(self.input_refs, ArtifactRef,
                               'attempt input_refs must contain ArtifactRef values')
        output_keys = _tuple_of(self.output_keys, ArtifactKey,
                                'attempt output_keys must contain ArtifactKey values')
        _string(self.retry_request_id, 'retry_request_id')
        object.__setattr__(self, 'input_refs', input_refs)
        object.__setattr__(self, 'output_keys', output_keys)


@dataclass(frozen=True)
class ArtifactRetryRequest:
    request_id: str
    artifact_key: ArtifactKey
    base_ref: ArtifactRef
    status: RetryStatus
    created_at: float
    result_ref: ArtifactRef | None = None

    def __post_init__(self) -> None:
        _text(self.request_id, 'retry request_id')
        if not isinstance(self.artifact_key, ArtifactKey):
            raise TypeError('retry artifact_key must be ArtifactKey')
        if not isinstance(self.base_ref, ArtifactRef):
            raise TypeError('retry base_ref must be ArtifactRef')
        if self.base_ref.key != self.artifact_key:
            raise DefinitionError('retry base_ref must identify artifact_key')
        _known(self.status, get_args(RetryStatus), 'retry status')
        _number(self.created_at, 'retry created_at')
        if self.status == 'fulfilled':
            if not isinstance(self.result_ref, ArtifactRef):
                raise DefinitionError('fulfilled retry requires result_ref')
            if self.result_ref.key != self.artifact_key:
                raise DefinitionError('retry result_ref must identify artifact_key')
            if self.result_ref.version <= self.base_ref.version:
                raise DefinitionError('retry result_ref must be newer than base_ref')
        elif self.result_ref is not None:
            raise DefinitionError('only fulfilled retry can contain result_ref')


@dataclass(frozen=True)
class ProgressEvent:
    attempt_id: str
    sequence: int
    update: ProgressUpdate
    created_at: float

    def __post_init__(self) -> None:
        _text(self.attempt_id, 'attempt_id')
        _integer(self.sequence, 'progress sequence', minimum=1)
        if not isinstance(self.update, ProgressUpdate):
            raise TypeError('update must be ProgressUpdate')
        _number(self.created_at, 'created_at')


@dataclass(frozen=True)
class CaseFailure:
    attempt_id: str
    invocation_id: str
    operation_id: str
    case_id: str
    error: RuntimeErrorInfo
    input_refs: tuple[ArtifactRef, ...]
    output_keys: tuple[ArtifactKey, ...]
    failed_at: float

    def __post_init__(self) -> None:
        _text(self.attempt_id, 'case failure attempt_id')
        _text(self.invocation_id, 'case failure invocation_id')
        _text(self.operation_id, 'case failure operation_id')
        _text(self.case_id, 'case failure case_id')
        if not isinstance(self.error, RuntimeErrorInfo):
            raise TypeError('case failure error must be RuntimeErrorInfo')
        inputs = _tuple_of(self.input_refs, ArtifactRef,
                           'case failure input_refs must contain ArtifactRef values')
        outputs = _tuple_of(self.output_keys, ArtifactKey,
                            'case failure output_keys must contain ArtifactKey values',
                            nonempty=True)
        if any(key.partition_key != self.case_id for key in outputs):
            raise DefinitionError('case failure output keys must identify its case')
        _number(self.failed_at, 'case failure failed_at')
        object.__setattr__(self, 'input_refs', inputs)
        object.__setattr__(self, 'output_keys', outputs)


@dataclass(frozen=True)
class UserIntervention:
    intervention_id: str
    operation_id: str
    target_key: ArtifactKey
    target_ref: ArtifactRef | None
    message: str
    field: str
    quote: str
    start: int | None
    end: int | None
    created_at: float
    introduced_version: int

    def __post_init__(self) -> None:
        _text(self.intervention_id, 'intervention_id')
        _text(self.operation_id, 'intervention operation_id')
        if not isinstance(self.target_key, ArtifactKey):
            raise TypeError('intervention target_key must be ArtifactKey')
        if self.target_ref is not None and (
            not isinstance(self.target_ref, ArtifactRef)
            or self.target_ref.key != self.target_key
        ):
            raise DefinitionError('intervention target_ref must identify target_key')
        _text(self.message, 'intervention message')
        _string(self.field, 'intervention field')
        _string(self.quote, 'intervention quote')
        if (self.start is None) != (self.end is None):
            raise DefinitionError('intervention start and end must both be set or omitted')
        if self.start is not None:
            _integer(self.start, 'intervention start')
            _integer(self.end, 'intervention end')
            if self.end < self.start:
                raise DefinitionError('intervention end must not precede start')
        _number(self.created_at, 'intervention created_at')
        _integer(self.introduced_version, 'intervention introduced_version', minimum=1)

    @property
    def case_id(self) -> str:
        return self.target_key.partition_key


@dataclass(frozen=True)
class InterventionBundle:
    operation_id: str
    partition_key: str
    interventions: tuple[UserIntervention, ...]

    def __post_init__(self) -> None:
        _text(self.operation_id, 'intervention bundle operation_id')
        _string(self.partition_key, 'intervention bundle partition_key')
        interventions = _tuple_of(self.interventions, UserIntervention,
                                  'intervention bundle must contain UserIntervention values',
                                  nonempty=True)
        _unique(interventions, 'intervention ids must be unique within one invocation',
                key=lambda item: item.intervention_id)
        if any(item.operation_id != self.operation_id for item in interventions):
            raise DefinitionError('intervention operation must match its bundle')
        if self.partition_key and any(
            item.case_id != self.partition_key for item in interventions
        ):
            raise DefinitionError('partitioned intervention must match its bundle case')
        object.__setattr__(self, 'interventions', interventions)


@dataclass(frozen=True)
class InterventionSnapshot:
    intervention: UserIntervention
    status: InterventionStatus
    consumed_by_attempt_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.intervention, UserIntervention):
            raise TypeError('intervention snapshot requires UserIntervention')
        _known(self.status, get_args(InterventionStatus), 'intervention status')
        attempts = tuple(self.consumed_by_attempt_ids)
        if not all(isinstance(attempt_id, str) and attempt_id for attempt_id in attempts):
            raise TypeError('consumed attempt ids must be non-empty strings')
        _unique(attempts, 'consumed attempt ids must be unique')
        if self.status == 'pending' and attempts:
            raise DefinitionError('pending intervention cannot have consuming attempts')
        if self.status != 'pending' and not attempts:
            raise DefinitionError('adopted intervention requires a consuming attempt')
        object.__setattr__(self, 'consumed_by_attempt_ids', attempts)


@dataclass(frozen=True)
class RuntimeProgress:
    total: int = 0
    completed: int = 0
    running: int = 0
    failed: int = 0
    pending: int = 0
    percentage: float = 0.0
    case_total: int = 0
    case_completed: int = 0
    case_running: int = 0
    case_failed: int = 0
    case_pending: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.total,
            self.completed,
            self.running,
            self.failed,
            self.pending,
            self.case_total,
            self.case_completed,
            self.case_running,
            self.case_failed,
            self.case_pending,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in counts
        ):
            raise DefinitionError('runtime progress counts must be non-negative integers')
        if self.completed + self.running + self.failed + self.pending != self.total:
            raise DefinitionError('runtime progress operation counts must sum to total')
        if (
            self.case_completed
            + self.case_running
            + self.case_failed
            + self.case_pending
            != self.case_total
        ):
            raise DefinitionError('runtime progress case counts must sum to case_total')
        if (
            not isinstance(self.percentage, (int, float))
            or isinstance(self.percentage, bool)
            or not 0 <= self.percentage <= 100
        ):
            raise DefinitionError('runtime progress percentage must be between 0 and 100')


@dataclass(frozen=True)
class CaseOperationSnapshot:
    operation_id: str
    status: CaseOperationStatus
    output_refs: tuple[ArtifactRef, ...] = ()
    latest_attempt_id: str = ''
    retry_count: int = 0
    latest_progress: ProgressUpdate | None = None
    error: RuntimeErrorInfo | None = None

    def __post_init__(self) -> None:
        _text(self.operation_id, 'case operation_id')
        _known(self.status, get_args(CaseOperationStatus), 'case operation status')
        outputs = _tuple_of(self.output_refs, ArtifactRef,
                            'case operation output_refs must contain ArtifactRef values')
        _string(self.latest_attempt_id, 'case latest_attempt_id')
        _integer(self.retry_count, 'case retry_count')
        if self.latest_progress is not None and not isinstance(
            self.latest_progress, ProgressUpdate
        ):
            raise TypeError('case latest_progress must be ProgressUpdate or None')
        if self.error is not None and not isinstance(self.error, RuntimeErrorInfo):
            raise TypeError('case operation error must be RuntimeErrorInfo or None')
        if self.status == 'failed' and self.error is None:
            raise DefinitionError('failed case operation requires error details')
        object.__setattr__(self, 'output_refs', outputs)


@dataclass(frozen=True)
class CaseSnapshot:
    run_id: str
    case_id: str
    display_index: int
    status: CaseStatus
    operations: tuple[CaseOperationSnapshot, ...]
    artifacts: Mapping[ArtifactKey, ArtifactRef] = field(default_factory=dict)
    failures: tuple[CaseFailure, ...] = ()
    interventions: tuple[InterventionSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _text(self.run_id, 'case snapshot run_id')
        _text(self.case_id, 'case snapshot case_id')
        _integer(self.display_index, 'case display_index', minimum=1)
        _known(self.status, get_args(CaseStatus), 'case status')
        operations = _tuple_of(self.operations, CaseOperationSnapshot,
                               'case operations must contain CaseOperationSnapshot values')
        _unique(operations, 'case operation ids must be unique',
                key=lambda item: item.operation_id)
        artifacts = dict(self.artifacts)
        for key, ref in artifacts.items():
            if (
                not isinstance(key, ArtifactKey)
                or not isinstance(ref, ArtifactRef)
                or ref.key != key
                or key.partition_key != self.case_id
            ):
                raise DefinitionError('case artifacts must identify this case')
        failures = _tuple_of(self.failures, CaseFailure,
                             'case failures must contain CaseFailure values')
        if any(failure.case_id != self.case_id for failure in failures):
            raise DefinitionError('case failures must identify this case')
        interventions = _tuple_of(self.interventions, InterventionSnapshot,
                                  'case interventions must contain InterventionSnapshot values')
        if any(item.intervention.case_id != self.case_id for item in interventions):
            raise DefinitionError('case interventions must identify this case')
        object.__setattr__(self, 'operations', operations)
        object.__setattr__(self, 'artifacts', MappingProxyType(artifacts))
        object.__setattr__(self, 'failures', failures)
        object.__setattr__(self, 'interventions', interventions)


@dataclass(frozen=True)
class RuntimeSnapshot:
    run_id: str
    status: RunStatus = 'created'
    running: tuple[InvocationSnapshot, ...] = ()
    ready_count: int = 0
    completed_artifacts: Mapping[ArtifactKey, ArtifactRef] = field(default_factory=dict)
    partition_sets: Mapping[ArtifactKey, PartitionSet] = field(default_factory=dict)
    error: RuntimeErrorInfo | None = None
    active_attempts: tuple[AttemptSnapshot, ...] = ()
    awaiting_artifacts: tuple[ArtifactKey, ...] = ()
    case_failures: tuple[CaseFailure, ...] = ()
    interventions: tuple[InterventionSnapshot, ...] = ()
    progress: RuntimeProgress = field(default_factory=RuntimeProgress)

    def __post_init__(self) -> None:
        _text(self.run_id, 'run_id')
        _known(self.status, get_args(RunStatus), 'run status')

        running = _tuple_of(self.running, InvocationSnapshot,
                            'running must contain InvocationSnapshot values')
        _unique(running, 'running invocation ids must be unique',
                key=lambda item: item.invocation_id)

        _integer(self.ready_count, 'ready_count')

        completed = dict(self.completed_artifacts)
        for key, ref in completed.items():
            if not isinstance(key, ArtifactKey) or not isinstance(ref, ArtifactRef):
                raise TypeError('completed_artifacts must map ArtifactKey to ArtifactRef')
            if key != ref.key:
                raise DefinitionError('completed artifact key must match its ref')

        partition_sets = dict(self.partition_sets)
        for key, partitions in partition_sets.items():
            if not isinstance(key, ArtifactKey) or key.partition_key:
                raise TypeError('partition_sets keys must be scalar ArtifactKey values')
            if not isinstance(partitions, PartitionSet):
                raise TypeError('partition_sets values must be PartitionSet')

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

        attempts = _tuple_of(self.active_attempts, AttemptSnapshot,
                             'attempts must contain AttemptSnapshot values')
        _unique(attempts, 'attempt ids must be unique', key=lambda attempt: attempt.attempt_id)

        awaiting = _tuple_of(self.awaiting_artifacts, ArtifactKey,
                             'awaiting_artifacts must contain ArtifactKey values')
        _unique(awaiting, 'awaiting artifact keys must be unique')

        failures = _tuple_of(self.case_failures, CaseFailure,
                             'case_failures must contain CaseFailure values')
        _unique(failures, 'case failure attempt ids must be unique',
                key=lambda failure: failure.attempt_id)

        interventions = _tuple_of(self.interventions, InterventionSnapshot,
                                  'interventions must contain InterventionSnapshot values')
        _unique(interventions, 'runtime intervention ids must be unique',
                key=lambda item: item.intervention.intervention_id)
        if not isinstance(self.progress, RuntimeProgress):
            raise TypeError('runtime progress must be RuntimeProgress')

        object.__setattr__(self, 'running', running)
        object.__setattr__(self, 'completed_artifacts', MappingProxyType(completed))
        object.__setattr__(self, 'partition_sets', MappingProxyType(partition_sets))
        object.__setattr__(self, 'active_attempts', attempts)
        object.__setattr__(self, 'awaiting_artifacts', awaiting)
        object.__setattr__(self, 'case_failures', failures)
        object.__setattr__(self, 'interventions', interventions)


@dataclass(frozen=True)
class OperationDefinitionSnapshot:
    operation_id: str
    inputs: tuple[tuple[str, str, str, str], ...]
    outputs: tuple[tuple[str, str, str], ...]
    execution: str
    max_concurrency: int
    timeout: float | None = None


@dataclass(frozen=True)
class RunHistory:
    snapshot: RuntimeSnapshot
    operations: tuple[OperationDefinitionSnapshot, ...]
    artifacts: tuple[ArtifactRecord, ...]
    attempts: tuple[AttemptSnapshot, ...]
    progress_events: tuple[ProgressEvent, ...]
    retry_requests: tuple[ArtifactRetryRequest, ...]
    interventions: tuple[UserIntervention, ...]


__all__ = [
    'ArtifactRetryRequest', 'AttemptSnapshot', 'AttemptStatus', 'CaseFailure',
    'CaseOperationSnapshot', 'CaseSnapshot', 'InterventionSnapshot',
    'InterventionStatus', 'InvocationSnapshot', 'OperationDefinitionSnapshot',
    'ProgressEvent', 'ProgressUpdate', 'RetryStatus', 'RunConfiguration', 'RunHistory',
    'RunStatus', 'RuntimeErrorInfo', 'RuntimeProgress', 'RuntimeSnapshot',
    'UserIntervention',
]
