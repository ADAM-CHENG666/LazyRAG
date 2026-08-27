from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import math
import re
import uuid
from collections.abc import Mapping
from typing import Any

from evo import artifacts as A
from evo.artifact_flow import FlowDefinition, FlowSnapshot
from evo.artifact_runtime import (
    ArtifactKey,
    ArtifactRecord,
    ArtifactRef,
    AttemptSnapshot,
    PartitionSet,
    RecordedOperationEvent,
)
from evo.operations.dataset.chunks_build import BuildChunksParams
from evo.operations.dataset.kb_client import KnowledgeBaseClient
from evo.operations.dataset.qaplan import LANES, _lane_counts
from evo.operations.dataset.qaplan_capacity import project_automatic_plan as _project_cases_automatic_plan

from .contracts import ServiceError
from .public import public_thread_state, public_value


class ProjectionService:
    def __init__(self, flow: Any, definition: FlowDefinition, *, capability_client: KnowledgeBaseClient | Any | None = None) -> None:
        self.flow = flow
        self.definition = definition
        self.capability_client = capability_client

    @staticmethod
    def _validate_page_size(value: object) -> int:
        if value is None:
            return 50
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
            raise ServiceError(400, 'page_size must be an integer between 1 and 200')
        return value

    @staticmethod
    def _normalize_filters(filters: Mapping[object, object]) -> tuple[tuple[str, object], ...]:
        normalized: list[tuple[str, object]] = []
        for key, value in filters.items():
            if not isinstance(key, str) or not key:
                raise ServiceError(400, 'filter keys must be non-empty strings')
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                raise ServiceError(400, f'filter {key} must be a scalar value')
            if isinstance(value, float) and not math.isfinite(value):
                raise ServiceError(400, f'filter {key} must be finite')
            normalized.append((key, value))
        return tuple(sorted(normalized))

    @staticmethod
    def _build_revision(refs: tuple[ArtifactRef, ...]) -> str:
        if not refs or any(not isinstance(ref, ArtifactRef) for ref in refs):
            raise ServiceError(400, 'revision requires one or more artifact refs')
        payload = [[ref.key.artifact_id, ref.key.partition_key, ref.version] for ref in sorted(refs)]
        return _encode_context('r1', {'refs': payload})

    @staticmethod
    def _build_execution_revision(statuses: Mapping[str, Mapping[str, str]]) -> str:
        cases: list[list[object]] = []
        for case_id, operations in sorted(statuses.items()):
            if not isinstance(case_id, str) or not case_id:
                raise ServiceError(400, 'execution revision case id is invalid')
            cases.append([case_id, [
                operations.get('dataset.qaplan_spec', 'pending'),
                operations.get('dataset.generate_case', 'pending'),
                operations.get('dataset.enhance_case', 'pending'),
            ]])
        return _encode_context('e1', {'cases': cases})

    @staticmethod
    def _resolve_revision(revision: str) -> tuple[ArtifactRef, ...]:
        payload = _decode_context(revision, 'r1', 'revision')
        refs = payload.get('refs')
        if not isinstance(refs, list) or not refs:
            raise ServiceError(400, 'revision is invalid')
        try:
            result = tuple(sorted(
                ArtifactRef(ArtifactKey(str(item[0]), str(item[1])), item[2])
                for item in refs
                if isinstance(item, list) and len(item) == 3
            ))
        except (TypeError, ValueError):
            raise ServiceError(400, 'revision is invalid') from None
        if len(result) != len(refs) or len({ref.key for ref in result}) != len(result):
            raise ServiceError(400, 'revision is invalid')
        return result

    @staticmethod
    def _build_page_token(*, thread_id: str, list_name: str, revision: str,
                          filters: tuple[tuple[str, object], ...], page_size: int, next_offset: int,
                          execution_revision: str = '') -> str:
        ProjectionService._resolve_revision(revision)
        size = ProjectionService._validate_page_size(page_size)
        if not isinstance(next_offset, int) or isinstance(next_offset, bool) or next_offset < 0:
            raise ServiceError(400, 'next page offset must be a non-negative integer')
        normalized = ProjectionService._normalize_filters(dict(filters))
        if normalized != filters or not isinstance(thread_id, str) or not thread_id or not isinstance(list_name, str) or not list_name:
            raise ServiceError(400, 'page token context is invalid')
        if execution_revision and not isinstance(execution_revision, str):
            raise ServiceError(400, 'execution revision is invalid')
        payload: dict[str, object] = {
            'thread_id': thread_id,
            'list_name': list_name,
            'revision': revision,
            'filters': [list(item) for item in normalized],
            'page_size': size,
            'next_offset': next_offset,
        }
        if execution_revision:
            payload['execution_revision'] = execution_revision
        return _encode_context('p1', payload)

    @staticmethod
    def _resolve_page_token(token: str, *, thread_id: str, list_name: str,
                            filters: tuple[tuple[str, object], ...], page_size: int) -> dict[str, Any]:
        payload = _decode_context(token, 'p1', 'page_token')
        normalized = ProjectionService._normalize_filters(dict(filters))
        size = ProjectionService._validate_page_size(page_size)
        token_filters = payload.get('filters')
        if not isinstance(token_filters, list):
            raise ServiceError(400, 'page_token is invalid')
        try:
            decoded_filters = tuple((item[0], item[1]) for item in token_filters if isinstance(item, list) and len(item) == 2)
        except (TypeError, IndexError):
            raise ServiceError(400, 'page_token is invalid') from None
        offset = payload.get('next_offset')
        if (
            len(decoded_filters) != len(token_filters)
            or payload.get('thread_id') != thread_id
            or payload.get('list_name') != list_name
            or decoded_filters != normalized
            or payload.get('page_size') != size
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ServiceError(400, 'page_token does not match this query')
        revision = payload.get('revision')
        if not isinstance(revision, str):
            raise ServiceError(400, 'page_token is invalid')
        execution_revision = payload.get('execution_revision', '')
        if not isinstance(execution_revision, str):
            raise ServiceError(400, 'page_token is invalid')
        return {
            'revision': revision,
            'execution_revision': execution_revision,
            'next_offset': offset,
        }

    async def gates(self, thread_id: str) -> dict[str, Any]:
        history = await self.flow.run_history(thread_id)
        snapshot = history.snapshot
        gates = []
        for stage in self.definition.stages:
            records = tuple(record for record in history.runtime.artifacts if record.ref.key == stage.result_key)
            effective = snapshot.runtime.completed_artifacts.get(stage.result_key)
            versions = [record.ref.version for record in records]
            gates.append({
                'step': stage.name,
                'artifact_id': stage.result_key.artifact_id,
                'versions': versions,
                'effective_version': None if effective is None else effective.version,
                'latest_version': max(versions, default=None),
            })
        return {'thread_id': thread_id, 'gates': gates}

    async def stage_snapshot(self, thread_id: str, stage: str) -> dict[str, Any]:
        if stage not in A.STEPS:
            raise ServiceError(422, f'stage must be one of: {", ".join(A.STEPS)}')
        return {'thread_id': thread_id, 'stage': stage, 'snapshot': public_value(
            await self.flow.stage_snapshot(thread_id, stage),
        )}

    async def case_snapshot(self, thread_id: str, case_id: str) -> dict[str, Any]:
        return {'thread_id': thread_id, 'case_id': case_id, 'snapshot': public_value(
            await self.flow.case_snapshot(thread_id, case_id),
        )}

    async def artifact(self, thread_id: str, artifact_id: str, partition_key: str = '',
                       version: int | None = None) -> dict[str, Any]:
        key = ArtifactKey(artifact_id, partition_key)
        record = (
            await self.flow.head(thread_id, key)
            if version is None
            else await self.flow.record(thread_id, ArtifactRef(key, version))
        )
        if record is None:
            raise ServiceError(404, 'artifact version not found')
        return {
            'thread_id': thread_id,
            'record': public_value(record),
            'value': public_value(await self.flow.read(thread_id, record.ref)),
        }

    async def dataset_result(self, thread_id: str, *, page_size: int | None = None,
                             page_token: str = '') -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        size = self._validate_page_size(page_size)
        filters = self._normalize_filters({})
        version: int | None = None
        if page_token:
            context = self._resolve_page_token(
                page_token,
                thread_id=thread_id,
                list_name='dataset.result',
                filters=filters,
                page_size=size,
            )
            refs = self._resolve_revision(context['revision'])
            if len(refs) != 1 or refs[0].key != ArtifactKey.scalar(A.EVAL_DATASET):
                raise ServiceError(400, 'page_token is invalid')
            revision = context['revision']
            version = refs[0].version
            offset = context['next_offset']
        else:
            revision = ''
            offset = 0

        try:
            artifact = await self.artifact(thread_id, A.EVAL_DATASET, version=version)
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            raise
        if not page_token:
            revision = self._build_revision((_public_artifact_ref(artifact['record']),))

        value = _overview_mapping(artifact['value'], 'dataset result')
        raw_cases = value.get('cases', ())
        if not isinstance(raw_cases, list):
            raise ServiceError(503, 'dataset result cases are invalid')
        rows = [_dataset_result_case(item) for item in raw_cases if isinstance(item, Mapping)]
        page = _page(rows, size, str(offset))
        next_offset = page['next_page_token']
        return {
            'thread_id': thread_id,
            'revision': revision,
            'completed_with_problems': bool(value.get('completed_with_problems', False)),
            'total_size': len(rows),
            'failed_case_count': _overview_count(value.get('failed_case_num', 0), 'dataset result.failed_case_num'),
            'items': page['items'],
            'next_page_token': '' if not next_offset else self._build_page_token(
                thread_id=thread_id,
                list_name='dataset.result',
                revision=revision,
                filters=filters,
                page_size=size,
                next_offset=int(next_offset),
            ),
        }

    async def dataset_result_download(self, thread_id: str, revision: str) -> tuple[str, bytes]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        refs = self._resolve_revision(revision)
        if len(refs) != 1 or refs[0].key != ArtifactKey.scalar(A.EVAL_DATASET):
            raise ServiceError(400, 'revision is invalid')
        artifact = await self.artifact(thread_id, A.EVAL_DATASET, version=refs[0].version)
        value = _overview_mapping(artifact['value'], 'dataset result')
        raw_cases = value.get('cases', ())
        if not isinstance(raw_cases, list):
            raise ServiceError(503, 'dataset result cases are invalid')
        rows = [_dataset_result_case(item) for item in raw_cases if isinstance(item, Mapping)]
        return f'dataset-{thread_id}.csv', _dataset_result_csv(rows)

    async def materials_overview(self, thread_id: str) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        status = _overview_stage_status(
            await self.stage_snapshot(thread_id, 'dataset.material_preparation'),
        )
        try:
            artifact = await self.artifact(thread_id, A.DATASET_BUILD_CHUNKS_MANIFEST)
        except ServiceError as error:
            if error.status_code != 404:
                raise
            imported = await _optional_overview_artifact(
                self, thread_id, A.DATASET_IMPORT_CASES_MANIFEST,
            )
            case_plan = None
            revision = None
            if imported is not None:
                case_plan = _import_case_plan(imported['value'])
                revision = self._build_revision((_public_artifact_ref(imported['record']),))
            return {
                'thread_id': thread_id,
                'revision': revision,
                'status': status,
                'case_plan': case_plan,
                'chunks': None,
                'warnings': [],
            }

        value = artifact['value']
        source = _overview_mapping(value, 'materials overview').get('source')
        summary = _overview_mapping(value, 'materials overview').get('summary')
        case_counts = _overview_mapping(source, 'materials overview.source').get('case_counts')
        chunk_counts = _overview_mapping(summary, 'materials overview.summary').get('chunk_counts')
        plan = _overview_mapping(case_counts, 'materials overview.source.case_counts')
        chunks = _overview_mapping(chunk_counts, 'materials overview.summary.chunk_counts')
        scanned = _overview_count(chunks.get('scanned'), 'materials overview.scanned')
        effective = _overview_count(chunks.get('effective'), 'materials overview.effective')
        selected = _overview_count(chunks.get('selected'), 'materials overview.selected')
        warnings = _overview_warnings(_overview_mapping(value, 'materials overview').get('warnings'))
        return {
            'thread_id': thread_id,
            'revision': self._build_revision((_public_artifact_ref(artifact['record']),)),
            'status': status,
            'case_plan': {
                'target': _overview_count(plan.get('target'), 'materials overview.target'),
                'imported': _overview_count(plan.get('imported'), 'materials overview.imported'),
                'automatic': _overview_count(plan.get('automatic'), 'materials overview.automatic'),
            },
            'chunks': {
                'scanned': scanned,
                'effective': effective,
                'selected': selected,
                'effective_rate': None if not scanned else effective / scanned,
                'selection_rate': None if not effective else selected / effective,
            },
            'warnings': warnings,
        }

    async def topics_overview(self, thread_id: str) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        stage_snapshot = await self.stage_snapshot(thread_id, 'dataset.topic_discovery')
        status = _overview_stage_status(stage_snapshot)
        chunk_requests, label_requests = await asyncio.gather(
            _optional_overview_artifact(self, thread_id, A.DATASET_CHUNK_REQUESTS),
            _optional_overview_artifact(self, thread_id, A.DATASET_EMBEDDING_LABEL_REQUESTS),
        )
        entity_total = _partition_count(None if chunk_requests is None else chunk_requests['value'])
        semantic_total = _partition_count(None if label_requests is None else label_requests['value'])
        try:
            artifact = await self.artifact(thread_id, A.DATASET_TOPIC_MANIFEST)
        except ServiceError as error:
            if error.status_code != 404:
                raise
            return {
                'thread_id': thread_id,
                'revision': None,
                'status': status,
                'total_topics': None,
                'question_types': None,
                'stages': _topic_execution_stages(status, entity_total, semantic_total, None),
            }

        stats = _overview_mapping(_overview_mapping(artifact['value'], 'topics overview').get('stats'),
                                  'topics overview.stats')
        total = _overview_count(stats.get('total_topic_count'), 'topics overview.total_topic_count')
        types = _overview_mapping(stats.get('question_types'), 'topics overview.question_types')
        precision = _overview_count(
            _overview_mapping(types.get('precision'), 'topics overview.question_types.precision').get('count'),
            'topics overview.question_types.precision.count',
        )
        reasoning = _overview_count(
            _overview_mapping(types.get('reasoning'), 'topics overview.question_types.reasoning').get('count'),
            'topics overview.question_types.reasoning.count',
        )
        return {
            'thread_id': thread_id,
            'revision': self._build_revision((_public_artifact_ref(artifact['record']),)),
            'status': status,
            'total_topics': total,
            'question_types': {
                'precision': {'count': precision, 'rate': None if not total else precision / total},
                'reasoning': {'count': reasoning, 'rate': None if not total else reasoning / total},
            },
            'stages': _topic_execution_stages(status, entity_total, semantic_total, total),
        }

    async def cases_overview(self, thread_id: str) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        status = _overview_stage_status(
            await self.stage_snapshot(thread_id, 'dataset.case_generation'),
        )
        try:
            params = await self.artifact(thread_id, A.DATASET_QAPLAN_PLAN_PARAMS)
        except ServiceError as error:
            if error.status_code != 404:
                raise
            return _empty_cases_overview(
                thread_id,
                status,
                revision=None,
                execution_revision=self._build_execution_revision({}),
            )

        revision = self._build_revision((_public_artifact_ref(params['record']),))
        manifest = await _optional_overview_artifact(self, thread_id, A.DATASET_QAPLAN_MANIFEST)
        topic_manifest = await _optional_overview_artifact(self, thread_id, A.DATASET_TOPIC_MANIFEST)
        import_manifest = await _optional_overview_artifact(self, thread_id, A.DATASET_IMPORT_CASES_MANIFEST)
        requests = await _optional_overview_artifact(self, thread_id, A.EVAL_CASE_REQUESTS)
        automatic_plan = _project_cases_automatic_plan(
            manifest=None if manifest is None else manifest['value'],
            params=params['value'],
            topic_manifest=None if topic_manifest is None else topic_manifest['value'],
            import_manifest=None if import_manifest is None else import_manifest['value'],
        )
        if requests is None:
            initial_statuses = (
                {} if import_manifest is None
                else _initial_case_statuses(import_manifest['value'])
            )
            return {
                'thread_id': thread_id,
                'revision': revision,
                'execution_revision': self._build_execution_revision({}),
                'status': status,
                'stages': (
                    self._summarize_case_statuses(initial_statuses)
                    if initial_statuses else _pending_case_stage_overview(_planned_case_count(
                        None if import_manifest is None else import_manifest['value'],
                    ))
                ),
                'automatic_plan': automatic_plan,
            }

        case_ids = _case_ids_from_partition_set(requests['value'])
        statuses = await self._case_operation_statuses(thread_id, case_ids)
        stages = self._summarize_case_statuses(statuses)
        return {
            'thread_id': thread_id,
            'revision': revision,
            'execution_revision': self._build_execution_revision(statuses),
            'status': status,
            'stages': stages,
            'automatic_plan': automatic_plan,
        }

    async def _case_stage_counts(self, thread_id: str, case_ids: tuple[str, ...]) -> dict[str, Any]:
        return self._summarize_case_statuses(
            await self._case_operation_statuses(thread_id, case_ids),
        )

    @staticmethod
    def _summarize_case_statuses(statuses_by_case: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
        statuses_by_operation: dict[str, list[str]] = {
            'dataset.qaplan_spec': [],
            'dataset.generate_case': [],
            'dataset.enhance_case': [],
        }
        for statuses in statuses_by_case.values():
            for operation_id, values in statuses_by_operation.items():
                values.append(statuses[operation_id])
        return {
            'plan': _case_operation_summary(statuses_by_operation['dataset.qaplan_spec']),
            'generate': _case_operation_summary(statuses_by_operation['dataset.generate_case']),
            'grading': _case_operation_summary(statuses_by_operation['dataset.enhance_case']),
        }

    async def _case_operation_statuses(self, thread_id: str,
                                       case_ids: tuple[str, ...]) -> dict[str, dict[str, str]]:
        operation_ids = ('dataset.qaplan_spec', 'dataset.generate_case', 'dataset.enhance_case')
        batch = getattr(self.flow, 'case_operation_statuses', None)
        if callable(batch):
            raw_statuses = await batch(thread_id, case_ids, operation_ids)
            return {
                case_id: self._normalized_case_operation_statuses(case_id, raw_statuses.get(case_id))
                for case_id in case_ids
            }
        snapshots = await asyncio.gather(*(self.case_snapshot(thread_id, case_id) for case_id in case_ids))
        return {
            case_id: self._normalized_case_operation_statuses(
                case_id,
                {
                    operation.get('operation_id'): operation.get('status')
                    for operation in _overview_mapping(
                        _overview_mapping(snapshot, 'case snapshot').get('snapshot'),
                        'case snapshot.snapshot',
                    ).get('runtime', {}).get('operations', [])
                    if isinstance(operation, Mapping)
                },
            )
            for case_id, snapshot in zip(case_ids, snapshots, strict=True)
        }

    @staticmethod
    def _normalized_case_operation_statuses(case_id: str, raw_statuses: object) -> dict[str, str]:
        by_id = _overview_mapping(raw_statuses, f'case {case_id} operation statuses')
        result: dict[str, str] = {}
        for operation_id in ('dataset.qaplan_spec', 'dataset.generate_case', 'dataset.enhance_case'):
            operation_status = by_id.get(operation_id)
            if operation_status not in {'pending', 'running', 'succeeded', 'failed', 'cancelled'}:
                raise ServiceError(409, f'case snapshot has no valid status for {operation_id}')
            result[operation_id] = {
                'succeeded': 'completed',
                'cancelled': 'canceled',
            }.get(operation_status, operation_status)
        return result

    async def _case_spec_values(self, thread_id: str, case_ids: tuple[str, ...]) -> dict[str, object]:
        async def read_optional(case_id: str) -> tuple[str, object | None]:
            try:
                artifact = await self.artifact(thread_id, A.DATASET_QAPLAN_SPEC, case_id)
            except ServiceError as error:
                if error.status_code != 404:
                    raise
                return case_id, None
            return case_id, artifact['value']

        values = await asyncio.gather(*(read_optional(case_id) for case_id in case_ids))
        return {case_id: value for case_id, value in values if value is not None}

    async def cases(self, thread_id: str, *, page_size: int | None = None, page_token: str = '',
                    plan_status: str = '', generate_status: str = '', grading_status: str = '', source: str = '',
                    question_type: str = '', difficulty: str = '') -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        size = self._validate_page_size(page_size)
        filters = _case_filters(
            plan_status, generate_status, grading_status, source, question_type, difficulty,
        )
        normalized_filters = self._normalize_filters(filters)
        complete_keys = (
            ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST),
            ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN),
            ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST),
            ArtifactKey.scalar(A.EVAL_CASE_REQUESTS),
        )
        planned_keys = (
            ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST),
            ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS),
        )
        refs: tuple[ArtifactRef, ...] = ()
        revision = ''
        execution_revision = ''
        offset = 0
        if page_token:
            context = self._resolve_page_token(
                page_token, thread_id=thread_id, list_name='dataset.cases',
                filters=normalized_filters, page_size=size,
            )
            refs = self._resolve_revision(context['revision'])
            if {ref.key for ref in refs} not in (set(complete_keys), set(planned_keys)):
                raise ServiceError(400, 'page_token is invalid')
            revision = context['revision']
            execution_revision = context['execution_revision']
            offset = context['next_offset']

        refs_by_key = {ref.key: ref for ref in refs}
        keys = tuple(ref.key for ref in refs) if refs else complete_keys
        try:
            artifacts = await self._read_artifact_batch(thread_id, keys, refs_by_key)
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            if not page_token and error.status_code == 404:
                try:
                    keys = planned_keys
                    artifacts = await self._read_artifact_batch(thread_id, keys, {})
                except ServiceError as planned_error:
                    if planned_error.status_code != 404:
                        raise
                    return {
                        'thread_id': thread_id, 'revision': None,
                        'execution_revision': self._build_execution_revision({}),
                        'items': [], 'next_page_token': '',
                    }
            else:
                raise
        if not page_token:
            revision = self._build_revision(tuple(_public_artifact_ref(artifacts[key]['record']) for key in keys))

        if set(keys) == set(planned_keys):
            rows = _planned_case_rows(
                artifacts[planned_keys[0]]['value'], artifacts[planned_keys[1]]['value'],
            )
            statuses = _pending_case_statuses(tuple(row['case_id'] for row in rows))
        else:
            case_ids = _case_ids_from_partition_set(artifacts[ArtifactKey.scalar(A.EVAL_CASE_REQUESTS)]['value'])
            statuses = await self._case_operation_statuses(thread_id, case_ids)
            rows = _case_rows(
                case_ids,
                artifacts[ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST)]['value'],
                artifacts[ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN)]['value'],
                artifacts[ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)]['value'],
                statuses,
                {},
            )
        rows = _ordered_case_rows(
            rows, artifacts[ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST)]['value'],
        )
        current_execution_revision = self._build_execution_revision(statuses)
        if page_token and execution_revision != current_execution_revision:
            raise ServiceError(409, 'page_token execution snapshot is unavailable')
        execution_revision = current_execution_revision
        rows = [row for row in rows if _case_matches(row, filters)]
        page = _page(rows, size, str(offset))
        if set(keys) == set(complete_keys) and page['items']:
            page_case_ids = tuple(item['case_id'] for item in page['items'])
            specifications = await self._case_spec_values(thread_id, page_case_ids)
            page['items'] = _case_rows(
                page_case_ids,
                artifacts[ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST)]['value'],
                artifacts[ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN)]['value'],
                artifacts[ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)]['value'],
                statuses,
                specifications,
            )
        next_offset = page['next_page_token']
        return {
            'thread_id': thread_id,
            'revision': revision,
            'execution_revision': execution_revision,
            'items': page['items'],
            'next_page_token': '' if not next_offset else self._build_page_token(
                thread_id=thread_id, list_name='dataset.cases', revision=revision,
                filters=normalized_filters, page_size=size, next_offset=int(next_offset),
                execution_revision=execution_revision,
            ),
        }

    async def case_detail(self, thread_id: str, case_id: str) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        case_id = _required_id(case_id, 'case_id')
        base_keys = (
            ArtifactKey.scalar(A.EVAL_CASE_REQUESTS),
            ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST),
            ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN),
            ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST),
            ArtifactKey.scalar(A.DATASET_SELECTED_DOCS),
        )
        try:
            artifacts = await self._read_artifact_batch(thread_id, base_keys, {})
        except ServiceError as error:
            if error.status_code == 404:
                raise ServiceError(404, f'case not found: {case_id}') from None
            raise
        case_ids = _case_ids_from_partition_set(artifacts[base_keys[0]]['value'])
        if case_id not in case_ids:
            raise ServiceError(404, f'case not found: {case_id}')
        statuses = await self._case_operation_statuses(thread_id, (case_id,))
        optional = await self._case_detail_artifacts(thread_id, case_id)
        row = _case_rows(
            (case_id,),
            artifacts[base_keys[1]]['value'],
            artifacts[base_keys[2]]['value'],
            artifacts[base_keys[3]]['value'],
            statuses,
            {} if optional['spec'] is None else {case_id: optional['spec']['value']},
        )[0]
        spec = optional['spec']
        draft = optional['draft']
        enhancement = optional['enhancement']
        references = _detail_case_references(spec, draft, row['source'])
        documents = _selected_document_names(artifacts[base_keys[4]]['value'])
        refs = tuple(
            _public_artifact_ref(artifacts[key]['record'])
            for key in base_keys
        ) + tuple(
            _public_artifact_ref(optional[name]['record'])
            for name in ('spec', 'draft', 'enhancement')
            if optional[name] is not None
        )
        return {
            'thread_id': thread_id,
            'revision': self._build_revision(refs),
            'case_id': case_id,
            'source': row['source'],
            'question_type': row['question_type'],
            'difficulty': row['difficulty'],
            'topic': _case_detail_topic(row['topic'], artifacts[base_keys[3]]['value']),
            'references': _detail_reference_rows(references, documents),
            'stages': {
                'plan': {'status': row['stages']['plan']},
                'generate': _detail_generate_stage(row['stages']['generate'], draft),
                'grading': _detail_grading_stage(row['stages']['grading'], enhancement),
            },
        }

    async def _case_detail_artifacts(self, thread_id: str, case_id: str) -> dict[str, dict[str, Any] | None]:
        artifact_ids = {
            'spec': A.DATASET_QAPLAN_SPEC,
            'draft': A.DATASET_CASE_DRAFT,
            'enhancement': A.DATASET_CASE_ENHANCEMENT,
        }

        async def read_optional(artifact_id: str) -> dict[str, Any] | None:
            try:
                return await self.artifact(thread_id, artifact_id, case_id)
            except ServiceError as error:
                if error.status_code != 404:
                    raise
                return None

        values = await asyncio.gather(*(read_optional(artifact_id) for artifact_id in artifact_ids.values()))
        return dict(zip(artifact_ids, values, strict=True))

    async def topics(self, thread_id: str, *, question_type: str = '',
                     min_chunk_count: int | None = None, max_chunk_count: int | None = None,
                     page_size: int | None = None, page_token: str = '') -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        size = self._validate_page_size(page_size)
        filters = _topic_filters(question_type, min_chunk_count, max_chunk_count)
        normalized_filters = self._normalize_filters(filters)
        version: int | None = None
        if page_token:
            context = self._resolve_page_token(
                page_token,
                thread_id=thread_id,
                list_name='dataset.topics',
                filters=normalized_filters,
                page_size=size,
            )
            refs = self._resolve_revision(context['revision'])
            if len(refs) != 1 or refs[0].key != ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST):
                raise ServiceError(400, 'page_token is invalid')
            revision = context['revision']
            version = refs[0].version
            offset = context['next_offset']
        else:
            revision = ''
            offset = 0

        try:
            artifact = await self.artifact(thread_id, A.DATASET_TOPIC_MANIFEST, version=version)
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            if not page_token and error.status_code == 404:
                return {
                    'thread_id': thread_id,
                    'revision': None,
                    'items': [],
                    'next_page_token': '',
                }
            raise
        ref = _public_artifact_ref(artifact['record'])
        if not page_token:
            revision = self._build_revision((ref,))

        manifest = artifact['value']
        source_topics = manifest.get('topics', ()) if isinstance(manifest, Mapping) else ()
        rows = [
            {
                'topic_id': topic.get('topic_id'),
                'name': topic.get('name'),
                'question_type': topic.get('question_type'),
                'chunk_count': topic.get('chunk_count'),
            }
            for topic in source_topics
            if isinstance(topic, Mapping) and _topic_matches(topic, filters)
        ]
        rows.sort(key=lambda item: str(item['topic_id']))
        page = _page(rows, size, str(offset))
        next_offset = page['next_page_token']
        return {
            'thread_id': thread_id,
            'revision': revision,
            'items': page['items'],
            'next_page_token': (
                '' if not next_offset else self._build_page_token(
                    thread_id=thread_id,
                    list_name='dataset.topics',
                    revision=revision,
                    filters=normalized_filters,
                    page_size=size,
                    next_offset=int(next_offset),
                )
            ),
        }

    async def materials_documents(self, thread_id: str, *, included: bool | None = None,
                                  knowledge_base_id: str = '', page_size: int | None = None,
                                  page_token: str = '') -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        size = self._validate_page_size(page_size)
        filters = _document_filters(included, knowledge_base_id)
        normalized_filters = self._normalize_filters(filters)
        refs: tuple[ArtifactRef, ...] = ()
        revision = ''
        offset = 0
        if page_token:
            context = self._resolve_page_token(
                page_token,
                thread_id=thread_id,
                list_name='dataset.materials_documents',
                filters=normalized_filters,
                page_size=size,
            )
            refs = self._resolve_revision(context['revision'])
            allowed_keys = {
                ArtifactKey.scalar(A.DATASET_SELECTED_DOCS),
                ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES),
            }
            if (not {ref.key for ref in refs}.issubset(allowed_keys)
                    or ArtifactKey.scalar(A.DATASET_SELECTED_DOCS) not in {ref.key for ref in refs}):
                raise ServiceError(400, 'page_token is invalid')
            revision, offset = context['revision'], context['next_offset']

        refs_by_key = {ref.key: ref for ref in refs}
        selected_version = refs_by_key.get(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS))
        candidate_version = refs_by_key.get(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES))
        try:
            selected = await self.artifact(
                thread_id, A.DATASET_SELECTED_DOCS,
                version=None if selected_version is None else selected_version.version,
            )
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            if not page_token and error.status_code == 404:
                return {'thread_id': thread_id, 'revision': None, 'items': [], 'next_page_token': ''}
            raise

        candidates = None
        if not page_token or candidate_version is not None:
            try:
                candidates = await self.artifact(
                    thread_id, A.DATASET_BUILD_CHUNK_CANDIDATES,
                    version=None if candidate_version is None else candidate_version.version,
                )
            except ServiceError as error:
                if page_token and candidate_version is not None and error.status_code == 404:
                    raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
                if not page_token and error.status_code == 404:
                    candidates = None
                else:
                    raise

        if not page_token:
            refs = (_public_artifact_ref(selected['record']),)
            if candidates is not None:
                refs += (_public_artifact_ref(candidates['record']),)
            revision = self._build_revision(refs)

        counts = _document_chunk_counts(None if candidates is None else candidates['value'])
        source = selected['value'] if isinstance(selected['value'], Mapping) else {}
        documents = source.get('documents', ()) if isinstance(source, Mapping) else ()
        rows = [
            _document_dto(document, counts, has_candidates=candidates is not None)
            for document in documents
            if isinstance(document, Mapping) and _document_matches(document, filters)
        ]
        rows.sort(key=lambda item: (item['_discovery_index'], item['knowledge_base']['id'], item['document_id']))
        for row in rows:
            row.pop('_discovery_index')
        page = _page(rows, size, str(offset))
        next_offset = page['next_page_token']
        return {
            'thread_id': thread_id,
            'revision': revision,
            'items': page['items'],
            'next_page_token': (
                '' if not next_offset else self._build_page_token(
                    thread_id=thread_id,
                    list_name='dataset.materials_documents',
                    revision=revision,
                    filters=normalized_filters,
                    page_size=size,
                    next_offset=int(next_offset),
                )
            ),
        }

    async def material_document_detail(
        self,
        thread_id: str,
        knowledge_base_id: str,
        document_id: str,
        *,
        selected: bool | None = None,
        split_rule: str = '',
        page_size: int | None = None,
        page_token: str = '',
    ) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        size = self._validate_page_size(page_size)
        filters = _document_detail_filters(knowledge_base_id, document_id, selected, split_rule)
        normalized_filters = self._normalize_filters(filters)
        selected_key = ArtifactKey.scalar(A.DATASET_SELECTED_DOCS)
        candidates_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)
        refs: tuple[ArtifactRef, ...] = ()
        revision = ''
        offset = 0
        if page_token:
            context = self._resolve_page_token(
                page_token, thread_id=thread_id, list_name='dataset.material_document_detail',
                filters=normalized_filters, page_size=size,
            )
            refs = self._resolve_revision(context['revision'])
            if (not {ref.key for ref in refs}.issubset({selected_key, candidates_key})
                    or selected_key not in {ref.key for ref in refs}):
                raise ServiceError(400, 'page_token is invalid')
            revision, offset = context['revision'], context['next_offset']
        refs_by_key = {ref.key: ref for ref in refs}
        try:
            selected_artifact = await self.artifact(
                thread_id, selected_key.artifact_id,
                version=None if selected_key not in refs_by_key else refs_by_key[selected_key].version,
            )
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            raise
        candidates_artifact = None
        if not page_token or candidates_key in refs_by_key:
            try:
                candidates_artifact = await self.artifact(
                    thread_id, candidates_key.artifact_id,
                    version=None if candidates_key not in refs_by_key else refs_by_key[candidates_key].version,
                )
            except ServiceError as error:
                if page_token and error.status_code == 404:
                    raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
                if not page_token and error.status_code == 404:
                    candidates_artifact = None
                else:
                    raise
        if not page_token:
            refs = (_public_artifact_ref(selected_artifact['record']),)
            if candidates_artifact is not None:
                refs += (_public_artifact_ref(candidates_artifact['record']),)
            revision = self._build_revision(refs)

        selected_value = selected_artifact['value']
        source_documents = selected_value.get('documents', ()) if isinstance(selected_value, Mapping) else ()
        document = next(
            (
                item for item in source_documents
                if isinstance(item, Mapping)
                and item.get('kb_id') == knowledge_base_id
                and item.get('doc_id') == document_id
            ),
            None,
        )
        if document is None:
            raise ServiceError(404, 'knowledge base or document not found')
        candidate_value = None if candidates_artifact is None else candidates_artifact['value']
        all_chunks = _document_candidate_chunks(candidate_value, knowledge_base_id, document_id)
        quotas = _document_quotas(candidate_value, all_chunks, knowledge_base_id, document_id)
        rows = [
            _document_chunk_dto(chunk)
            for chunk in all_chunks
            if _document_chunk_matches(chunk, filters)
        ]
        rows.sort(key=lambda item: (item['_discovery_index'], item['chunk_id']))
        for row in rows:
            row.pop('_discovery_index')
        page = _page(rows, size, str(offset))
        next_offset = page['next_page_token']
        return {
            'thread_id': thread_id,
            'revision': revision,
            'document': {
                'id': document_id,
                'name': str(document.get('filename') or document_id),
                'included': document.get('included') is True,
                'knowledge_base': {
                    'id': knowledge_base_id,
                    'name': str(document.get('knowledge_base_name') or knowledge_base_id),
                },
            },
            'chunk_summary': (
                None if candidates_artifact is None else {
                    'effective': len(all_chunks),
                    'selected': sum(chunk.get('selected') is True for chunk in all_chunks),
                }
            ),
            'quotas': quotas,
            'chunks': {
                'items': page['items'],
                'next_page_token': (
                    '' if not next_offset else self._build_page_token(
                        thread_id=thread_id,
                        list_name='dataset.material_document_detail',
                        revision=revision,
                        filters=normalized_filters,
                        page_size=size,
                        next_offset=int(next_offset),
                    )
                ),
            },
        }

    async def topic_detail(
        self, thread_id: str, topic_id: str, *, page_size: int | None = None, page_token: str = '',
    ) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        size = self._validate_page_size(page_size)
        filters = self._normalize_filters({'topic_id': _required_id(topic_id, 'topic_id')})
        manifest_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
        documents_key = ArtifactKey.scalar(A.DATASET_SELECTED_DOCS)
        refs: tuple[ArtifactRef, ...] = ()
        revision = ''
        offset = 0
        if page_token:
            context = self._resolve_page_token(
                page_token, thread_id=thread_id, list_name='dataset.topic_detail', filters=filters, page_size=size,
            )
            refs = self._resolve_revision(context['revision'])
            if ({ref.key for ref in refs if ref.key in {manifest_key, documents_key}} != {manifest_key, documents_key}
                    or any(ref.key.artifact_id not in {A.DATASET_TOPIC_MANIFEST, A.DATASET_SELECTED_DOCS, A.DATASET_CHUNK}
                           for ref in refs)):
                raise ServiceError(400, 'page_token is invalid')
            revision, offset = context['revision'], context['next_offset']
        refs_by_key = {ref.key: ref for ref in refs}
        try:
            basics = await self._read_artifact_batch(thread_id, (manifest_key, documents_key), refs_by_key)
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            raise
        manifest = basics[manifest_key]['value']
        topics = manifest.get('topics', ()) if isinstance(manifest, Mapping) else ()
        topic = next((item for item in topics if isinstance(item, Mapping) and item.get('topic_id') == topic_id), None)
        if topic is None:
            raise ServiceError(404, 'topic not found')
        chunk_ids = _topic_chunk_ids(topic)
        chunk_keys = tuple(ArtifactKey(A.DATASET_CHUNK, chunk_id) for chunk_id in chunk_ids)
        if page_token:
            if {ref.key for ref in refs} != {manifest_key, documents_key, *chunk_keys}:
                raise ServiceError(400, 'page_token is invalid')
        try:
            chunks = await self._read_artifact_batch(thread_id, chunk_keys, refs_by_key)
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            raise
        if not page_token:
            refs = (
                _public_artifact_ref(basics[manifest_key]['record']),
                _public_artifact_ref(basics[documents_key]['record']),
                *(_public_artifact_ref(chunks[key]['record']) for key in chunk_keys),
            )
            revision = self._build_revision(refs)

        document_names = _document_names(basics[documents_key]['value'])
        rows = [_topic_chunk_dto(chunks[key]['value'], document_names) for key in chunk_keys]
        page = _page(rows, size, str(offset))
        next_offset = page['next_page_token']
        return {
            'thread_id': thread_id,
            'revision': revision,
            'topic': {
                'topic_id': topic_id,
                'name': str(topic.get('name') or ''),
                'question_type': str(topic.get('question_type') or ''),
                'chunk_count': topic.get('chunk_count'),
            },
            'chunks': {
                'items': page['items'],
                'next_page_token': (
                    '' if not next_offset else self._build_page_token(
                        thread_id=thread_id,
                        list_name='dataset.topic_detail',
                        revision=revision,
                        filters=filters,
                        page_size=size,
                        next_offset=int(next_offset),
                    )
                ),
            },
        }

    async def material_adjustment_options(self, thread_id: str) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        source_key = ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG)
        selection_key = ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS)
        chunks_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS)
        artifacts = await self._read_artifact_batch(
            thread_id, (source_key, selection_key, chunks_key), {},
        )
        source = _mapping_value(artifacts[source_key]['value'], 'source_config')
        selection = _mapping_value(artifacts[selection_key]['value'], 'select_docs_params')
        params = _mapping_value(artifacts[chunks_key]['value'], 'build_chunks_params')
        source_ids = _source_knowledge_base_ids(source)
        names = source.get('knowledge_base_names')
        names = names if isinstance(names, Mapping) else {}
        enabled = {
            item.get('kb_id'): item.get('included') is True
            for item in selection.get('knowledge_bases', ())
            if isinstance(item, Mapping) and isinstance(item.get('kb_id'), str)
        }
        refs = tuple(_public_artifact_ref(artifacts[key]['record']) for key in (
            source_key, selection_key, chunks_key,
        ))
        try:
            capabilities = (
                self.capability_client.parser_capabilities(source_ids)
                if self.capability_client is not None and source_ids else {}
            )
        except Exception as exc:
            raise ServiceError(503, 'parser capabilities are unavailable') from exc
        # Report the configuration the pipeline actually runs with. The params
        # artifact is seeded empty and stays empty until the user submits an
        # adjustment, so reading its keys directly would report every split rule
        # and layout type as disabled while candidates were in fact built from
        # the defaults.
        try:
            effective = BuildChunksParams.from_dict(params)
        except ValueError as exc:
            raise ServiceError(503, 'build_chunks_params projection is invalid') from exc
        return {
            'thread_id': thread_id,
            'revision': self._build_revision(refs),
            'target_case_count': source.get('target_case_count'),
            'min_target_case_count': _material_target_minimum(source),
            'knowledge_bases': [
                {
                    'id': kb_id,
                    'name': str(names.get(kb_id) or kb_id),
                    'included': enabled.get(kb_id, False),
                }
                for kb_id in source_ids
            ],
            'split_rules': _project_capability_directory(
                capabilities, source_ids, enabled, 'split_rules', effective.groups, priority=True,
            ),
            'layout_types': _project_capability_directory(
                capabilities, source_ids, enabled, 'layout_types', effective.allowed_types, priority=False,
            ),
        }

    async def case_topic_options(
        self, thread_id: str, case_id: str, *, page_size: int | None = None, page_token: str = '',
    ) -> dict[str, Any]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        size = self._validate_page_size(page_size)
        filters = self._normalize_filters({'case_id': _required_id(case_id, 'case_id')})
        plan_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN)
        topic_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
        refs: tuple[ArtifactRef, ...] = ()
        revision = ''
        offset = 0
        if page_token:
            context = self._resolve_page_token(
                page_token, thread_id=thread_id, list_name='dataset.case_topic_options',
                filters=filters, page_size=size,
            )
            refs = self._resolve_revision(context['revision'])
            if {ref.key for ref in refs} != {plan_key, topic_key}:
                raise ServiceError(400, 'page_token is invalid')
            revision, offset = context['revision'], context['next_offset']
        refs_by_key = {ref.key: ref for ref in refs}
        try:
            artifacts = await self._read_artifact_batch(
                thread_id, (plan_key, topic_key), refs_by_key,
            )
        except ServiceError as error:
            if page_token and error.status_code == 404:
                raise ServiceError(409, 'page_token pagination snapshot is unavailable') from None
            raise
        if not page_token:
            revision = self._build_revision(tuple(
                _public_artifact_ref(artifacts[key]['record']) for key in (plan_key, topic_key)
            ))

        plan = _mapping_value(artifacts[plan_key]['value'], 'qaplan_plan')
        items = plan.get('items') if isinstance(plan.get('items'), list) else []
        current = next(
            (item for item in items if isinstance(item, Mapping) and item.get('case_id') == case_id), None,
        )
        if current is None:
            raise ServiceError(404, 'generated case plan not found')
        question_type = current.get('question_type')
        difficulty = current.get('difficulty')
        current_topic_id = current.get('topic_id')
        required_chunks = _TOPIC_OPTION_MIN_CHUNKS.get(difficulty)
        if question_type not in {'precision', 'reasoning'} or required_chunks is None or not isinstance(current_topic_id, str):
            raise ServiceError(503, 'qaplan plan projection is invalid')
        occupied = {
            item.get('topic_id')
            for item in items
            if isinstance(item, Mapping)
            and item.get('case_id') != case_id
            and item.get('question_type') == question_type
            and item.get('difficulty') == difficulty
            and isinstance(item.get('topic_id'), str)
        }
        topics = _mapping_value(artifacts[topic_key]['value'], 'topic_manifest').get('topics')
        topics = topics if isinstance(topics, list) else ()
        rows = [
            {
                'topic_id': topic.get('topic_id'),
                'name': str(topic.get('name') or ''),
                'chunk_count': topic.get('chunk_count'),
            }
            for topic in topics if isinstance(topic, Mapping)
            and topic.get('question_type') == question_type
            and isinstance(topic.get('chunk_count'), int)
            # Same eligibility as qaplan: difficulty needs at least 1/2/3 chunks.
            and topic['chunk_count'] >= required_chunks
            and topic.get('topic_id') not in occupied | {current_topic_id}
        ]
        rows.sort(key=lambda row: str(row['topic_id']))
        page = _page(rows, size, str(offset))
        next_offset = page['next_page_token']
        return {
            'thread_id': thread_id,
            'case_id': case_id,
            'items': page['items'],
            'next_page_token': (
                '' if not next_offset else self._build_page_token(
                    thread_id=thread_id, list_name='dataset.case_topic_options', revision=revision,
                    filters=filters, page_size=size, next_offset=int(next_offset),
                )
            ),
        }

    async def _read_artifact_batch(
        self, thread_id: str, keys: tuple[ArtifactKey, ...], refs_by_key: Mapping[ArtifactKey, ArtifactRef],
    ) -> dict[ArtifactKey, dict[str, Any]]:
        if len(set(keys)) != len(keys):
            raise ServiceError(503, 'duplicate artifact key in projection')
        values = await asyncio.gather(*(
            self.artifact(thread_id, key.artifact_id, key.partition_key,
                          version=None if key not in refs_by_key else refs_by_key[key].version)
            for key in keys
        ))
        return dict(zip(keys, values, strict=True))

    async def artifact_history(self, thread_id: str, artifact_id: str, partition_key: str = '') -> dict[str, Any]:
        records = await self.flow.history(thread_id, ArtifactKey(artifact_id, partition_key))
        return {
            'thread_id': thread_id,
            'items': public_value(records),
            'total_size': len(records),
        }

    async def gate_content(self, thread_id: str, stage: str, version: int) -> dict[str, Any]:
        ref = await self._gate_ref(thread_id, stage, version)
        return {
            'thread_id': thread_id,
            'step': stage,
            'version': version,
            'content': public_value(await self.flow.read(thread_id, ref)),
        }

    async def gate_download(self, thread_id: str, stage: str, version: int) -> tuple[str, bytes]:
        content = (await self.gate_content(thread_id, stage, version))['content']
        return (
            f'{stage}-v{version}.json',
            json.dumps(
                content,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode(),
        )

    async def steps(self, thread_id: str) -> dict[str, Any]:
        _, _, pages = await _execution_projection(
            self.flow,
            self.definition,
            thread_id,
        )
        return {
            'thread_id': thread_id,
            'active_step_id': next(
                (item['step_id'] for item in pages if item['active']),
                '',
            ),
            'items': [_public_step(item) for item in pages],
            'total_size': len(pages),
        }

    async def events(self, thread_id: str, step_id: str = '', after_event_id: str = '') -> dict[str, Any]:
        return await execution_events(
            self.flow,
            self.definition,
            thread_id,
            step_id=step_id,
            after_event_id=after_event_id,
        )

    async def abtest_case_details(self, thread_id: str, version: int, page_size: int, page_token: str,
                                  keyword: str = '', outcome: str = '') -> dict[str, Any]:
        value = (await self.gate_content(thread_id, 'abtest', version))['content']
        data = value if isinstance(value, Mapping) else {}
        summary = data.get('summary') if isinstance(data.get('summary'), Mapping) else {}
        rows = _rows(data.get('case_deltas') or summary.get('case_deltas'))
        rows = rows or _comparison_rows(data)
        rows = _filter(rows, keyword, ('case_id', 'query', 'outcome'))
        if outcome:
            rows = [row for row in rows if row.get('outcome') == outcome]
        return _page(rows, page_size, page_token)

    async def trace_detail(self, thread_id: str, trace_id: str) -> dict[str, Any]:
        await self.flow.snapshot(thread_id)
        from evo.traces import build_trace_detail_view

        value = await asyncio.to_thread(build_trace_detail_view, trace_id)
        return public_value(value)

    async def trace_compare(self, thread_id: str, left: str, right: str) -> dict[str, Any]:
        await self.flow.snapshot(thread_id)
        from evo.traces import build_trace_compare_view

        value = await asyncio.to_thread(build_trace_compare_view, left, right)
        return public_value(value)

    async def candidates(self, thread_id: str, status: str, page_size: int, page_token: str) -> dict[str, Any]:
        if thread_id and not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        run_ids = (thread_id,) if thread_id else await self.flow.run_ids()
        items = []
        for run_id in run_ids:
            for stage in ('repair', 'abtest'):
                key = ArtifactKey.scalar(A.ROOTS[stage])
                for record in await self.flow.history(run_id, key):
                    value = await self.flow.read(run_id, record.ref)
                    items.append(_candidate(run_id, stage, record.ref, value))
        if status:
            items = [item for item in items if item['status'] == status]
        items.sort(key=lambda item: item['candidate_id'])
        if page_token:
            items = [item for item in items if item['candidate_id'] > page_token]
        page = items[:page_size]
        return {
            'items': page,
            'next_page_token': (
                page[-1]['candidate_id'] if len(page) == page_size else ''
            ),
        }

    async def candidate(self, candidate_id: str) -> dict[str, Any]:
        thread_id, artifact, version = _parse_candidate(candidate_id)
        stage = next(
            (
                stage for stage in ('repair', 'abtest')
                if A.ROOTS[stage] == artifact
            ),
            '',
        )
        if not stage:
            raise ServiceError(404, 'candidate not found')
        ref = await self._gate_ref(thread_id, stage, version)
        value = await self.flow.read(thread_id, ref)
        return _candidate(thread_id, stage, ref, value, detail=True)

    async def _gate_ref(self, thread_id: str, stage: str, version: int) -> ArtifactRef:
        if stage not in A.ROOTS:
            raise ServiceError(422, f'step must be one of: {", ".join(A.STEPS)}')
        if version < 1:
            raise ServiceError(422, 'version must be positive')
        ref = ArtifactRef(ArtifactKey.scalar(A.ROOTS[stage]), version)
        if await self.flow.record(thread_id, ref) is None:
            raise ServiceError(404, 'gate artifact version not found')
        return ref


def _stage_status(stage_status: str, flow_status: str) -> str:
    if stage_status == 'awaiting_approval':
        return 'completed'
    return 'idle' if stage_status == 'pending' and flow_status == 'idle' else stage_status


def _display_step_status(status: str, progress: StageProgress | None) -> str:
    """Pass through /steps status without Dataset-specific extensions."""
    del progress
    return status


def _comparison_rows(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    origin = value.get('origin') if isinstance(value.get('origin'), Mapping) else {}
    candidate = value.get('candidate') if isinstance(value.get('candidate'), Mapping) else {}
    before = {str(row.get('case_id') or ''): row for row in _rows(origin.get('cases'))}
    after = {str(row.get('case_id') or ''): row for row in _rows(candidate.get('cases'))}
    result = []
    for case_id in dict.fromkeys((*before, *after)):
        left, right = before.get(case_id, {}), after.get(case_id, {})
        old = float(left.get('overall') or left.get('overall_score') or 0)
        new = float(right.get('overall') or right.get('overall_score') or 0)
        result.append({
            'case_id': case_id,
            'outcome': (
                'improved' if new > old
                else 'regressed' if new < old
                else 'unchanged'
            ),
            'before': dict(left),
            'after': dict(right),
            'delta': {'overall_score': round(new - old, 4)},
        })
    return result


def _candidate(thread_id: str, stage: str, ref: ArtifactRef, value: object, *, detail: bool = False) -> dict[str, Any]:
    data = value if isinstance(value, Mapping) else {}
    row = {
        'candidate_id': f'{thread_id}:{ref.key.artifact_id}@v{ref.version}',
        'thread_id': thread_id,
        'source_step': stage,
        'source_ref': f'{ref.key.artifact_id}@v{ref.version}',
        'status': str(data.get('status') or ''),
        'summary': public_value({
            key: data[key]
            for key in ('status', 'verdict', 'algo_id', 'candidate_algo_id')
            if key in data
        }),
    }
    if detail:
        diff = data.get('diff') if isinstance(data.get('diff'), Mapping) else {}
        row['files'] = [str(path) for path in diff if '/' not in str(path)]
    return row


def _parse_candidate(value: str) -> tuple[str, str, int]:
    try:
        thread_id, ref = value.split(':', 1)
        artifact, version = ref.rsplit('@v', 1)
        return thread_id, artifact, int(version)
    except ValueError as exc:
        raise ServiceError(404, 'candidate not found') from exc


def _rows(value: object) -> list[dict[str, Any]]:
    return [dict(row) for row in value] if isinstance(value, list) else []


def _filter(rows: list[dict[str, Any]], keyword: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    keyword = keyword.strip().lower()
    return rows if not keyword else [
        row for row in rows
        if any(keyword in str(row.get(field) or '').lower() for field in fields)
    ]


def _topic_filters(question_type: str, min_chunk_count: int | None,
                   max_chunk_count: int | None) -> dict[str, object]:
    if question_type not in ('', 'precision', 'reasoning'):
        raise ServiceError(400, 'question_type must be precision or reasoning')
    for name, value in (('min_chunk_count', min_chunk_count), ('max_chunk_count', max_chunk_count)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ServiceError(400, f'{name} must be a non-negative integer')
    if min_chunk_count is not None and max_chunk_count is not None and min_chunk_count > max_chunk_count:
        raise ServiceError(400, 'min_chunk_count must not exceed max_chunk_count')
    return {
        **({'question_type': question_type} if question_type else {}),
        **({'min_chunk_count': min_chunk_count} if min_chunk_count is not None else {}),
        **({'max_chunk_count': max_chunk_count} if max_chunk_count is not None else {}),
    }


_TOPIC_OPTION_MIN_CHUNKS = {'easy': 1, 'medium': 2, 'hard': 3}


def _project_capability_directory(capabilities: Mapping[str, Any], source_ids: list[str], enabled: Mapping[str, bool],
                                  field: str, selected: object, *, priority: bool) -> list[dict[str, object]]:
    configured = list(selected) if isinstance(selected, (list, tuple)) else []
    active_sources = [kb_id for kb_id in source_ids if enabled.get(kb_id) is True]
    rows: dict[str, str] = {}
    for kb_id in source_ids:
        capability = capabilities.get(kb_id, {})
        values = capability.get(field, ()) if isinstance(capability, Mapping) else ()
        if not isinstance(values, (list, tuple)):
            raise ServiceError(503, 'parser capabilities are invalid')
        for item in values:
            if not isinstance(item, Mapping) or not isinstance(item.get('id'), str) or not item.get('id'):
                raise ServiceError(503, 'parser capabilities are invalid')
            identifier = item['id']
            rows.setdefault(identifier, str(item.get('name') or identifier))
    result = []
    for identifier in sorted(rows):
        supported = bool(active_sources) and all(
            any(isinstance(item, Mapping) and item.get('id') == identifier
                for item in (capabilities.get(kb_id, {}) or {}).get(field, ()))
            for kb_id in active_sources
        )
        enabled_now = identifier in configured
        row: dict[str, object] = {
            'id': identifier, 'name': rows[identifier], 'supported': supported, 'enabled': enabled_now,
        }
        if priority:
            row['priority'] = configured.index(identifier) + 1 if enabled_now else None
        result.append(row)
    return result


def _mapping_value(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ServiceError(503, f'{name} projection is invalid')
    return value


def _source_knowledge_base_ids(source: Mapping[str, object]) -> list[str]:
    raw_ids = source.get('kb_ids', source.get('kb_id', ()))
    ids = [item for item in raw_ids if isinstance(item, str)] if isinstance(raw_ids, list) else []
    raw_csv = source.get('csv_sources', source.get('csv_data', ()))
    if isinstance(raw_csv, list):
        for item in raw_csv:
            if isinstance(item, Mapping):
                csv_id = item.get('kb_id') if 'kb_id' in item else next(iter(item), None) if len(item) == 1 else None
                if isinstance(csv_id, str) and csv_id not in ids:
                    ids.append(csv_id)
    return ids


def _required_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(400, f'{name} must be a non-empty string')
    return value


def _document_detail_filters(knowledge_base_id: str, document_id: str, selected: bool | None,
                             split_rule: str) -> dict[str, object]:
    if selected is not None and not isinstance(selected, bool):
        raise ServiceError(400, 'selected must be a boolean')
    if not isinstance(split_rule, str) or (split_rule and not split_rule.strip()):
        raise ServiceError(400, 'split_rule must not be blank')
    return {
        'knowledge_base_id': _required_id(knowledge_base_id, 'knowledge_base_id'),
        'document_id': _required_id(document_id, 'document_id'),
        **({'selected': selected} if selected is not None else {}),
        **({'split_rule': split_rule} if split_rule else {}),
    }


def _document_candidate_chunks(value: object, knowledge_base_id: str, document_id: str) -> list[Mapping[str, object]]:
    source = value if isinstance(value, Mapping) else {}
    chunks = source.get('chunks', ()) if isinstance(source, Mapping) else ()
    return [
        item for item in chunks
        if isinstance(item, Mapping)
        and item.get('kb_id') == knowledge_base_id
        and item.get('doc_id') == document_id
    ]


def _document_quotas(value: object, chunks: list[Mapping[str, object]], knowledge_base_id: str,
                     document_id: str) -> list[dict[str, object]]:
    source = value if isinstance(value, Mapping) else {}
    quotas = source.get('quotas', ()) if isinstance(source, Mapping) else ()
    selected_by_group: dict[str, int] = {}
    for chunk in chunks:
        group = str(chunk.get('group') or '')
        selected_by_group[group] = selected_by_group.get(group, 0) + int(chunk.get('selected') is True)
    rows = [
        {
            'split_rule': str(quota.get('group') or ''),
            'required': quota.get('required'),
            'selected': selected_by_group.get(str(quota.get('group') or ''), 0),
        }
        for quota in quotas
        if isinstance(quota, Mapping)
        and quota.get('kb_id') == knowledge_base_id
        and quota.get('doc_id') == document_id
    ]
    return sorted(rows, key=lambda item: str(item['split_rule']))


def _document_chunk_matches(chunk: Mapping[str, object], filters: Mapping[str, object]) -> bool:
    return (
        ('selected' not in filters or chunk.get('selected') is filters['selected'])
        and ('split_rule' not in filters or chunk.get('group') == filters['split_rule'])
    )


def _document_chunk_dto(chunk: Mapping[str, object]) -> dict[str, object]:
    discovery_index = chunk.get('discovery_index')
    return {
        'chunk_id': str(chunk.get('chunk_id') or ''),
        'split_rule': str(chunk.get('group') or ''),
        'layout_type': str(chunk.get('type') or ''),
        'text': str(chunk.get('text') or ''),
        'selected': chunk.get('selected') is True,
        '_discovery_index': discovery_index if isinstance(discovery_index, int) else 0,
    }


def _topic_chunk_ids(topic: Mapping[str, object]) -> tuple[str, ...]:
    raw = topic.get('chunk_ids')
    if not isinstance(raw, list) or not raw:
        raise ServiceError(503, 'topic chunk_ids are invalid')
    values = tuple(_required_id(value, 'topic.chunk_id') for value in raw)
    if len(set(values)) != len(values):
        raise ServiceError(503, 'topic chunk_ids are duplicated')
    return values


def _document_names(value: object) -> dict[tuple[str, str], tuple[str, str]]:
    source = value if isinstance(value, Mapping) else {}
    documents = source.get('documents', ()) if isinstance(source, Mapping) else ()
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        kb_id, doc_id = document.get('kb_id'), document.get('doc_id')
        if isinstance(kb_id, str) and kb_id and isinstance(doc_id, str) and doc_id:
            result[kb_id, doc_id] = (
                str(document.get('knowledge_base_name') or kb_id),
                str(document.get('filename') or doc_id),
            )
    return result


def _topic_chunk_dto(chunk: object, document_names: Mapping[tuple[str, str], tuple[str, str]]) -> dict[str, object]:
    if not isinstance(chunk, Mapping):
        raise ServiceError(503, 'topic chunk artifact is invalid')
    kb_id, doc_id = chunk.get('kb_id'), chunk.get('doc_id')
    if not isinstance(kb_id, str) or not kb_id or not isinstance(doc_id, str) or not doc_id:
        raise ServiceError(503, 'topic chunk source is invalid')
    names = document_names.get((kb_id, doc_id))
    if names is None:
        raise ServiceError(503, 'topic chunk source document is unavailable')
    return {
        'chunk_id': str(chunk.get('chunk_id') or ''),
        'knowledge_base': {'id': kb_id, 'name': names[0]},
        'document': {'id': doc_id, 'name': names[1]},
        'split_rule': str(chunk.get('group') or ''),
        'layout_type': str(chunk.get('type') or ''),
        'text': str(chunk.get('text') or ''),
    }


def _topic_matches(topic: Mapping[str, object], filters: Mapping[str, object]) -> bool:
    chunk_count = topic.get('chunk_count')
    if not isinstance(chunk_count, int) or isinstance(chunk_count, bool):
        return False
    return (
        (not filters.get('question_type') or topic.get('question_type') == filters['question_type'])
        and ('min_chunk_count' not in filters or chunk_count >= filters['min_chunk_count'])
        and ('max_chunk_count' not in filters or chunk_count <= filters['max_chunk_count'])
    )


def _document_filters(included: bool | None, knowledge_base_id: str) -> dict[str, object]:
    if included is not None and not isinstance(included, bool):
        raise ServiceError(400, 'included must be a boolean')
    if not isinstance(knowledge_base_id, str) or (knowledge_base_id and not knowledge_base_id.strip()):
        raise ServiceError(400, 'knowledge_base_id must not be blank')
    return {
        **({'included': included} if included is not None else {}),
        **({'knowledge_base_id': knowledge_base_id} if knowledge_base_id else {}),
    }


def _document_matches(document: Mapping[str, object], filters: Mapping[str, object]) -> bool:
    return (
        ('included' not in filters or document.get('included') is filters['included'])
        and ('knowledge_base_id' not in filters or document.get('kb_id') == filters['knowledge_base_id'])
    )


def _document_chunk_counts(value: object) -> dict[tuple[str, str], dict[str, int]]:
    source = value if isinstance(value, Mapping) else {}
    chunks = source.get('chunks', ()) if isinstance(source, Mapping) else ()
    result: dict[tuple[str, str], dict[str, int]] = {}
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        kb_id, doc_id = chunk.get('kb_id'), chunk.get('doc_id')
        if not isinstance(kb_id, str) or not isinstance(doc_id, str):
            continue
        counts = result.setdefault((kb_id, doc_id), {'effective': 0, 'selected': 0})
        counts['effective'] += 1
        counts['selected'] += int(chunk.get('selected') is True)
    return result


def _document_dto(document: Mapping[str, object], counts: Mapping[tuple[str, str], Mapping[str, int]], *,
                  has_candidates: bool) -> dict[str, object]:
    kb_id = str(document.get('kb_id') or '')
    doc_id = str(document.get('doc_id') or '')
    included = document.get('included') is True
    count = counts.get((kb_id, doc_id))
    chunks: dict[str, object] | None = None
    if included and has_candidates:
        effective = int((count or {}).get('effective', 0))
        selected = int((count or {}).get('selected', 0))
        chunks = {
            'effective': effective,
            'selected': selected,
            'selection_rate': selected / effective if effective else None,
        }
    discovery_index = document.get('discovery_index')
    return {
        'document_id': doc_id,
        'name': str(document.get('filename') or doc_id),
        'included': included,
        'knowledge_base': {'id': kb_id, 'name': str(document.get('knowledge_base_name') or kb_id)},
        'chunks': chunks,
        '_discovery_index': discovery_index if isinstance(discovery_index, int) else 0,
    }


def _public_artifact_ref(record: object) -> ArtifactRef:
    if not isinstance(record, Mapping):
        raise ServiceError(503, 'artifact record projection is invalid')
    ref = record.get('ref')
    if not isinstance(ref, Mapping) or not isinstance(ref.get('key'), Mapping):
        raise ServiceError(503, 'artifact record projection is invalid')
    key = ref['key']
    try:
        return ArtifactRef(
            ArtifactKey(key['artifact_id'], key.get('partition_key', '')),
            ref['version'],
        )
    except (KeyError, TypeError, ValueError):
        raise ServiceError(503, 'artifact record projection is invalid') from None


_DATASET_RESULT_FIELDS = (
    'case_id', 'question', 'question_type', 'difficulty', 'ground_truth', 'grading_guidance',
    'key_points', 'forbidden_claims', 'reference_context', 'reference_doc', 'reference_doc_ids',
    'reference_chunk_ids', 'generate_reason', 'is_deleted',
)


def _dataset_result_case(value: Mapping[str, Any]) -> dict[str, Any]:
    preparation = value.get('source_preparation') if isinstance(value.get('source_preparation'), Mapping) else {}
    enhancement = preparation.get('dataset_enhancement') \
        if isinstance(preparation.get('dataset_enhancement'), Mapping) else {}
    return {
        'case_id': str(value.get('case_id') or value.get('id') or ''),
        'question': str(value.get('question') or ''),
        'question_type': str(value.get('question_type') or ''),
        'difficulty': str(value.get('difficulty') or ''),
        'ground_truth': value.get('ground_truth', value.get('answer', '')),
        'grading_guidance': str(value.get('grading_guidance') or ''),
        'key_points': list(value.get('key_points') or enhancement.get('key_points') or []),
        'forbidden_claims': list(value.get('forbidden_claims') or enhancement.get('forbidden_claims') or []),
        'reference_context': value.get('reference_context') or [],
        'reference_doc': list(value.get('reference_doc') or []),
        'reference_doc_ids': list(value.get('reference_doc_ids') or []),
        'reference_chunk_ids': list(value.get('reference_chunk_ids') or []),
        'generate_reason': str(value.get('generate_reason') or ''),
        'is_deleted': bool(value.get('is_deleted', False)),
    }


def _dataset_result_csv(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=_DATASET_RESULT_FIELDS)
    writer.writeheader()
    for row in rows:
        serialized = dict(row)
        for field in ('key_points', 'forbidden_claims'):
            serialized[field] = json.dumps(serialized[field], ensure_ascii=False, separators=(',', ':'))
        context = serialized['reference_context']
        serialized['reference_context'] = (
            json.dumps(context, ensure_ascii=False, separators=(',', ':'))
            if isinstance(context, (list, dict)) else str(context or '')
        )
        for field in ('reference_doc', 'reference_doc_ids', 'reference_chunk_ids'):
            serialized[field] = ','.join(str(item) for item in serialized[field])
        serialized['is_deleted'] = 'true' if serialized['is_deleted'] else 'false'
        writer.writerow(serialized)
    return ('\ufeff' + output.getvalue()).encode('utf-8')


def _page(rows: list[dict[str, Any]], size: int, token: str) -> dict[str, Any]:
    if not str(token or '0').isdigit():
        raise ServiceError(422, 'page_token must be a non-negative integer offset')
    offset = int(token or 0)
    page = rows[offset:offset + size]
    return {
        'items': public_value(page),
        'next_page_token': str(offset + size) if offset + size < len(rows) else '',
        'total_size': len(rows),
    }


def _encode_context(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return f'{prefix}.{base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")}'


def _decode_context(value: str, prefix: str, field: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith(f'{prefix}.'):
        raise ServiceError(400, f'{field} is invalid')
    encoded = value[len(prefix) + 1:]
    try:
        decoded = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ServiceError(400, f'{field} is invalid') from None
    if not isinstance(payload, dict):
        raise ServiceError(400, f'{field} is invalid')
    return payload


_TERMINAL_ATTEMPTS = frozenset({
    'cancelled', 'succeeded', 'failed', 'interrupted', 'discarded',
})
_TERMINAL_STEPS = frozenset({
    'completed', 'paused', 'cancelled', 'canceled', 'failed',
})
_STEP_ID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    'lazyrag:evo:step-events:v1',
)


async def execution_events(flow: Any, definition: FlowDefinition, run_id: str, *, step_id: str = '',
                           after_event_id: str = '') -> dict[str, Any]:
    snapshot, items, pages = await _execution_projection(
        flow,
        definition,
        run_id,
    )
    page = None
    if step_id:
        page = _resolve_step(snapshot, pages, step_id)
        items = [item for item in items if item['step_id'] == step_id]
    if after_event_id:
        items = events_after(items, after_event_id)
    terminal = _terminal(snapshot) if page is None else _step_terminal(page)
    return {
        'thread_id': run_id,
        'step_id': step_id or None,
        'items': items,
        'terminal': terminal,
        'reason': _stream_end_reason(snapshot, page) if terminal else '',
        **public_thread_state(snapshot),
    }


async def _execution_projection(
    flow: Any, definition: FlowDefinition, run_id: str,
) -> tuple[FlowSnapshot, list[dict[str, Any]], list[dict[str, Any]],]:
    history = await flow.run_history(run_id)
    snapshot = history.snapshot
    attempts = history.runtime.attempts
    operation_events = history.runtime.operation_events
    results = {
        stage.name: tuple(record for record in history.runtime.artifacts if record.ref.key == stage.result_key)
        for stage in definition.stages
    }
    approvals = {
        stage.name: tuple(record for record in history.runtime.artifacts if record.ref.key == stage.approval_key)
        for stage in definition.stages
    }

    items = flow_events(
        snapshot,
        attempts,
        operation_events,
        results,
        approvals,
        await _historical_partition_sets(flow, run_id, attempts),
        definition,
    )
    pages = _step_pages(snapshot, items)
    _link_execution_pages(snapshot, items, pages)
    return snapshot, items, pages


def flow_events(snapshot: FlowSnapshot, attempts: tuple[AttemptSnapshot, ...],
                operation_events: tuple[RecordedOperationEvent, ...], results: Mapping[str, tuple[ArtifactRecord, ...]],
                approvals: Mapping[str, tuple[ArtifactRecord, ...]], partition_sets: Mapping[ArtifactRef, PartitionSet],
                definition: FlowDefinition) -> list[dict[str, Any]]:
    rows: list[tuple[float, int, str, dict[str, Any]]] = []
    events_by_attempt: dict[str, list[RecordedOperationEvent]] = {}
    for event in operation_events:
        events_by_attempt.setdefault(event.attempt_id, []).append(event)
    completions: dict[ArtifactKey, list[AttemptSnapshot]] = {}

    for attempt in sorted(attempts, key=lambda item: (item.created_at, item.attempt_id)):
        stage = _operation_stage(definition, attempt.operation_id)
        if not stage:
            continue
        partition = attempt_case(snapshot, attempt, partition_sets)
        base = {
            'thread_id': snapshot.run_id,
            'step_id': f'{snapshot.run_id}:{stage}',
            'stage': stage,
            'next_step_id': '',
            'next_step_run_id': '',
            'operation_id': attempt.operation_id,
            'attempt_id': attempt.attempt_id,
            **({'partition': partition} if partition is not None else {}),
        }
        rows.append((
            attempt.created_at,
            0,
            attempt.attempt_id,
            {
                **base,
                'event_id': f'{snapshot.run_id}:{attempt.attempt_id}:start',
                'event_type': attempt.operation_id,
                'status': 'running',
                'timestamp': attempt.created_at,
            },
        ))
        for event in sorted(
            events_by_attempt.get(attempt.attempt_id, ()),
            key=lambda item: item.sequence,
        ):
            rows.append((
                event.created_at,
                1,
                f'{attempt.attempt_id}:{event.sequence}',
                {
                    **base,
                    'event_id': f'{snapshot.run_id}:operation-event:{event.sequence}',
                    'event_type': event.event.event_type,
                    'level': event.event.level,
                    'timestamp': event.created_at,
                    'message': event.event.message,
                    'data': public_value(event.event.data),
                    **({'status': event.event.status} if event.event.status is not None else {}),
                    **(
                        {'progress': {'current': event.event.current, 'total': event.event.total}}
                        if event.event.current is not None or event.event.total is not None
                        else {}
                    ),
                },
            ))
        if attempt.status not in _TERMINAL_ATTEMPTS:
            continue

        finished = attempt.finished_at or attempt.started_at or attempt.created_at
        rows.append((
            finished,
            2,
            attempt.attempt_id,
            {
                **base,
                'event_id': f'{snapshot.run_id}:{attempt.attempt_id}:terminal',
                'event_type': attempt.operation_id,
                'status': _attempt_status(attempt.status),
                'timestamp': finished,
                **(
                    {'message': attempt.error.message}
                    if attempt.error is not None
                    else {}
                ),
            },
        ))
        if attempt.status == 'succeeded':
            for key in attempt.output_keys:
                completions.setdefault(key, []).append(attempt)

    result_attempts: dict[ArtifactRef, AttemptSnapshot | None] = {}
    result_counts: dict[ArtifactKey, int] = {}
    for stage, records in results.items():
        for index, record in enumerate(records):
            attempt = None
            if record.producer.startswith('operation:'):
                result_index = result_counts.get(record.ref.key, 0)
                attempt = _matching_attempt(completions, record, result_index)
                result_counts[record.ref.key] = result_index + 1
            result_attempts[record.ref] = attempt
            timestamp = _attempt_time(attempt, index)
            rows.append((
                timestamp,
                3,
                f'{stage}:{record.ref.version}',
                {
                    'thread_id': snapshot.run_id,
                    'step_id': f'{snapshot.run_id}:{stage}',
                    'stage': stage,
                    'next_step_id': '',
                    'next_step_run_id': '',
                    'event_id': f'{snapshot.run_id}:{stage}:v{record.ref.version}',
                    'event_type': 'step.finish',
                    'status': 'completed',
                    'timestamp': timestamp,
                    'artifact': _artifact(record),
                },
            ))

    for stage, records in approvals.items():
        stage_results = {record.ref: record for record in results.get(stage, ())}
        for index, record in enumerate(records):
            result = next(
                (stage_results[ref] for ref in record.input_refs if ref in stage_results),
                None,
            )
            attempt = None if result is None else result_attempts.get(result.ref)
            timestamp = _attempt_time(attempt, index)
            rows.append((
                timestamp,
                4,
                f'{stage}:approval:{record.ref.version}',
                {
                    'thread_id': snapshot.run_id,
                    'step_id': f'{snapshot.run_id}:{stage}',
                    'stage': stage,
                    'next_step_id': '',
                    'next_step_run_id': '',
                    'event_id': (
                        f'{snapshot.run_id}:{stage}:'
                        f'approval:v{record.ref.version}'
                    ),
                    'event_type': 'checkpoint.continue',
                    'status': 'completed',
                    'timestamp': timestamp,
                    'artifact': _artifact(record),
                },
            ))

    rows.sort(key=lambda row: (row[0], row[1], row[2]))
    items = [row[3] for row in rows]
    _assign_step_ids(snapshot.run_id, items)
    return items


def _assign_step_ids(run_id: str, items: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    last_by_stage: dict[str, str] = {}
    current_stage = ''
    current_step_id = ''
    closed = True

    for item in items:
        stage = item['stage']
        if item['event_type'] == 'checkpoint.continue' and stage in last_by_stage:
            item['step_id'] = last_by_stage[stage]
            continue
        if stage != current_stage or closed:
            counts[stage] = counts.get(stage, 0) + 1
            current_stage = stage
            current_step_id = _step_id(run_id, stage, counts[stage])
            last_by_stage[stage] = current_step_id
            closed = False
        item['step_id'] = current_step_id
        if item['event_type'] == 'step.finish':
            closed = True


def _step_pages(snapshot: FlowSnapshot, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        step_id = item['step_id']
        page = by_id.get(step_id)
        if page is None:
            page = {
                'thread_id': snapshot.run_id,
                'step_id': step_id,
                'stage': item['stage'],
                'title': item['stage'],
                'order_index': len(pages),
                'event_count': 0,
                'next_step_id': '',
                'next_step_run_id': '',
                'version': None,
                'status': 'running',
                'continues_previous': bool(
                    pages and pages[-1]['stage'] == item['stage']
                ),
                'active': False,
                '_closed': False,
                '_started_at': item['timestamp'],
                '_ended_at': item['timestamp'],
                '_next_started_at': None,
                '_next_stage': '',
            }
            by_id[step_id] = page
            pages.append(page)

        page['event_count'] += 1
        page['_started_at'] = min(page['_started_at'], item['timestamp'])
        page['_ended_at'] = max(page['_ended_at'], item['timestamp'])
        if item['event_type'] == 'step.finish':
            page['status'] = 'completed'
            page['version'] = item['artifact']['version']
            page['_closed'] = True
        elif item.get('status') in {'failed', 'canceled'}:
            page['status'] = item['status']
        elif item.get('status') == 'running':
            page['status'] = 'running'

    _append_current_page(snapshot, pages)
    current = next(
        (page for page in reversed(pages) if page['stage'] == snapshot.current_stage),
        None,
    )
    if current is not None and not current['_closed']:
        progress = next(
            stage for stage in snapshot.stages
            if stage.stage == snapshot.current_stage
        )
        current['status'] = _stage_status(progress.status, snapshot.status)

    for index, page in enumerate(pages):
        stage_progress = next(
            (progress for progress in snapshot.stages if progress.stage == page['stage']),
            None,
        )
        page['status'] = _display_step_status(page['status'], stage_progress)
        page['order_index'] = index
        page['continues_previous'] = bool(
            index and pages[index - 1]['stage'] == page['stage']
        )
        page['active'] = (
            index == len(pages) - 1
            and page['status'] in {'running', 'pausing', 'cancelling'}
        )
    return pages


def _append_current_page(snapshot: FlowSnapshot, pages: list[dict[str, Any]]) -> None:
    if snapshot.status == 'completed':
        return
    progress = next(
        stage for stage in snapshot.stages
        if stage.stage == snapshot.current_stage
    )
    latest = next(
        (page for page in reversed(pages) if page['stage'] == progress.stage),
        None,
    )
    settled = progress.status in {'awaiting_approval', 'completed'}
    if settled and latest is not None:
        return
    if latest is not None and latest is pages[-1] and not latest['_closed']:
        return

    count = sum(page['stage'] == progress.stage for page in pages) + 1
    pages.append({
        'thread_id': snapshot.run_id,
        'step_id': _step_id(snapshot.run_id, progress.stage, count),
        'stage': progress.stage,
        'title': progress.stage,
        'order_index': len(pages),
        'event_count': 0,
        'next_step_id': '',
        'next_step_run_id': '',
        'version': (
            None if progress.result_ref is None else progress.result_ref.version
        ),
        'status': _stage_status(progress.status, snapshot.status),
        'continues_previous': bool(
            pages and pages[-1]['stage'] == progress.stage
        ),
        'active': False,
        '_closed': False,
        '_started_at': None,
        '_ended_at': None,
        '_next_started_at': None,
        '_next_stage': '',
    })


def _link_execution_pages(snapshot: FlowSnapshot, items: list[dict[str, Any]], pages: list[dict[str, Any]]) -> None:
    for index, page in enumerate(pages):
        next_page = pages[index + 1] if index + 1 < len(pages) else None
        if next_page is not None:
            page['next_step_id'] = next_page['step_id']
            page['next_step_run_id'] = next_page['step_id']
            page['_next_started_at'] = next_page['_started_at']
            page['_next_stage'] = next_page['stage']
            continue

        next_stage = _next_stage(snapshot, page['stage']) if page['_closed'] else ''
        page['_next_stage'] = next_stage
        if next_stage:
            count = sum(item['stage'] == next_stage for item in pages) + 1
            page['next_step_run_id'] = _step_id(
                snapshot.run_id,
                next_stage,
                count,
            )

    by_id = {page['step_id']: page for page in pages}
    for item in items:
        page = by_id[item['step_id']]
        item['next_step_id'] = page['next_step_id']
        item['next_step_run_id'] = page['next_step_run_id']


def _next_stage(snapshot: FlowSnapshot, stage: str) -> str:
    index = next(
        index for index, progress in enumerate(snapshot.stages)
        if progress.stage == stage
    )
    return (
        snapshot.stages[index + 1].stage
        if index + 1 < len(snapshot.stages)
        else ''
    )


def _step_id(run_id: str, stage: str, ordinal: int) -> str:
    return str(uuid.uuid5(
        _STEP_ID_NAMESPACE,
        f'{run_id}:{stage}:{ordinal}',
    ))


def _resolve_step(snapshot: FlowSnapshot, pages: list[dict[str, Any]], step_id: str) -> dict[str, Any]:
    page = next((item for item in pages if item['step_id'] == step_id), None)
    if page is not None:
        return page
    if pages and pages[-1]['next_step_run_id'] == step_id:
        source = pages[-1]
        return {
            'thread_id': snapshot.run_id,
            'step_id': step_id,
            'stage': source['_next_stage'],
            'title': source['_next_stage'],
            'order_index': len(pages),
            'event_count': 0,
            'next_step_id': '',
            'next_step_run_id': '',
            'version': None,
            'status': 'pending',
            'continues_previous': source['stage'] == source['_next_stage'],
            'active': False,
            '_closed': False,
            '_started_at': None,
            '_ended_at': None,
            '_next_started_at': None,
            '_next_stage': '',
        }
    raise ServiceError(422, 'unknown step_id for thread')


def _public_step(page: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in page.items()
        if not key.startswith('_') and key != 'thread_id'
    }


def _step_terminal(page: Mapping[str, Any]) -> bool:
    return bool(page['next_step_id']) or page['status'] in _TERMINAL_STEPS


def _stream_end_reason(snapshot: FlowSnapshot, page: Mapping[str, Any] | None) -> str:
    if page is not None:
        page_status = str(page['status'])
        if page_status == 'completed' or page['next_step_id']:
            pending = snapshot.pending_approval
            if pending is not None and pending.stage == page['stage']:
                return 'checkpoint_wait'
            if (
                snapshot.status == 'completed'
                and snapshot.current_stage == page['stage']
            ):
                return 'flow_completed'
            return 'step_completed'
        if page_status in {'cancelled', 'canceled'}:
            return 'cancelled'
        if page_status == 'failed':
            return 'failed'
        if page_status == 'paused':
            return 'user_paused'

    if snapshot.pending_approval is not None:
        return 'checkpoint_wait'
    return {
        'paused': 'user_paused',
        'cancelled': 'cancelled',
        'failed': 'failed',
        'completed': 'flow_completed',
    }.get(snapshot.status, 'step_completed')


def events_after(items: list[dict[str, Any]], event_id: str) -> list[dict[str, Any]]:
    for index, item in enumerate(items):
        if item['event_id'] == event_id:
            return items[index + 1:]
    raise ServiceError(422, 'unknown event_id for event scope')


def attempt_case(snapshot: FlowSnapshot, attempt: AttemptSnapshot,
                 historical: Mapping[ArtifactRef, PartitionSet] | None = None) -> dict[str, Any] | None:
    partition_key = attempt.partition_key
    if not partition_key:
        return None
    output = next((key for key in attempt.output_keys if key.partition_key), None)
    if output is None:
        return {'id': partition_key}
    partition_set_id = A.PARTITION_SET_BY_ARTIFACT.get(output.artifact_id)
    if partition_set_id is None:
        return {'id': partition_key}

    partitions = next(
        (
            (historical or {}).get(ref)
            for ref in attempt.input_refs
            if ref.key == ArtifactKey.scalar(partition_set_id)
        ),
        None,
    )
    if partitions is None:
        partitions = snapshot.runtime.partition_sets.get(
            ArtifactKey.scalar(partition_set_id)
        )
    if partitions is None or partition_key not in partitions:
        return {'id': partition_key}
    return {
        'id': partition_key,
        'index': partitions.keys.index(partition_key) + 1,
        'total': len(partitions.keys),
    }


async def _historical_partition_sets(flow: Any, run_id: str, attempts: tuple[AttemptSnapshot, ...]
                                     ) -> Mapping[ArtifactRef, PartitionSet]:
    partition_ids = frozenset(A.PARTITION_SET_BY_ARTIFACT.values())
    refs = tuple(dict.fromkeys(
        ref
        for attempt in attempts
        for ref in attempt.input_refs
        if ref.key.artifact_id in partition_ids
    ))
    if not refs:
        return {}
    values = await flow.read_many(run_id, refs)
    return {
        ref: value
        for ref, value in values.items()
        if isinstance(value, PartitionSet)
    }


def _operation_stage(definition: FlowDefinition, operation_id: str) -> str:
    index = definition.stage_index_for_operation(operation_id)
    return '' if index is None else definition.stages[index].name


def _terminal(snapshot: FlowSnapshot) -> bool:
    if snapshot.status in {
        'paused', 'awaiting_approval', 'cancelled', 'failed', 'completed',
    }:
        return True
    return False


def _matching_attempt(completions: Mapping[ArtifactKey, list[AttemptSnapshot]], record: ArtifactRecord, index: int
                      ) -> AttemptSnapshot | None:
    attempts = completions.get(record.ref.key, ())
    return attempts[index] if index < len(attempts) else None


def _attempt_time(attempt: AttemptSnapshot | None, fallback: int) -> float:
    if attempt is None:
        return float(fallback)
    return attempt.finished_at or attempt.started_at or attempt.created_at


def _attempt_status(status: str) -> str:
    return {
        'succeeded': 'completed',
        'cancelled': 'canceled',
        'interrupted': 'canceled',
        'discarded': 'canceled',
    }.get(status, status)


def _overview_stage_status(snapshot: Mapping[str, Any]) -> str:
    value = snapshot.get('snapshot')
    progress = value.get('progress') if isinstance(value, Mapping) else None
    status = progress.get('status') if isinstance(progress, Mapping) else None
    if not isinstance(status, str):
        raise ServiceError(409, 'stage snapshot has no status')
    return status


def _overview_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ServiceError(409, f'{name} is invalid')
    return value


def _material_target_minimum(source: Mapping[str, object]) -> int:
    if source.get('supplement_existing_eval_set') is not True:
        return 1
    imported_cases = source.get('imported_cases', ())
    if not isinstance(imported_cases, list):
        return 1
    return max(1, sum(
        isinstance(case, Mapping) and case.get('is_deleted') is not True
        for case in imported_cases
    ))


def _overview_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ServiceError(409, f'{name} must be a non-negative integer')
    return value


def _overview_warnings(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ServiceError(409, 'materials overview.warnings is invalid')
    return list(value)


async def _optional_overview_artifact(service: ProjectionService, thread_id: str,
                                      artifact_id: str) -> dict[str, Any] | None:
    try:
        return await service.artifact(thread_id, artifact_id)
    except ServiceError as error:
        if error.status_code != 404:
            raise
        return None


def _empty_cases_overview(thread_id: str, status: str, *, revision: str | None,
                          execution_revision: str) -> dict[str, Any]:
    return {
        'thread_id': thread_id,
        'revision': revision,
        'execution_revision': execution_revision,
        'status': status,
        'stages': _pending_case_stage_overview(),
        'automatic_plan': None,
    }


def _pending_case_stage_overview(total: int | None = None) -> dict[str, Any]:
    return {
        name: {
            'status': 'pending', 'completed': 0 if total is not None else None, 'total': total,
            'status_counts': None if total is None else {
                'pending': total, 'running': 0, 'completed': 0, 'failed': 0, 'canceled': 0,
            },
        }
        for name in ('plan', 'generate', 'grading')
    }


def _partition_count(value: object | None) -> int | None:
    if value is None:
        return None
    return len(_case_ids_from_partition_set(value))


def _topic_execution_stages(status: str, entity_total: int | None, semantic_total: int | None,
                            topic_total: int | None) -> dict[str, dict[str, object]]:
    totals = {'entities': entity_total, 'semantic': semantic_total, 'topics': topic_total}
    # Published topic_manifest is the settle signal. Stage may still be paused
    # at the continue gate; rings must not stay at 0/N pending.
    settled = topic_total is not None or status in {'completed', 'succeeded'}
    if settled:
        return {
            name: {'status': 'completed', 'completed': total or 0, 'total': total}
            for name, total in totals.items()
        }
    return {
        name: {'status': 'pending', 'completed': 0, 'total': total}
        for name, total in totals.items()
    }


def _planned_case_count(value: object | None) -> int | None:
    if value is None:
        return None
    allocation = _overview_mapping(
        _overview_mapping(_overview_mapping(value, 'case import manifest').get('stats'), 'case import manifest.stats').get('case_allocation'),
        'case import manifest.case_allocation',
    )
    target = allocation.get('target_case_count')
    return target if isinstance(target, int) and not isinstance(target, bool) and target >= 0 else None


def _import_case_plan(value: object) -> dict[str, int]:
    allocation = _overview_mapping(
        _overview_mapping(_overview_mapping(value, 'case import manifest').get('stats'), 'case import manifest.stats').get('case_allocation'),
        'case import manifest.case_allocation',
    )
    return {
        'target': _overview_count(allocation.get('target_case_count'), 'case import target'),
        'imported': _overview_count(allocation.get('import_case_count'), 'case import imported'),
        'automatic': _overview_count(allocation.get('auto_case_count'), 'case import automatic'),
    }


def _pending_case_statuses(case_ids: tuple[str, ...]) -> dict[str, dict[str, str]]:
    return {case_id: {
        'dataset.qaplan_spec': 'pending', 'dataset.generate_case': 'pending',
        'dataset.enhance_case': 'pending',
    } for case_id in case_ids}


def _initial_case_statuses(import_manifest_value: object) -> dict[str, dict[str, str]]:
    imported = _overview_mapping(import_manifest_value, 'case import manifest')
    allocation = _overview_mapping(
        _overview_mapping(imported.get('stats'), 'case import manifest.stats').get('case_allocation'),
        'case import manifest.case_allocation',
    )
    assignments = _overview_mapping(allocation.get('assignments'), 'case import manifest.assignments')
    statuses = _pending_case_statuses(tuple(assignments))
    for case_id, raw_assignment in assignments.items():
        assignment = _overview_mapping(raw_assignment, f'case assignment {case_id}')
        if assignment.get('mode') == 'imported':
            statuses[case_id] = {
                'dataset.qaplan_spec': 'completed',
                'dataset.generate_case': 'completed',
                'dataset.enhance_case': 'completed',
            }
    return statuses


def _planned_case_rows(import_manifest_value: object, params_value: object) -> list[dict[str, Any]]:
    imported = _overview_mapping(import_manifest_value, 'case import manifest')
    allocation = _overview_mapping(
        _overview_mapping(imported.get('stats'), 'case import manifest.stats').get('case_allocation'),
        'case import manifest.case_allocation',
    )
    assignments = _overview_mapping(allocation.get('assignments'), 'case import manifest.assignments')
    details = imported.get('details')
    if not isinstance(details, list):
        raise ServiceError(503, 'case import manifest.details is invalid')
    imported_cases = {item.get('source_row_number'): item.get('case') for item in details if isinstance(item, Mapping)}
    generated_ids = [case_id for case_id, assignment in assignments.items()
                     if isinstance(assignment, Mapping) and assignment.get('mode') == 'generated']
    lane_counts = _lane_counts(params_value, len(generated_ids))
    generated_meta: dict[str, tuple[str, str]] = {}
    index = 0
    for lane, question_type, difficulty in LANES:
        for _ in range(lane_counts[lane]):
            generated_meta[generated_ids[index]] = (question_type, difficulty)
            index += 1
    statuses = _initial_case_statuses(import_manifest_value)
    rows = []
    for case_id, raw_assignment in assignments.items():
        assignment = _overview_mapping(raw_assignment, f'case assignment {case_id}')
        mode = assignment.get('mode')
        if mode == 'imported':
            case = _overview_mapping(imported_cases.get(assignment.get('source_row_number')), f'imported case {case_id}')
            question_type = _case_choice(case.get('question_type'), {'precision', 'reasoning'}, 'question_type')
            difficulty = _case_optional_choice(case.get('difficulty'), {'easy', 'medium', 'hard'}, 'difficulty')
        elif mode == 'generated' and case_id in generated_meta:
            question_type, difficulty = generated_meta[case_id]
        else:
            raise ServiceError(503, f'case assignment mode is invalid: {case_id}')
        rows.append({'case_id': case_id, 'stages': {'plan': statuses[case_id]['dataset.qaplan_spec'],
                     'generate': statuses[case_id]['dataset.generate_case'], 'grading': statuses[case_id]['dataset.enhance_case']},
                     'source': mode, 'question_type': question_type, 'difficulty': difficulty, 'topic': None})
    return rows


def _case_ids_from_partition_set(value: object) -> tuple[str, ...]:
    data = _overview_mapping(value, 'case partition set')
    raw = data.get('keys')
    if not isinstance(raw, list) or not all(isinstance(case_id, str) and case_id for case_id in raw):
        raise ServiceError(409, 'case partition set.keys is invalid')
    if len(set(raw)) != len(raw):
        raise ServiceError(409, 'case partition set.keys is invalid')
    return tuple(raw)


def _natural_case_id_key(case_id: str) -> tuple[object, ...]:
    """Sort case_0010 before case_0011 (and before case_0009 when reversed in storage)."""
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r'(\d+)', case_id) if part)


def _ordered_case_rows(rows: list[dict[str, Any]], import_manifest_value: object) -> list[dict[str, Any]]:
    """Order imported cases by natural case_id, then generated cases the same way."""
    imported = _overview_mapping(import_manifest_value, 'case import manifest')
    allocation = _overview_mapping(
        _overview_mapping(imported.get('stats'), 'case import manifest.stats').get('case_allocation'),
        'case import manifest.case_allocation',
    )
    assignments = _overview_mapping(allocation.get('assignments'), 'case import manifest.assignments')

    def sort_key(item: tuple[int, Mapping[str, Any]]) -> tuple[int, tuple[object, ...], int]:
        index, row = item
        case_id = str(row.get('case_id') or '')
        raw_assignment = assignments.get(case_id)
        group = (
            0
            if isinstance(raw_assignment, Mapping) and raw_assignment.get('mode') == 'imported'
            else 1
        )
        return (group, _natural_case_id_key(case_id), index)

    return [row for _, row in sorted(enumerate(rows), key=sort_key)]


def _case_operation_summary(statuses: list[str]) -> dict[str, Any]:
    counts = {name: statuses.count(name) for name in ('pending', 'running', 'completed', 'failed', 'canceled')}
    if counts['failed']:
        status = 'failed'
    elif counts['running']:
        status = 'running'
    elif statuses and counts['completed'] == len(statuses):
        status = 'completed'
    elif statuses and counts['canceled'] == len(statuses):
        status = 'canceled'
    else:
        status = 'pending'
    return {
        'status': status,
        'completed': counts['completed'],
        'total': len(statuses),
        'status_counts': counts,
    }


def _case_filters(plan_status: str, generate_status: str, grading_status: str, source: str,
                  question_type: str, difficulty: str) -> dict[str, object]:
    values = {
        'plan_status': (plan_status, {'pending', 'running', 'completed', 'failed', 'canceled'}),
        'generate_status': (generate_status, {'pending', 'running', 'completed', 'failed', 'canceled'}),
        'grading_status': (grading_status, {'pending', 'running', 'completed', 'failed', 'canceled'}),
        'source': (source, {'imported', 'generated'}),
        'question_type': (question_type, {'precision', 'reasoning'}),
        'difficulty': (difficulty, {'easy', 'medium', 'hard'}),
    }
    result: dict[str, object] = {}
    for name, (value, allowed) in values.items():
        if not isinstance(value, str):
            raise ServiceError(400, f'{name} must be a string')
        if not value:
            continue
        if value not in allowed:
            raise ServiceError(400, f'{name} is invalid')
        result[name] = value
    return result


def _case_rows(case_ids: tuple[str, ...], import_manifest_value: object, plan_value: object,
               topic_manifest_value: object, statuses: Mapping[str, Mapping[str, str]],
               specifications: Mapping[str, object] | None = None) -> list[dict[str, Any]]:
    imported = _overview_mapping(import_manifest_value, 'case import manifest')
    allocation = _overview_mapping(
        _overview_mapping(imported.get('stats'), 'case import manifest.stats').get('case_allocation'),
        'case import manifest.case_allocation',
    )
    assignments = _overview_mapping(allocation.get('assignments'), 'case import manifest.assignments')
    details = imported.get('details')
    if not isinstance(details, list):
        raise ServiceError(503, 'case import manifest.details is invalid')
    imported_cases = {
        detail.get('source_row_number'): detail.get('case')
        for detail in details if isinstance(detail, Mapping)
    }
    plan = _overview_mapping(plan_value, 'qaplan plan')
    raw_plan_items = plan.get('items')
    if not isinstance(raw_plan_items, list):
        raise ServiceError(503, 'qaplan plan.items is invalid')
    plan_items = {
        item.get('case_id'): item
        for item in raw_plan_items if isinstance(item, Mapping)
    }
    topics = _overview_mapping(topic_manifest_value, 'topic manifest').get('topics')
    if not isinstance(topics, list):
        raise ServiceError(503, 'topic manifest.topics is invalid')
    topics_by_id = {
        item.get('topic_id'): item
        for item in topics if isinstance(item, Mapping)
    }
    rows = []
    for case_id in case_ids:
        assignment = _overview_mapping(assignments.get(case_id), f'case assignment {case_id}')
        mode = assignment.get('mode')
        case_statuses = statuses.get(case_id)
        if case_statuses is None:
            raise ServiceError(409, f'case snapshot is missing: {case_id}')
        if mode == 'imported':
            source_row = assignment.get('source_row_number')
            case = _overview_mapping(imported_cases.get(source_row), f'imported case {case_id}')
            question_type = _case_choice(case.get('question_type'), {'precision', 'reasoning'}, 'question_type')
            difficulty = _case_optional_choice(case.get('difficulty'), {'easy', 'medium', 'hard'}, 'difficulty')
            topic = None
        elif mode == 'generated':
            plan_item = _overview_mapping(plan_items.get(case_id), f'qaplan plan item {case_id}')
            question_type = _case_choice(plan_item.get('question_type'), {'precision', 'reasoning'}, 'question_type')
            difficulty = _case_choice(plan_item.get('difficulty'), {'easy', 'medium', 'hard'}, 'difficulty')
            specification = None if specifications is None else specifications.get(case_id)
            spec_topic = (
                _overview_mapping(specification, f'qaplan spec {case_id}').get('topic')
                if isinstance(specification, Mapping) else None
            )
            topic_id = (
                _overview_mapping(spec_topic, f'qaplan spec {case_id}.topic').get('topic_id')
                if isinstance(spec_topic, Mapping) else plan_item.get('topic_id')
            )
            topic_value = _overview_mapping(topics_by_id.get(topic_id), f'topic {topic_id}')
            topic = {'topic_id': topic_id, 'name': _required_id(topic_value.get('name'), 'topic.name')}
        else:
            raise ServiceError(503, f'case assignment mode is invalid: {case_id}')
        rows.append({
            'case_id': case_id,
            'stages': {
                'plan': case_statuses['dataset.qaplan_spec'],
                'generate': case_statuses['dataset.generate_case'],
                'grading': case_statuses['dataset.enhance_case'],
            },
            'source': mode,
            'question_type': question_type,
            'difficulty': difficulty,
            'topic': topic,
        })
    return rows


def _case_choice(value: object, allowed: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ServiceError(503, f'case {name} is invalid')
    return value


def _case_optional_choice(value: object, allowed: set[str], name: str) -> str | None:
    if value in (None, ''):
        return None
    return _case_choice(value, allowed, name)


def _case_matches(row: Mapping[str, object], filters: Mapping[str, object]) -> bool:
    stages = row.get('stages')
    if not isinstance(stages, Mapping):
        return False
    return (
        (not filters.get('plan_status') or stages.get('plan') == filters['plan_status'])
        and (not filters.get('generate_status') or stages.get('generate') == filters['generate_status'])
        and (not filters.get('grading_status') or stages.get('grading') == filters['grading_status'])
        and (not filters.get('source') or row.get('source') == filters['source'])
        and (not filters.get('question_type') or row.get('question_type') == filters['question_type'])
        and (not filters.get('difficulty') or row.get('difficulty') == filters['difficulty'])
    )


def _case_detail_topic(value: object, topic_manifest_value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    topic = _overview_mapping(value, 'case topic')
    topic_id = _required_id(topic.get('topic_id'), 'topic.topic_id')
    manifest = _overview_mapping(topic_manifest_value, 'topic manifest')
    topics = manifest.get('topics')
    if not isinstance(topics, list):
        raise ServiceError(503, 'topic manifest.topics is invalid')
    source = next(
        (item for item in topics if isinstance(item, Mapping) and item.get('topic_id') == topic_id),
        None,
    )
    source = _overview_mapping(source, f'topic {topic_id}')
    return {
        'topic_id': topic_id,
        'name': _required_id(source.get('name'), 'topic.name'),
        'chunk_count': _overview_count(source.get('chunk_count'), 'topic.chunk_count'),
    }


def _detail_case_references(spec: dict[str, Any] | None, draft: dict[str, Any] | None,
                            source: object) -> list[Mapping[str, object]]:
    if draft is not None:
        value = _overview_mapping(draft['value'], 'case draft').get('references')
    elif spec is not None:
        spec_value = _overview_mapping(spec['value'], 'qaplan spec')
        if source == 'imported':
            value = _overview_mapping(spec_value.get('imported_case'), 'imported qaplan spec').get('references')
        else:
            value = spec_value.get('references')
    else:
        value = []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ServiceError(503, 'case references are invalid')
    return list(value)


def _selected_document_names(value: object) -> dict[tuple[str, str], tuple[str, str]]:
    documents = _overview_mapping(value, 'selected docs').get('documents')
    if not isinstance(documents, list):
        raise ServiceError(503, 'selected docs.documents is invalid')
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for document in documents:
        item = _overview_mapping(document, 'selected docs.document')
        kb_id = _required_id(item.get('kb_id'), 'document.kb_id')
        doc_id = _required_id(item.get('doc_id'), 'document.doc_id')
        result[(kb_id, doc_id)] = (
            _required_id(item.get('knowledge_base_name'), 'document.knowledge_base_name'),
            _required_id(item.get('filename'), 'document.filename'),
        )
    return result


def _detail_reference_rows(references: list[Mapping[str, object]],
                           documents: Mapping[tuple[str, str], tuple[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for reference in references:
        kb_id = _required_id(reference.get('kb_id'), 'reference.kb_id')
        doc_id = _required_id(reference.get('doc_id'), 'reference.doc_id')
        names = documents.get((kb_id, doc_id))
        if names is None:
            raise ServiceError(503, f'reference document is unavailable: {kb_id}/{doc_id}')
        rows.append({
            'chunk_id': _required_id(reference.get('chunk_id'), 'reference.chunk_id'),
            'knowledge_base': {'id': kb_id, 'name': names[0]},
            'document': {'id': doc_id, 'name': names[1]},
            'text': _required_id(reference.get('text'), 'reference.text'),
        })
    return rows


def _detail_generate_stage(status: object, draft: dict[str, Any] | None) -> dict[str, Any]:
    result = {'status': status, 'question': None, 'answer': None, 'grading_guidance': None}
    if draft is None:
        return result
    value = _overview_mapping(draft['value'], 'case draft')
    result.update({
        field: _required_id(value.get(field), f'case draft.{field}')
        for field in ('question', 'answer', 'grading_guidance')
    })
    return result


def _detail_grading_stage(status: object, enhancement: dict[str, Any] | None) -> dict[str, Any]:
    result = {'status': status, 'key_points': None, 'forbidden_claims': None}
    if enhancement is None:
        return result
    value = _overview_mapping(enhancement['value'], 'case enhancement')
    key_points = value.get('key_points')
    forbidden_claims = value.get('forbidden_claims')
    if not isinstance(key_points, list) or not isinstance(forbidden_claims, list):
        raise ServiceError(503, 'case enhancement is invalid')
    result['key_points'] = [
        {
            'statement': _required_id(_overview_mapping(item, 'case key point').get('statement'), 'key_point.statement'),
            'evidence_chunk_ids': _detail_text_list(
                _overview_mapping(item, 'case key point').get('evidence_chunk_ids'), 'key_point.evidence_chunk_ids',
            ),
        }
        for item in key_points
    ]
    result['forbidden_claims'] = _detail_text_list(forbidden_claims, 'case forbidden_claims')
    return result


def _detail_text_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ServiceError(503, f'{name} is invalid')
    return list(value)


def _artifact(record: ArtifactRecord) -> dict[str, Any]:
    return {
        'artifact_id': record.ref.key.artifact_id,
        'partition_key': record.ref.key.partition_key,
        'version': record.ref.version,
        'ref': f'{record.ref.key.artifact_id}@v{record.ref.version}',
    }


__all__ = [
    'ProjectionService', 'attempt_case', 'events_after', 'flow_events',
]
