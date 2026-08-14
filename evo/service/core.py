from __future__ import annotations

import asyncio
import copy
import logging
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evo import artifacts as A
from evo.artifact_flow import ArtifactFlow, ArtifactUpdate, FlowDefinition, FlowSnapshot
from evo.artifact_runtime import (
    RUN_CONFIGURATION_ARTIFACT_ID,
    ArtifactCommit,
    ArtifactDraft,
    ArtifactKey,
    ArtifactRef,
    DefinitionError,
    OperationResult,
    PartitionSet,
)
from evo.message_intent import MessageIntent, MessageRequest, MessageTurnResult
from evo.operations import evo_flow_definition
from evo.operations.dataset.source_config import normalize_source_config
from evo.operations.dataset.kb_client import KnowledgeBaseClient
from evo.repair_model import EvoModelConfigError, resolve_evo_model

from .contracts import (
    ArtifactUpdateBody,
    AutomaticUpdateBody,
    CaseRerunBody,
    CaseStructureBody,
    CommandRequest,
    ConfigurationUpdateBody,
    ControlRequest,
    DatasetApplyBody,
    ExternalResultBody,
    RetryRequest,
    ServiceError,
    ThreadCreate,
    TopicApplyBody,
)
from .projections import ProjectionService
from .public import public_message_history, public_thread_state, public_value
from .router import RouterService


_STAGES = tuple(A.STEPS)
_FIRST_FRAME_TIMEOUT = 60.0
_THREAD_ID_ATTEMPTS = 32
_AUTO_WAIT_TIMEOUT = 30.0
_AUTO_STOPPED = frozenset({'idle', 'cancelled', 'failed', 'completed'})
_CONFIG_ARTIFACTS = {
    'run_config': A.RUN_CONFIG,
    'source_config': A.CORPUS_SOURCE_CONFIG,
    'qaplan_plan_params': A.DATASET_QAPLAN_PLAN_PARAMS,
    'target_config': A.EVAL_TARGET_CONFIG,
    'eval_policy': A.EVAL_POLICY,
    'repair_policy': A.REPAIR_POLICY,
    'candidate_config': A.ABTEST_CANDIDATE_CONFIG,
}
_CONFIG_ARTIFACT_IDS = frozenset(_CONFIG_ARTIFACTS.values())


logger = logging.getLogger(__name__)


class EvoService:
    def __init__(self, root: str | Path, definition: FlowDefinition, flow: ArtifactFlow) -> None:
        self.root = Path(root)
        self.definition = definition
        self.flow = flow
        self.messages = MessageIntent(self.root, flow)
        self.capability_client = KnowledgeBaseClient()
        self.projections = ProjectionService(flow, definition, capability_client=self.capability_client)
        self.router = RouterService(self.root, flow)
        self._control_locks: dict[str, asyncio.Lock] = {}
        self._message_locks: dict[str, asyncio.Lock] = {}
        self._auto_tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    @classmethod
    async def open(cls, root: str | Path, definition: FlowDefinition | None = None, *, max_concurrency: int = 4,
                   terminate_timeout: float = 1.0) -> EvoService:
        root = Path(root)
        definition = definition or evo_flow_definition()
        flow = await ArtifactFlow.open(
            root / 'artifact-runtime',
            definition,
            max_concurrency=max_concurrency,
            terminate_timeout=terminate_timeout,
        )
        service = cls(root, definition, flow)
        await asyncio.to_thread(service.router.reconcile_unpublished)
        await asyncio.to_thread(service.router.reconcile_published)
        return service

    async def create_thread(self, request: ThreadCreate | Mapping[str, Any]) -> dict[str, Any]:
        request = (
            request
            if isinstance(request, ThreadCreate)
            else ThreadCreate.model_validate(request)
        )
        try:
            resolve_evo_model(request.llm_config.get('evo_llm'))
        except EvoModelConfigError as exc:
            raise ServiceError(422, exc.detail()) from exc
        thread_id = await self._new_thread_id()
        seed = _seed_values(thread_id, request)
        keys = tuple(ArtifactKey.scalar(artifact_id) for artifact_id in A.SEEDS)
        commit = ArtifactCommit(
            f'create:{thread_id}',
            'user:create',
            tuple(
                ArtifactDraft(key, seed[key.artifact_id])
                for key in keys
            ),
            {key: None for key in keys},
        )
        snapshot = await self.flow.create(
            thread_id,
            commit,
            configuration={'mode': 'interactive', 'automatic': request.automatic},
        )
        return _public_thread(request, snapshot)

    async def _new_thread_id(self) -> str:
        for _ in range(_THREAD_ID_ATTEMPTS):
            thread_id = f'thr-{uuid.uuid4().hex[:8]}'
            if not await self.flow.has_run(thread_id):
                return thread_id
        raise ServiceError(503, 'unable to allocate thread_id')

    async def list_threads(self, page_size: int, page_token: str, status: str = '') -> dict[str, Any]:
        offset = _page_offset(page_token)
        items = []
        for thread_id in sorted(await self.flow.run_ids()):
            item = await self.public_thread(thread_id, include_inputs=False)
            if not status or item['status'] == status:
                items.append(item)
        page = items[offset:offset + page_size]
        next_offset = offset + page_size
        return {
            'items': page,
            'next_page_token': str(next_offset) if next_offset < len(items) else '',
        }

    async def public_thread(self, thread_id: str, *, include_inputs: bool = True) -> dict[str, Any]:
        snapshot, metadata, configuration = await asyncio.gather(
            self.flow.snapshot(thread_id),
            self._run_config(thread_id),
            self.flow.configuration(thread_id),
        )
        configuration_ref = snapshot.runtime.completed_artifacts.get(
            ArtifactKey.scalar(RUN_CONFIGURATION_ARTIFACT_ID)
        )
        item = {
            'thread_id': thread_id,
            'mode': str(configuration.values.get('mode') or 'interactive'),
            'automatic': bool(configuration.values.get('automatic')),
            'automatic_version': 0 if configuration_ref is None else configuration_ref.version,
            'title': str(metadata.get('title') or ''),
            **public_thread_state(snapshot),
        }
        if include_inputs:
            item['inputs'] = public_value(metadata.get('inputs') or {})
            item['retryable'] = snapshot.status == 'failed'
        return item

    async def start(self, thread_id: str, request: CommandRequest | Mapping[str, Any]) -> dict[str, str]:
        request = _command(request)
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            snapshot, automatic = await asyncio.gather(
                self.flow.snapshot(thread_id),
                self._auto_enabled(thread_id),
            )
            if snapshot.status == 'paused':
                raise ServiceError(409, 'paused thread requires resume')
            if snapshot.status != 'idle':
                raise ServiceError(409, 'thread has already been started')
            target = request.until_step or _STAGES[0]
            if target != _STAGES[0]:
                raise ServiceError(422, f'start runs through {_STAGES[0]}')
            await self.flow.start(thread_id)
            if automatic:
                self._ensure_auto_task(thread_id)
            return _accepted(thread_id, request.command_id, 'start')

    async def continue_thread(self, thread_id: str, request: CommandRequest | Mapping[str, Any]) -> dict[str, str]:
        request = _command(request)
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            if await self._auto_enabled(thread_id):
                snapshot = await self.flow.snapshot(thread_id)
                if snapshot.status == 'idle':
                    raise ServiceError(409, 'thread has not been started')
                if snapshot.status in {'cancelled', 'failed'}:
                    raise ServiceError(409, f'cannot continue thread from {snapshot.status}')
                if snapshot.status == 'paused':
                    await self.flow.resume(thread_id)
                self._ensure_auto_task(thread_id)
                return _accepted(thread_id, request.command_id, 'continue')

            snapshot = await self.flow.snapshot(thread_id)
            pending = snapshot.pending_approval
            if pending is None:
                raise ServiceError(409, 'thread is not awaiting approval')
            next_stage = _next_stage(pending.stage)
            target = request.until_step or next_stage
            if target != next_stage:
                raise ServiceError(
                    422,
                    f'continue from {pending.stage} runs through {next_stage}',
                )
            await self.flow.approve(thread_id, pending.stage)
            return _accepted(thread_id, request.command_id, 'continue')

    async def retry(self, thread_id: str, request: RetryRequest | Mapping[str, Any]) -> dict[str, str]:
        request = _retry_request(request)
        _validate_retry_stage(request.stage)
        command_id = (
            request.command_id.strip()
            or f'retry:{thread_id}:{time.time_ns()}'
        )

        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            snapshot = await self.flow.snapshot(thread_id)
            stage = request.stage or snapshot.current_stage
            await self.flow.retry_stage(thread_id, stage, request_id=command_id)
            await self._continue_automatic(thread_id)
            return _accepted(thread_id, command_id, 'retry')

    async def rerun_stage(self, thread_id: str, stage: str, request: ControlRequest | Mapping[str, Any]
                          ) -> dict[str, str]:
        request = _control_request(request)
        _validate_retry_stage(stage)
        command_id = request.command_id.strip() or f'rerun-stage:{thread_id}:{stage}:{time.time_ns()}'
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            await self.flow.rerun_stage(thread_id, stage, request_id=command_id)
            await self._continue_automatic(thread_id)
            return _accepted(thread_id, command_id, 'rerun-stage')

    async def rerun_case(self, thread_id: str, case_id: str, request: CaseRerunBody | Mapping[str, Any]
                         ) -> dict[str, str]:
        request = request if isinstance(request, CaseRerunBody) else CaseRerunBody.model_validate(request)
        command_id = request.command_id.strip() or f'rerun-case:{thread_id}:{case_id}:{time.time_ns()}'
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            await self.flow.rerun_case(
                thread_id,
                case_id,
                request_id=command_id,
                from_stage=request.stage,
                from_artifact=(ArtifactKey.partition(request.artifact_id, case_id) if request.artifact_id else None),
            )
            await self._continue_automatic(thread_id)
            return _accepted(thread_id, command_id, 'rerun-case')

    async def retry_case(self, thread_id: str, case_id: str, request: ControlRequest | Mapping[str, Any]
                         ) -> dict[str, str]:
        request = _control_request(request)
        command_id = request.command_id.strip() or f'retry-case:{thread_id}:{case_id}:{time.time_ns()}'
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            await self.flow.retry_failed_case(thread_id, case_id, request_id=command_id)
            await self._continue_automatic(thread_id)
            return _accepted(thread_id, command_id, 'retry-case')

    async def pause(self, thread_id: str, request: ControlRequest | Mapping[str, Any]) -> dict[str, str]:
        request = _control_request(request)
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            await self._stop_auto_task(thread_id)
            snapshot = await self.flow.snapshot(thread_id)
            if snapshot.status != 'paused':
                await self.flow.pause(thread_id)
            return _accepted(thread_id, request.command_id, 'pause')

    async def resume(self, thread_id: str, request: ControlRequest | Mapping[str, Any]) -> dict[str, str]:
        request = _control_request(request)
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            await self.flow.resume(thread_id)
            await self._continue_automatic(thread_id)
            return _accepted(thread_id, request.command_id, 'resume')

    async def cancel(self, thread_id: str, request: ControlRequest | Mapping[str, Any]) -> dict[str, str]:
        request = _control_request(request)
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            snapshot = await self.flow.snapshot(thread_id)
            if snapshot.status != 'cancelled':
                await self.flow.cancel(thread_id)
            return _accepted(thread_id, request.command_id, 'cancel')

    async def update_artifacts(self, thread_id: str, request: ArtifactUpdateBody | Mapping[str, Any]) -> dict[str, Any]:
        request = request if isinstance(request, ArtifactUpdateBody) else ArtifactUpdateBody.model_validate(request)
        config_targets = sorted({item.artifact_id for item in request.updates} & _CONFIG_ARTIFACT_IDS)
        if config_targets:
            raise ServiceError(422, f'configuration requires the configuration endpoint: {", ".join(config_targets)}')
        await self.flow.update_artifacts(
            thread_id,
            tuple(
                ArtifactUpdate(
                    ArtifactRef(ArtifactKey(item.artifact_id, item.partition_key), item.base_version),
                    item.value,
                )
                for item in request.updates
            ),
            request_id=request.request_id,
        )
        await self._continue_automatic(thread_id)
        return await self.public_thread(thread_id, include_inputs=False)

    async def apply_materials(self, thread_id: str, request: DatasetApplyBody | Mapping[str, Any]) -> dict[str, Any]:
        request = request if isinstance(request, DatasetApplyBody) else DatasetApplyBody.model_validate(request)
        if 'chunk_selection_changes' in request.changes:
            if set(request.changes) != {'chunk_selection_changes'}:
                raise ServiceError(400, 'chunk_selection_changes cannot be combined with scan configuration')
            return await self.apply_material_chunk_selection(thread_id, request.model_dump())
        return await self.apply_material_scan_config(thread_id, request.model_dump())

    async def apply_material_scan_config(self, thread_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        request_id, revision, changes = _dataset_apply_request(request)
        scan_fields = {'target_case_count', 'knowledge_bases', 'documents', 'split_rule_ids', 'layout_type_ids'}
        if not changes or set(changes) - scan_fields:
            raise ServiceError(400, 'changes must contain only scan configuration fields')
        source_key = ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG)
        selection_key = ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS)
        chunks_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS)
        refs = _revision_refs(revision, (source_key, selection_key, chunks_key))
        source, selection, params = await self._read_expected_values(thread_id, refs)
        source = _copy_mapping(source, 'source_config')
        selection = _copy_mapping(selection, 'select_docs_params')
        params = _copy_mapping(params, 'build_chunks_params')
        source_ids = _source_ids(source)
        if 'target_case_count' in changes:
            target = changes['target_case_count']
            if isinstance(target, bool) or not isinstance(target, int) or target < 1:
                raise ServiceError(422, 'target_case_count must be a positive integer')
            source['target_case_count'] = target
            source['min_case_count'] = target
        if 'knowledge_bases' in changes:
            enabled = _selection_map(selection, source_ids)
            for item in _list_value(changes['knowledge_bases'], 'knowledge_bases'):
                entry = _mapping_value(item, 'knowledge_bases[]')
                kb_id = _text_value(entry.get('id'), 'knowledge_bases[].id')
                included = entry.get('included')
                if kb_id not in enabled:
                    raise ServiceError(404, 'knowledge base not found')
                if not isinstance(included, bool):
                    raise ServiceError(422, 'knowledge base change is invalid')
                enabled[kb_id] = included
            selection['knowledge_bases'] = [{'kb_id': key, 'included': enabled[key]} for key in source_ids]
        if 'documents' in changes:
            documents = await self._current_documents(thread_id)
            excluded = _excluded_document_keys(selection)
            for item in _list_value(changes['documents'], 'documents'):
                entry = _mapping_value(item, 'documents[]')
                key = (_text_value(entry.get('knowledge_base_id'), 'documents[].knowledge_base_id'),
                       _text_value(entry.get('document_id'), 'documents[].document_id'))
                included = entry.get('included')
                if key not in documents:
                    raise ServiceError(404, 'document not found')
                if not isinstance(included, bool):
                    raise ServiceError(422, 'document change is invalid')
                (excluded.discard if included else excluded.add)(key)
            selection['excluded_docs'] = [{'kb_id': kb_id, 'doc_id': doc_id} for kb_id, doc_id in sorted(excluded)]
        if 'split_rule_ids' in changes:
            params['groups'] = _id_list(changes['split_rule_ids'], 'split_rule_ids')
        if 'layout_type_ids' in changes:
            params['allowed_types'] = _id_list(changes['layout_type_ids'], 'layout_type_ids')
        _validate_material_parser_capabilities(
            getattr(self, 'capability_client', None), source_ids, _selection_map(selection, source_ids), params,
        )
        return await self._commit_changed_values(
            thread_id, f'dataset-materials-scan:{request_id}', 'user:dataset-materials-scan', refs,
            {source_key: source, selection_key: selection, chunks_key: params},
        )

    async def apply_material_chunk_selection(self, thread_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        request_id, revision, changes = _dataset_apply_request(request)
        if set(changes) != {'chunk_selection_changes'}:
            raise ServiceError(400, 'changes must contain only chunk_selection_changes')
        updates = _list_value(changes['chunk_selection_changes'], 'chunk_selection_changes')
        if not updates:
            raise ServiceError(400, 'chunk_selection_changes must not be empty')
        docs_key = ArtifactKey.scalar(A.DATASET_SELECTED_DOCS)
        candidates_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)
        refs = _revision_refs(revision, (docs_key, candidates_key))
        _, candidates = await self._read_expected_values(thread_id, refs)
        candidates = _copy_mapping(candidates, 'build_chunk_candidates')
        chunks = _list_value(candidates.get('chunks', ()), 'build_chunk_candidates.chunks')
        by_identity = {(item.get('kb_id'), item.get('doc_id'), item.get('chunk_id')): item for item in chunks if isinstance(item, dict)}
        seen: set[tuple[str, str, str]] = set()
        for item in updates:
            entry = _mapping_value(item, 'chunk_selection_changes[]')
            identity = (_text_value(entry.get('knowledge_base_id'), 'chunk_selection_changes[].knowledge_base_id'),
                        _text_value(entry.get('document_id'), 'chunk_selection_changes[].document_id'),
                        _text_value(entry.get('chunk_id'), 'chunk_selection_changes[].chunk_id'))
            selected = entry.get('selected')
            if identity in seen or not isinstance(selected, bool):
                raise ServiceError(422, 'chunk selection change is invalid')
            if identity not in by_identity:
                raise ServiceError(404, 'chunk not found')
            seen.add(identity)
            by_identity[identity]['selected'] = selected
        _validate_candidate_quotas(chunks, candidates.get('quotas', ()))
        return await self._commit_changed_values(
            thread_id, f'dataset-materials-selection:{request_id}', 'user:dataset-materials-selection', refs,
            {candidates_key: candidates},
        )

    async def apply_topic_names(self, thread_id: str, request: TopicApplyBody | Mapping[str, Any]) -> dict[str, Any]:
        request = request if isinstance(request, TopicApplyBody) else TopicApplyBody.model_validate(request)
        topic_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
        refs = _revision_refs(request.expected_revision, (topic_key,))
        (manifest,) = await self._read_expected_values(thread_id, refs)
        manifest = _copy_mapping(manifest, 'topic_manifest')
        topics = _list_value(manifest.get('topics', ()), 'topic_manifest.topics')
        by_id = {item.get('topic_id'): item for item in topics if isinstance(item, dict)}
        seen: set[str] = set()
        for item in request.changes:
            entry = _mapping_value(item, 'changes[]')
            topic_id = _text_value(entry.get('topic_id'), 'changes[].topic_id')
            name = _text_value(entry.get('name'), 'changes[].name')
            if topic_id in seen:
                raise ServiceError(422, 'topic name change is invalid')
            if topic_id not in by_id:
                raise ServiceError(404, 'topic not found')
            seen.add(topic_id)
            by_id[topic_id]['name'] = name
        return await self._commit_changed_values(
            thread_id, f'dataset-topic-names:{request.request_id}', 'user:dataset-topic-names', refs,
            {topic_key: manifest},
        )

    async def _read_expected_values(self, thread_id: str, refs: tuple[ArtifactRef, ...]) -> tuple[object, ...]:
        if not await self.flow.has_run(thread_id):
            raise ServiceError(404, f'thread not found: {thread_id}')
        try:
            return await asyncio.gather(*(self.flow.read(thread_id, ref) for ref in refs))
        except (DefinitionError, KeyError) as error:
            raise ServiceError(409, 'expected_revision is unavailable') from error

    async def _current_documents(self, thread_id: str) -> set[tuple[str, str]]:
        key = ArtifactKey.scalar(A.DATASET_SELECTED_DOCS)
        record = await self.flow.head(thread_id, key)
        if record is None:
            raise ServiceError(404, 'selected documents are unavailable')
        value = _mapping_value(await self.flow.read(thread_id, record.ref), 'selected_docs')
        return {(item.get('kb_id'), item.get('doc_id')) for item in _list_value(value.get('documents', ()), 'selected_docs.documents')
                if isinstance(item, Mapping) and isinstance(item.get('kb_id'), str) and isinstance(item.get('doc_id'), str)}

    async def _commit_changed_values(self, thread_id: str, commit_id: str, producer: str,
                                     refs: tuple[ArtifactRef, ...], values: Mapping[ArtifactKey, object]) -> dict[str, Any]:
        current_values = await asyncio.gather(*(self.flow.read(thread_id, ref) for ref in refs))
        writes = tuple(
            ArtifactDraft(ref.key, values[ref.key])
            for ref, current in zip(refs, current_values, strict=True)
            if ref.key in values and values[ref.key] != current
        )
        if not writes:
            raise ServiceError(422, 'changes do not alter the current value')
        try:
            await self.flow.commit(thread_id, ArtifactCommit(commit_id, producer, writes, {ref.key: ref for ref in refs}))
        except DefinitionError as error:
            raise ServiceError(409, str(error)) from error
        await self._continue_automatic(thread_id)
        heads = await asyncio.gather(*(self.flow.head(thread_id, ref.key) for ref in refs))
        if any(record is None for record in heads):
            raise ServiceError(503, 'committed configuration is unavailable')
        return {'request_id': commit_id.rsplit(':', 1)[-1], 'status': 'applied',
                'revision': ProjectionService._build_revision(tuple(record.ref for record in heads if record is not None))}

    async def set_automatic(self, thread_id: str, request: AutomaticUpdateBody | Mapping[str, Any]) -> dict[str, Any]:
        request = request if isinstance(request, AutomaticUpdateBody) else AutomaticUpdateBody.model_validate(request)
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            configuration = await self.flow.configuration(thread_id)
            try:
                await self.flow.update_configuration(
                    thread_id,
                    {**configuration.values, 'mode': 'interactive', 'automatic': request.enabled},
                    request_id=request.request_id,
                    base_version=request.base_version,
                )
            except DefinitionError as exc:
                if str(exc) != 'artifact commit precondition is stale':
                    raise
                raise ServiceError(409, 'automatic base_version is stale') from exc
            if request.enabled:
                self._ensure_auto_task(thread_id)
            else:
                await self._stop_auto_task(thread_id)
        return await self.public_thread(thread_id, include_inputs=False)

    async def update_configuration(self, thread_id: str, request: ConfigurationUpdateBody | Mapping[str, Any]
                                   ) -> dict[str, Any]:
        request = (
            request
            if isinstance(request, ConfigurationUpdateBody)
            else ConfigurationUpdateBody.model_validate(request)
        )
        key = ArtifactKey.scalar(_CONFIG_ARTIFACTS[request.target])
        await self.flow.update_artifacts(
            thread_id,
            (ArtifactUpdate(ArtifactRef(key, request.base_version), request.value),),
            request_id=request.request_id,
        )
        await self._continue_automatic(thread_id)
        return await self.public_thread(thread_id, include_inputs=False)

    async def update_cases(self, thread_id: str, request: CaseStructureBody | Mapping[str, Any]) -> dict[str, Any]:
        request = request if isinstance(request, CaseStructureBody) else CaseStructureBody.model_validate(request)
        set_key = ArtifactKey.scalar(request.partition_set_id)
        writes = (
            ArtifactDraft(set_key, PartitionSet(tuple(request.case_ids))),
            *(
                ArtifactDraft(ArtifactKey.partition(seed.artifact_id, seed.case_id), seed.value)
                for seed in request.seeds
            ),
        )
        await self.flow.commit(
            thread_id,
            ArtifactCommit(
                f'case-structure:{request.request_id}',
                'user:case-structure',
                writes,
                {
                    set_key: ArtifactRef(set_key, request.base_version),
                    **{write.key: None for write in writes[1:]},
                },
            ),
        )
        await self._continue_automatic(thread_id)
        return await self.public_thread(thread_id, include_inputs=False)

    async def submit_external_result(self, thread_id: str, attempt_id: str,
                                     request: ExternalResultBody | Mapping[str, Any]) -> dict[str, str]:
        request = request if isinstance(request, ExternalResultBody) else ExternalResultBody.model_validate(request)
        await self.flow.submit_external_result(thread_id, attempt_id, OperationResult(request.values))
        await self._continue_automatic(thread_id)
        return {'status': 'accepted', 'thread_id': thread_id, 'attempt_id': attempt_id}

    async def message(self, thread_id: str, request: MessageRequest) -> MessageTurnResult:
        lock = self._message_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            result = await self.messages.run('user', thread_id, request)
        snapshot, automatic = await asyncio.gather(
            self.flow.snapshot(thread_id),
            self._auto_enabled(thread_id),
        )
        if automatic and snapshot.status not in _AUTO_STOPPED:
            self._ensure_auto_task(thread_id)
        elif not automatic:
            await self._stop_auto_task(thread_id)
        return result

    async def message_history(self, thread_id: str, page_size: int, page_token: str) -> dict[str, Any]:
        await self.flow.snapshot(thread_id)
        history = await asyncio.to_thread(
            self.messages.history,
            thread_id,
            page_size,
            page_token,
        )
        return public_message_history(history)

    async def delete_thread(self, thread_id: str) -> dict[str, Any]:
        async with self._control_locks.setdefault(thread_id, asyncio.Lock()):
            automatic = await self._auto_enabled(thread_id)
            await self._stop_auto_task(thread_id)
            try:
                await self.flow.release(thread_id)
            except BaseException:
                if automatic:
                    self._ensure_auto_task(thread_id)
                raise
            lock = self._message_locks.setdefault(thread_id, asyncio.Lock())
            async with lock:
                await self.router.delete_thread(thread_id)
                await self.messages.delete_thread(thread_id)
                await self.flow.delete_run(thread_id)
            return {
                'thread_id': thread_id,
                'deleted': True,
                'message': 'thread deleted',
            }

    async def close(self) -> None:
        self._closing = True
        tasks = tuple(self._auto_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._auto_tasks.clear()
        await self.flow.close()

    async def _run_config(self, thread_id: str) -> Mapping[str, Any]:
        record = await self.flow.head(thread_id, ArtifactKey.scalar(A.RUN_CONFIG))
        if record is None:
            return {}
        value = await self.flow.read(thread_id, record.ref)
        return value if isinstance(value, Mapping) else {}

    async def _auto_enabled(self, thread_id: str) -> bool:
        return bool((await self.flow.configuration(thread_id)).values.get('automatic'))

    async def _continue_automatic(self, thread_id: str) -> None:
        if await self._auto_enabled(thread_id):
            self._ensure_auto_task(thread_id)

    def _ensure_auto_task(self, thread_id: str) -> None:
        if self._closing:
            return
        current = self._auto_tasks.get(thread_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._drive_auto(thread_id),
            name=f'evo-auto:{thread_id}',
        )
        self._auto_tasks[thread_id] = task
        task.add_done_callback(
            lambda completed, run_id=thread_id: self._auto_task_done(
                run_id,
                completed,
            )
        )

    async def _drive_auto(self, thread_id: str) -> None:
        while not self._closing:
            lock = self._control_locks.setdefault(thread_id, asyncio.Lock())
            async with lock:
                snapshot = await self.flow.snapshot(thread_id)
                if snapshot.status in _AUTO_STOPPED:
                    return
                if snapshot.status == 'paused':
                    return
                if snapshot.status == 'awaiting_approval':
                    pending = snapshot.pending_approval
                    if pending is None:
                        raise RuntimeError('awaiting approval without pending stage')
                    await self.flow.approve(thread_id, pending.stage)
                    continue

            try:
                await self.flow.wait_until_boundary(
                    thread_id,
                    timeout=_AUTO_WAIT_TIMEOUT,
                )
            except TimeoutError:
                continue

    def _auto_task_done(self, thread_id: str, task: asyncio.Task[None]) -> None:
        if self._auto_tasks.get(thread_id) is task:
            del self._auto_tasks[thread_id]
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                'auto runner failed for %s',
                thread_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _stop_auto_task(self, thread_id: str) -> None:
        task = self._auto_tasks.pop(thread_id, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _dataset_apply_request(request: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    request_id = _text_value(request.get('request_id'), 'request_id')
    revision = _text_value(request.get('expected_revision'), 'expected_revision')
    changes = request.get('changes')
    if not isinstance(changes, Mapping):
        raise ServiceError(400, 'changes must be an object')
    return request_id, revision, changes


def _revision_refs(revision: str, keys: tuple[ArtifactKey, ...]) -> tuple[ArtifactRef, ...]:
    refs = ProjectionService._resolve_revision(revision)
    if {ref.key for ref in refs} != set(keys):
        raise ServiceError(400, 'expected_revision does not match this operation')
    return tuple(next(ref for ref in refs if ref.key == key) for key in keys)


def _copy_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ServiceError(503, f'{name} is invalid')
    return copy.deepcopy(dict(value))


def _mapping_value(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ServiceError(422, f'{name} must be an object')
    return value


def _list_value(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ServiceError(422, f'{name} must be an array')
    return value


def _text_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ServiceError(422, f'{name} must be a non-empty string')
    return value.strip()


def _source_ids(source: Mapping[str, Any]) -> list[str]:
    try:
        return normalize_source_config(source)['kb_ids']
    except ValueError as error:
        raise ServiceError(503, 'source_config knowledge bases are invalid') from error


def _selection_map(selection: Mapping[str, Any], source_ids: list[str]) -> dict[str, bool]:
    entries = _list_value(selection.get('knowledge_bases'), 'select_docs_params.knowledge_bases')
    values = {
        _text_value(_mapping_value(item, 'knowledge_bases[]').get('kb_id'), 'knowledge_bases[].kb_id'):
        _mapping_value(item, 'knowledge_bases[]').get('included')
        for item in entries
    }
    if set(values) != set(source_ids) or not all(isinstance(value, bool) for value in values.values()):
        raise ServiceError(503, 'select_docs_params knowledge bases are invalid')
    return values


def _excluded_document_keys(selection: Mapping[str, Any]) -> set[tuple[str, str]]:
    return {
        (_text_value(entry.get('kb_id'), 'excluded_docs[].kb_id'), _text_value(entry.get('doc_id'), 'excluded_docs[].doc_id'))
        for item in _list_value(selection.get('excluded_docs', []), 'select_docs_params.excluded_docs')
        for entry in (_mapping_value(item, 'excluded_docs[]'),)
    }


def _id_list(value: object, name: str) -> list[str]:
    values = [_text_value(item, f'{name}[]') for item in _list_value(value, name)]
    if not values or len(set(values)) != len(values):
        raise ServiceError(422, f'{name} must contain unique non-empty values')
    return values


def _validate_material_parser_capabilities(client: object | None, source_ids: list[str], enabled: Mapping[str, bool],
                                           params: Mapping[str, object]) -> None:
    if client is None:
        return
    parser_capabilities = getattr(client, 'parser_capabilities', None)
    if not callable(parser_capabilities):
        raise ServiceError(503, 'parser capabilities are unavailable')
    try:
        capabilities = parser_capabilities(source_ids)
    except Exception as exc:
        raise ServiceError(503, 'parser capabilities are unavailable') from exc
    active_sources = [kb_id for kb_id in source_ids if enabled.get(kb_id) is True]
    for field, param in (('split_rules', 'groups'), ('layout_types', 'allowed_types')):
        requested = _list_value(params.get(param, ()), f'build_chunks_params.{param}')
        supported = _supported_capability_ids(capabilities, active_sources, field)
        if not active_sources or any(item not in supported for item in requested):
            raise ServiceError(422, f'{param} contains a capability unsupported by current material sources')


def _supported_capability_ids(capabilities: object, active_sources: list[str], field: str) -> set[str]:
    if not isinstance(capabilities, Mapping):
        raise ServiceError(503, 'parser capabilities are invalid')
    common: set[str] | None = None
    for kb_id in active_sources:
        capability = capabilities.get(kb_id)
        if not isinstance(capability, Mapping):
            raise ServiceError(503, 'parser capabilities are invalid')
        values = capability.get(field)
        if not isinstance(values, list):
            raise ServiceError(503, 'parser capabilities are invalid')
        identifiers = {
            item.get('id') for item in values
            if isinstance(item, Mapping) and isinstance(item.get('id'), str) and item.get('id')
        }
        if len(identifiers) != len(values):
            raise ServiceError(503, 'parser capabilities are invalid')
        common = identifiers if common is None else common & identifiers
    return common or set()


def _validate_candidate_quotas(chunks: list[Any], quotas: object) -> None:
    rows = _list_value(quotas, 'build_chunk_candidates.quotas')
    for item in rows:
        quota = _mapping_value(item, 'quotas[]')
        key = (_text_value(quota.get('kb_id'), 'quotas[].kb_id'), _text_value(quota.get('doc_id'), 'quotas[].doc_id'),
               _text_value(quota.get('group'), 'quotas[].group'))
        required = quota.get('required')
        if isinstance(required, bool) or not isinstance(required, int) or required < 0:
            raise ServiceError(503, 'chunk quota is invalid')
        selected = sum(
            row.get('selected') is True
            for row in chunks
            if isinstance(row, Mapping) and (row.get('kb_id'), row.get('doc_id'), row.get('group')) == key
        )
        if selected != required:
            raise ServiceError(422, 'chunk selection does not satisfy quota')


def _seed_values(thread_id: str, request: ThreadCreate) -> dict[str, object]:
    inputs = request.inputs.model_dump()
    llm_config = dict(request.llm_config)
    target_config = {
        'router_chat_url': request.inputs.router_chat_url,
        'router_admin_url': request.inputs.router_admin_url,
        'algorithm_id': request.inputs.algorithm_id,
        'llm_config': llm_config,
        'case_deadline_seconds': request.inputs.case_deadline_seconds,
        'first_frame_timeout_seconds': _FIRST_FRAME_TIMEOUT,
    }
    source_config = {
        'kb_id': request.inputs.kb_id,
        'knowledge_base_names': request.inputs.knowledge_base_names,
        'csv_data': request.inputs.csv_data,
        'target_case_count': request.inputs.num_case,
        'min_case_count': request.inputs.num_case,
    }
    source_ids = normalize_source_config(source_config)['kb_ids']
    return {
        A.RUN_CONFIG: {
            'thread_id': thread_id,
            'title': request.title,
            'inputs': inputs,
            'num_case': request.inputs.num_case,
            'llm_config': llm_config,
        },
        A.CORPUS_SOURCE_CONFIG: source_config,
        A.DATASET_SELECT_DOCS_PARAMS: {
            'knowledge_bases': [
                {'kb_id': kb_id, 'included': True}
                for kb_id in source_ids
            ],
            'excluded_docs': [],
        },
        A.DATASET_BUILD_CHUNKS_PARAMS: {},
        A.DATASET_QAPLAN_PLAN_PARAMS: {},
        A.EVAL_TARGET_CONFIG: target_config,
        A.EVAL_POLICY: {'judge_llm_config': llm_config},
        A.REPAIR_POLICY: {
            'llm_config': llm_config,
            'thread_id': thread_id,
            'workspace_namespace': thread_id,
        },
        A.ABTEST_CANDIDATE_CONFIG: {
            'router_chat_url': request.inputs.router_chat_url,
            'router_admin_url': request.inputs.router_admin_url,
            'llm_config': llm_config,
            'case_deadline_seconds': request.inputs.case_deadline_seconds,
            'first_frame_timeout_seconds': _FIRST_FRAME_TIMEOUT,
        },
    }


def _public_thread(request: ThreadCreate, snapshot: FlowSnapshot) -> dict[str, Any]:
    return {
        'thread_id': snapshot.run_id,
        'mode': request.mode,
        'automatic': request.automatic,
        'automatic_version': 1,
        'title': request.title,
        **public_thread_state(snapshot),
    }


def _command(request: CommandRequest | Mapping[str, Any]) -> CommandRequest:
    return request if isinstance(request, CommandRequest) else CommandRequest.model_validate(request)


def _control_request(request: ControlRequest | Mapping[str, Any]) -> ControlRequest:
    return request if isinstance(request, ControlRequest) else ControlRequest.model_validate(request)


def _retry_request(request: RetryRequest | Mapping[str, Any]) -> RetryRequest:
    return request if isinstance(request, RetryRequest) else RetryRequest.model_validate(request)


def _accepted(thread_id: str, command_id: str, command: str) -> dict[str, str]:
    return {
        'status': 'accepted',
        'thread_id': thread_id,
        'command_id': command_id.strip() or f'{command}:{thread_id}:{time.time_ns()}',
    }


def _page_offset(token: str) -> int:
    if not str(token or '0').isdigit():
        raise ServiceError(422, 'page_token must be a non-negative integer offset')
    return int(token or 0)


def _validate_retry_stage(stage: str) -> None:
    if stage and stage not in _STAGES:
        raise ServiceError(422, f'stage must be one of: {", ".join(_STAGES)}')


def _next_stage(stage: str) -> str:
    index = _STAGES.index(stage)
    return _STAGES[index + 1] if index + 1 < len(_STAGES) else ''


__all__ = ['EvoService']
