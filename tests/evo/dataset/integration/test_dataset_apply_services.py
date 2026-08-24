from __future__ import annotations

import asyncio
import importlib
import pytest

from evo import artifacts as A
from evo.artifact_flow import ArtifactFlow, ArtifactUpdate, FlowDefinition, FlowStage
from evo.artifact_runtime import (
    ArtifactCommit,
    ArtifactDraft,
    ArtifactKey,
    ArtifactRecord,
    ArtifactRef,
    DefinitionError,
    PartitionSet,
)
from evo.service.contracts import ServiceError
from evo.service.core import EvoService
from evo.service.projections import ProjectionService


dataset_module = importlib.import_module('evo.operations.dataset.operations')


class _ApplyFlow:
    def __init__(self, values: dict[ArtifactKey, tuple[int, object]]) -> None:
        self.values = values
        self.commits: list[ArtifactCommit] = []
        self.status = 'running'
        self.resume_calls: list[str] = []

    async def has_run(self, _thread_id: str) -> bool:
        return True

    async def head(self, _thread_id: str, key: ArtifactKey) -> ArtifactRecord | None:
        value = self.values.get(key)
        return None if value is None else ArtifactRecord(ArtifactRef(key, value[0]), 'test')

    async def read(self, _thread_id: str, ref: ArtifactRef) -> object:
        version, value = self.values[ref.key]
        assert ref.version == version
        return value

    async def commit(self, _thread_id: str, commit: ArtifactCommit) -> object:
        self.commits.append(commit)
        return object()

    async def commit_values(self, thread_id: str, commit: ArtifactCommit) -> object:
        return await self.commit(thread_id, commit)

    async def commit_structure_with_values(
        self, thread_id: str, commit: ArtifactCommit, *, value_keys: tuple[ArtifactKey, ...],
    ) -> object:
        del value_keys
        return await self.commit(thread_id, commit)

    async def snapshot(self, _thread_id: str) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(status=self.status)

    async def resume(self, thread_id: str) -> None:
        self.resume_calls.append(thread_id)


def _service(values: dict[ArtifactKey, tuple[int, object]]) -> tuple[EvoService, _ApplyFlow]:
    flow = _ApplyFlow(values)
    service = object.__new__(EvoService)
    service.flow = flow

    async def _continue(_thread_id: str) -> None:
        return None

    service._continue_automatic = _continue  # type: ignore[method-assign]
    return service, flow


class _CapabilityClient:
    def __init__(self, values: dict[str, dict]) -> None:
        self.values = values

    def parser_capabilities(self, kb_ids: list[str]) -> dict[str, dict]:
        return {kb_id: self.values[kb_id] for kb_id in kb_ids}


def _revision(*refs: ArtifactRef) -> str:
    return ProjectionService._build_revision(tuple(refs))


def _material_values() -> dict[ArtifactKey, tuple[int, object]]:
    return {
        ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG): (3, {
            'kb_id': ['kb-a'], 'knowledge_base_names': {'kb-a': 'A'},
            'csv_data': [], 'target_case_count': 3,
        }),
        ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS): (4, {
            'knowledge_bases': [{'kb_id': 'kb-a', 'included': True}],
            'excluded_docs': [],
        }),
        ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS): (5, {
            'groups': ['block'], 'allowed_types': ['text'],
        }),
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): (6, {'documents': [
            {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'included': True},
        ]}),
        ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES): (7, {
            'chunks': [
                {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': True, 'group': 'block'},
                {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-2', 'selected': False, 'group': 'block'},
            ],
            'quotas': [{'kb_id': 'kb-a', 'doc_id': 'doc-1', 'group': 'block', 'required': 1}],
        }),
        ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS): (8, PartitionSet(('chunk-1',))),
        ArtifactKey.partition(A.DATASET_CHUNK_REQUEST, 'chunk-1'): (1, {'partition_key': 'chunk-1'}),
    }


def _generation_plan_values() -> dict[ArtifactKey, tuple[int, object]]:
    return _generation_plan_values_with_plan()


def _generation_plan_values_with_plan() -> dict[ArtifactKey, tuple[int, object]]:
    return {
        **_generation_plan_topic_values(),
        ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN): (13, {
            'stats': {
                'auto_case_count': 4,
                'lane_summaries': [
                    {'lane': 'precision_easy', 'eligible_topic_count': 2},
                    {'lane': 'precision_medium', 'eligible_topic_count': 1},
                    {'lane': 'precision_hard', 'eligible_topic_count': 1},
                    {'lane': 'reasoning_easy', 'eligible_topic_count': 2},
                    {'lane': 'reasoning_medium', 'eligible_topic_count': 1},
                    {'lane': 'reasoning_hard', 'eligible_topic_count': 1},
                ],
            },
        }),
    }


def _generation_plan_topic_values(*, eligible: dict[str, int] | None = None) -> dict[ArtifactKey, tuple[int, object]]:
    eligible = eligible or {
        'precision_easy': 2,
        'precision_medium': 1,
        'precision_hard': 1,
        'reasoning_easy': 2,
        'reasoning_medium': 1,
        'reasoning_hard': 1,
    }

    def _topics(count: int, *, question_type: str, chunks: int, start: int) -> list[dict[str, object]]:
        return [{
            'topic_id': f'{question_type}-{start + index}',
            'name': f'{question_type} {start + index}',
            'question_type': question_type,
            'chunk_ids': [f'chunk-{question_type}-{start + index}-{part}' for part in range(chunks)],
            'chunk_count': chunks,
        } for index in range(count)]

    next_id = 1
    topic_groups = []
    for question_type, chunks, lane_key in (
        ('precision', 1, 'precision_easy'),
        ('precision', 2, 'precision_medium'),
        ('precision', 3, 'precision_hard'),
        ('reasoning', 1, 'reasoning_easy'),
        ('reasoning', 2, 'reasoning_medium'),
        ('reasoning', 3, 'reasoning_hard'),
    ):
        count = eligible[lane_key]
        topic_groups.extend(_topics(count, question_type=question_type, chunks=chunks, start=next_id))
        next_id += count
    topics = topic_groups
    return {
        ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS): (12, {}),
        ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): (14, {'topics': topics}),
        ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST): (15, {
            'stats': {
                'case_allocation': {
                    'target_case_count': 4,
                    'import_case_count': 0,
                    'auto_case_count': 4,
                    'assignments': {
                        f'case_{index:04d}': {'mode': 'generated'}
                        for index in range(1, 5)
                    },
                },
            },
        }),
    }


def _distribution(*, precision_easy: int = 1, precision_medium: int = 1, precision_hard: int = 0,
                  reasoning_easy: int = 1, reasoning_medium: int = 1, reasoning_hard: int = 0) -> dict[str, object]:
    return {
        'precision': {'easy': precision_easy, 'medium': precision_medium, 'hard': precision_hard},
        'reasoning': {'easy': reasoning_easy, 'medium': reasoning_medium, 'hard': reasoning_hard},
    }


def _case_patch_values(*, source: str = 'generated') -> dict[ArtifactKey, tuple[int, object]]:
    case_id = 'case-1'
    refs = [
        {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-old-1', 'text': '旧引用一'},
        {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-old-2', 'text': '旧引用二'},
    ]
    return {
        ArtifactKey.scalar(A.EVAL_CASE_REQUESTS): (21, PartitionSet((case_id,))),
        ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST): (22, {
            'stats': {'case_allocation': {
                'target_case_count': 1,
                'import_case_count': 0 if source == 'generated' else 1,
                'auto_case_count': 1 if source == 'generated' else 0,
                'assignments': {case_id: {'mode': source}},
            }},
            'details': [],
        }),
        ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN): (23, {'items': [{
            'case_id': case_id, 'plan_item_id': 'plan-1', 'lane': 'precision_medium',
            'question_type': 'precision', 'difficulty': 'medium', 'topic_id': 'topic-old',
        }]}),
        ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): (24, {'topics': [
            {'topic_id': 'topic-old', 'name': '旧主题', 'question_type': 'precision',
             'chunk_ids': ['chunk-old-1', 'chunk-old-2'], 'chunk_count': 2},
            {'topic_id': 'topic-new', 'name': '新主题', 'question_type': 'precision',
             'chunk_ids': ['chunk-new-1', 'chunk-new-2'], 'chunk_count': 2},
        ]}),
        ArtifactKey.scalar(A.DATASET_SELECTED_DOCS): (25, {'documents': [
            {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'included': True},
        ]}),
        ArtifactKey.partition(A.DATASET_QAPLAN_SPEC, case_id): (26, {
            'id': case_id, 'mode': source, 'question_type': 'precision', 'difficulty': 'medium',
            'topic': {'topic_id': 'topic-old', 'name': '旧主题'},
            'instruction': '旧 instruction', 'qaplan': {'plan_item_id': 'plan-1', 'lane': 'precision_medium'},
            'references': refs,
        }),
        ArtifactKey.partition(A.DATASET_CASE_DRAFT, case_id): (27, {
            'id': case_id, 'question_type': 'precision', 'difficulty': 'medium',
            'question': '旧问题', 'answer': '旧答案', 'grading_guidance': '旧说明',
            'references': refs,
            'reference_context': [{'chunk_id': item['chunk_id'], 'text': item['text']} for item in refs],
            'reference_chunk_ids': [item['chunk_id'] for item in refs],
            'reference_doc_ids': ['doc-1'], 'source_preparation': {'kb_ids': ['kb-a']},
        }),
        ArtifactKey.partition(A.DATASET_CASE_ENHANCEMENT, case_id): (28, {
            'key_points': [{'id': 'key_point_1', 'statement': '旧得分点', 'evidence_chunk_ids': ['chunk-old-1']}],
            'forbidden_claims': [],
        }),
        ArtifactKey.partition(A.DATASET_CHUNK, 'chunk-new-1'): (29, {
            'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-new-1', 'text': '新引用一', 'available': True,
        }),
        ArtifactKey.partition(A.DATASET_CHUNK, 'chunk-new-2'): (30, {
            'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-new-2', 'text': '新引用二', 'available': True,
        }),
    }


def _case_patch_revision() -> str:
    case_id = 'case-1'
    return _revision(
        ArtifactRef(ArtifactKey.scalar(A.EVAL_CASE_REQUESTS), 21),
        ArtifactRef(ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST), 22),
        ArtifactRef(ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN), 23),
        ArtifactRef(ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST), 24),
        ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 25),
        ArtifactRef(ArtifactKey.partition(A.DATASET_QAPLAN_SPEC, case_id), 26),
        ArtifactRef(ArtifactKey.partition(A.DATASET_CASE_DRAFT, case_id), 27),
        ArtifactRef(ArtifactKey.partition(A.DATASET_CASE_ENHANCEMENT, case_id), 28),
    )


def test_patch_case_combines_requested_changes_and_keeps_plan_topic_as_single_source_of_truth() -> None:
    case_id = 'case-1'
    service, flow = _service(_case_patch_values())

    asyncio.run(service.patch_case(case_id='case-1', thread_id='thr-1', request={
        'request_id': 'case-patch-1',
        'expected_revision': _case_patch_revision(),
        'changes': {
            'plan': {'topic_id': 'topic-new'},
            'generate': {'question': '新问题', 'answer': '新答案', 'grading_guidance': '新说明'},
            'grading': {
                'key_points': [{'statement': '新得分点', 'evidence_chunk_ids': ['chunk-new-1']}],
                'forbidden_claims': ['新禁止项'],
            },
        },
    }))

    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-case-patch:case-1:case-patch-1'
    assert commit.expected_heads[ArtifactKey.partition(A.DATASET_CHUNK, 'chunk-new-1')] == ArtifactRef(
        ArtifactKey.partition(A.DATASET_CHUNK, 'chunk-new-1'), 29,
    )
    assert commit.expected_heads[ArtifactKey.partition(A.DATASET_CHUNK, 'chunk-new-2')] == ArtifactRef(
        ArtifactKey.partition(A.DATASET_CHUNK, 'chunk-new-2'), 30,
    )
    writes = {write.key: write.value for write in commit.writes}
    assert writes[ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN)]['items'][0]['topic_id'] == 'topic-new'
    assert [item['chunk_id'] for item in writes[ArtifactKey.partition(A.DATASET_QAPLAN_SPEC, case_id)]['references']] == [
        'chunk-new-1', 'chunk-new-2',
    ]
    assert writes[ArtifactKey.partition(A.DATASET_CASE_DRAFT, case_id)]['question'] == '新问题'
    assert writes[ArtifactKey.partition(A.DATASET_CASE_DRAFT, case_id)]['reference_chunk_ids'] == ['chunk-new-1', 'chunk-new-2']
    assert writes[ArtifactKey.partition(A.DATASET_CASE_ENHANCEMENT, case_id)] == {
        'key_points': [{'id': 'key_point_1', 'statement': '新得分点', 'evidence_chunk_ids': ['chunk-new-1']}],
        'forbidden_claims': ['新禁止项'],
    }


def test_patch_case_rejects_imported_case_topic_changes_and_evidence_outside_effective_references() -> None:
    imported, imported_flow = _service(_case_patch_values(source='imported'))
    with pytest.raises(ServiceError) as source_error:
        asyncio.run(imported.patch_case('thr-1', 'case-1', {
            'request_id': 'imported-plan', 'expected_revision': _case_patch_revision(),
            'changes': {'plan': {'topic_id': 'topic-new'}},
        }))
    assert source_error.value.status_code == 422
    assert imported_flow.commits == []

    service, flow = _service(_case_patch_values())
    with pytest.raises(ServiceError) as evidence_error:
        asyncio.run(service.patch_case('thr-1', 'case-1', {
            'request_id': 'bad-evidence', 'expected_revision': _case_patch_revision(),
            'changes': {'grading': {
                'key_points': [{'statement': '不支持的依据', 'evidence_chunk_ids': ['chunk-not-current']}],
                'forbidden_claims': [],
            }},
        }))
    assert evidence_error.value.status_code == 422
    assert flow.commits == []


def test_patch_case_requires_the_complete_case_detail_revision() -> None:
    service, flow = _service(_case_patch_values())
    plan_ref = ArtifactRef(ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN), 23)

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.patch_case('thr-1', 'case-1', {
            'request_id': 'partial-revision', 'expected_revision': _revision(plan_ref),
            'changes': {'generate': {'question': '新问题', 'answer': '新答案', 'grading_guidance': '新说明'}},
        }))

    assert error.value.status_code == 400
    assert flow.commits == []


def test_apply_generation_plan_converts_distribution_and_commits_params_with_revision_cas() -> None:
    values = _generation_plan_values()
    params_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS)
    params_ref = ArtifactRef(params_key, 12)
    service, flow = _service(values)

    asyncio.run(service.apply_generation_plan('thr-1', {
        'request_id': 'plan-1',
        'expected_revision': _revision(params_ref),
        'distribution': _distribution(),
    }))

    assert len(flow.commits) == 1
    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-generation-plan:plan-1'
    assert commit.expected_heads == {params_key: params_ref}
    assert len(commit.writes) == 1
    assert commit.writes[0].key == params_key
    assert commit.writes[0].value == {'lane_case_counts': {
        'precision_easy': 1, 'precision_medium': 1, 'precision_hard': 0,
        'reasoning_easy': 1, 'reasoning_medium': 1, 'reasoning_hard': 0,
    }}


def test_apply_generation_plan_works_without_qaplan_plan_using_topic_manifest() -> None:
    values = _generation_plan_topic_values()
    params_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS)
    params_ref = ArtifactRef(params_key, 12)
    service, flow = _service(values)

    asyncio.run(service.apply_generation_plan('thr-1', {
        'request_id': 'plan-no-qaplan',
        'expected_revision': _revision(params_ref),
        'distribution': _distribution(),
    }))

    assert len(flow.commits) == 1
    assert flow.commits[0].writes[0].value == {'lane_case_counts': {
        'precision_easy': 1, 'precision_medium': 1, 'precision_hard': 0,
        'reasoning_easy': 1, 'reasoning_medium': 1, 'reasoning_hard': 0,
    }}


@pytest.mark.parametrize(('distribution', 'message'), [
    (_distribution(precision_easy=2, precision_medium=1, reasoning_easy=1, reasoning_medium=1), 'total'),
        (_distribution(precision_easy=1, precision_medium=3, reasoning_easy=0, reasoning_medium=0), 'capacity'),
])
def test_apply_generation_plan_rejects_invalid_total_or_lane_capacity(distribution: dict[str, object], message: str) -> None:
    params_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS)
    service, flow = _service(_generation_plan_values())

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.apply_generation_plan('thr-1', {
            'request_id': f'plan-{message}',
            'expected_revision': _revision(ArtifactRef(params_key, 12)),
            'distribution': distribution,
        }))

    assert error.value.status_code == 422
    assert flow.commits == []


def test_apply_generation_plan_requires_the_overview_params_revision() -> None:
    params_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS)
    plan_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN)
    params_ref = ArtifactRef(params_key, 12)
    plan_ref = ArtifactRef(plan_key, 13)
    service, _ = _service(_generation_plan_values())

    with pytest.raises(ServiceError) as revision_error:
        asyncio.run(service.apply_generation_plan('thr-1', {
            'request_id': 'wrong-revision',
            'expected_revision': _revision(params_ref, plan_ref),
            'distribution': _distribution(),
        }))
    assert revision_error.value.status_code == 400


class _ResumableApplyFlow(_ApplyFlow):
    def __init__(self, values: dict[ArtifactKey, tuple[int, object]], *, status: str = 'paused') -> None:
        super().__init__(values)
        self.status = status
        self.resumed = False

    async def snapshot(self, _thread_id: str) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(status=self.status)

    async def resume(self, thread_id: str) -> None:
        del thread_id
        self.resumed = True
        self.status = 'running'


def test_apply_generation_plan_resumes_paused_thread_after_apply() -> None:
    values = _generation_plan_topic_values()
    params_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS)
    params_ref = ArtifactRef(params_key, 12)
    flow = _ResumableApplyFlow(values)
    service, _ = _service_with_resumable_flow(flow)

    asyncio.run(service.apply_generation_plan('thr-1', {
        'request_id': 'plan-resume',
        'expected_revision': _revision(params_ref),
        'distribution': _distribution(),
    }))

    assert flow.resumed is True
    assert len(flow.commits) == 1


def _service_with_resumable_flow(flow: _ResumableApplyFlow) -> tuple[EvoService, _ResumableApplyFlow]:
    service = object.__new__(EvoService)
    service.flow = flow

    async def _continue(_thread_id: str) -> None:
        return None

    service._continue_automatic = _continue  # type: ignore[method-assign]
    return service, flow


def test_apply_material_scan_config_commits_complete_changed_values_with_three_way_cas() -> None:
    service, flow = _service(_material_values())
    source = ArtifactRef(ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG), 3)
    selection = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS), 4)
    chunks = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS), 5)

    asyncio.run(service.apply_material_scan_config('thr-1', {
        'request_id': 'scan-1',
        'expected_revision': _revision(source, selection, chunks),
        'changes': {
            'target_case_count': 5,
            'documents': [{'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'included': False}],
            'split_rule_ids': ['block', 'line'],
        },
    }))

    assert len(flow.commits) == 1
    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-materials-scan:scan-1'
    assert commit.expected_heads == {ref.key: ref for ref in (source, selection, chunks)}
    values = {write.key.artifact_id: write.value for write in commit.writes}
    assert values[A.CORPUS_SOURCE_CONFIG]['target_case_count'] == 5
    assert values[A.DATASET_SELECT_DOCS_PARAMS]['excluded_docs'] == [{'kb_id': 'kb-a', 'doc_id': 'doc-1'}]
    assert values[A.DATASET_BUILD_CHUNKS_PARAMS]['groups'] == ['block', 'line']


def test_material_apply_rejects_mixing_scan_configuration_and_chunk_selection() -> None:
    service, _ = _service(_material_values())

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.apply_material_scan_config('thr-1', {
            'request_id': 'mixed-1', 'expected_revision': 'ignored',
            'changes': {'target_case_count': 5, 'chunk_selection_changes': []},
        }))
    assert error.value.status_code == 400


def test_material_apply_rejects_enabling_a_capability_not_supported_by_current_sources() -> None:
    service, _ = _service(_material_values())
    service.capability_client = _CapabilityClient({
        'kb-a': {
            'split_rules': [{'id': 'block', 'name': '段落'}],
            'layout_types': [{'id': 'text', 'name': '文本'}],
        },
    })
    source = ArtifactRef(ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG), 3)
    selection = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS), 4)
    chunks = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS), 5)

    with pytest.raises(ServiceError) as error:
        asyncio.run(service.apply_material_scan_config('thr-1', {
            'request_id': 'unsupported-capability',
            'expected_revision': _revision(source, selection, chunks),
            'changes': {'split_rule_ids': ['line']},
        }))

    assert error.value.status_code == 422


def test_material_apply_accepts_changes_that_leave_the_candidate_configuration_untouched() -> None:
    # The params artifact is seeded empty, so adjusting only the case count or the
    # document scope leaves 'groups' and 'allowed_types' absent. Capability
    # validation must treat that as "nothing requested" rather than rejecting it.
    values = _material_values()
    values[ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS)] = (5, {})
    service, flow = _service(values)
    service.capability_client = _CapabilityClient({
        'kb-a': {
            'split_rules': [{'id': 'block', 'name': '段落'}],
            'layout_types': [{'id': 'text', 'name': '文本'}],
        },
    })
    source = ArtifactRef(ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG), 3)
    selection = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS), 4)
    chunks = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS), 5)

    asyncio.run(service.apply_material_scan_config('thr-1', {
        'request_id': 'untouched-candidates',
        'expected_revision': _revision(source, selection, chunks),
        'changes': {'target_case_count': 7},
    }))

    assert len(flow.commits) == 1
    values_by_id = {write.key.artifact_id: write.value for write in flow.commits[0].writes}
    assert values_by_id[A.CORPUS_SOURCE_CONFIG]['target_case_count'] == 7
    # Untouched candidate configuration must not be rewritten.
    assert A.DATASET_BUILD_CHUNKS_PARAMS not in values_by_id


def test_apply_material_chunk_selection_preserves_quota_and_uses_document_snapshot_cas() -> None:
    service, flow = _service(_material_values())
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)
    candidates = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES), 7)

    asyncio.run(service.apply_material_chunk_selection('thr-1', {
        'request_id': 'selection-1',
        'expected_revision': _revision(docs, candidates),
        'changes': {'chunk_selection_changes': [
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': False},
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-2', 'selected': True},
        ]},
    }))

    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-materials-selection:selection-1'
    assert commit.expected_heads == {
        candidates.key: candidates,
        ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS): ArtifactRef(
            ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS), 8,
        ),
        ArtifactKey.partition(A.DATASET_CHUNK_REQUEST, 'chunk-2'): None,
    }
    assert [row['selected'] for row in commit.writes[0].value['chunks']] == [False, True]


def test_apply_material_chunk_selection_replaces_chunk_request_partitions_atomically() -> None:
    service, flow = _service(_material_values())
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)
    candidates = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES), 7)

    asyncio.run(service.apply_material_chunk_selection('thr-1', {
        'request_id': 'selection-replace',
        'expected_revision': _revision(docs, candidates),
        'changes': {'chunk_selection_changes': [
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': False},
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-2', 'selected': True},
        ]},
    }))

    commit = flow.commits[0]
    candidates_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)
    requests_key = ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS)
    new_request_key = ArtifactKey.partition(A.DATASET_CHUNK_REQUEST, 'chunk-2')
    assert [write.key for write in commit.writes] == [candidates_key, requests_key, new_request_key]
    assert commit.writes[1].value == PartitionSet(('chunk-2',))
    assert commit.writes[2].value == {'partition_key': 'chunk-2'}
    assert commit.expected_heads == {
        candidates_key: candidates,
        requests_key: ArtifactRef(requests_key, 8),
        new_request_key: None,
    }


def test_apply_material_chunk_selection_reuses_historical_partition_head_when_reselecting_chunk() -> None:
    values = _material_values()
    chunk_2_key = ArtifactKey.partition(A.DATASET_CHUNK_REQUEST, 'chunk-2')
    # The chunk was selected in an earlier topology, then removed. Its artifact
    # record remains in history even though the current PartitionSet excludes it.
    values[chunk_2_key] = (3, {'partition_key': 'chunk-2'})
    service, flow = _service(values)
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)
    candidates = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES), 7)

    asyncio.run(service.apply_material_chunk_selection('thr-1', {
        'request_id': 'selection-reselect',
        'expected_revision': _revision(docs, candidates),
        'changes': {'chunk_selection_changes': [
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': False},
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-2', 'selected': True},
        ]},
    }))

    commit = flow.commits[0]
    assert commit.expected_heads[chunk_2_key] == ArtifactRef(chunk_2_key, 3)


def test_apply_material_chunk_selection_resumes_a_paused_thread() -> None:
    service, flow = _service(_material_values())
    flow.status = 'paused'
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)
    candidates = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES), 7)

    asyncio.run(service.apply_material_chunk_selection('thr-1', {
        'request_id': 'selection-resume',
        'expected_revision': _revision(docs, candidates),
        'changes': {'chunk_selection_changes': [
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': False},
            {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-2', 'selected': True},
        ]},
    }))

    assert flow.resume_calls == ['thr-1']


def test_apply_material_chunk_selection_rejects_duplicated_selected_chunk_ids() -> None:
    values = _material_values()
    candidates_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)
    candidates = values[candidates_key][1]
    assert isinstance(candidates, dict)
    candidates['chunks'] = [
        {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': True, 'group': 'block'},
        {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': True, 'group': 'block'},
    ]
    candidates['quotas'] = [{'kb_id': 'kb-a', 'doc_id': 'doc-1', 'group': 'block', 'required': 2}]
    service, _ = _service(values)
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)

    with pytest.raises(ServiceError, match='selected chunk ids are duplicated'):
        asyncio.run(service.apply_material_chunk_selection('thr-1', {
            'request_id': 'selection-duplicate',
            'expected_revision': _revision(docs, ArtifactRef(candidates_key, 7)),
            'changes': {'chunk_selection_changes': [
                {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'chunk-1', 'selected': True},
            ]},
        }))


def test_apply_topic_names_changes_only_names_and_keeps_topic_discovery_out_of_the_commit() -> None:
    topic_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
    topic_ref = ArtifactRef(topic_key, 8)
    service, flow = _service({topic_key: (8, {'topics': [
        {'topic_id': 'topic-1', 'name': 'old', 'question_type': 'precision', 'chunk_ids': ['c-1'], 'chunk_count': 1},
        {'topic_id': 'topic-2', 'name': 'unchanged', 'question_type': 'reasoning', 'chunk_ids': ['c-2'], 'chunk_count': 1},
    ]})})

    asyncio.run(service.apply_topic_names('thr-1', {
        'request_id': 'topic-1', 'expected_revision': _revision(topic_ref),
        'changes': [{'topic_id': 'topic-1', 'name': 'new'}],
    }))

    commit = flow.commits[0]
    assert commit.commit_id == 'dataset-topic-names:topic-1'
    assert commit.expected_heads == {topic_key: topic_ref}
    assert commit.writes[0].key == topic_key
    assert commit.writes[0].value['topics'] == [
        {'topic_id': 'topic-1', 'name': 'new', 'question_type': 'precision', 'chunk_ids': ['c-1'], 'chunk_count': 1},
        {'topic_id': 'topic-2', 'name': 'unchanged', 'question_type': 'reasoning', 'chunk_ids': ['c-2'], 'chunk_count': 1},
    ]


def test_apply_services_return_404_for_missing_document_chunk_or_topic() -> None:
    service, _ = _service(_material_values())
    source = ArtifactRef(ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG), 3)
    selection = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS), 4)
    params = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS), 5)
    docs = ArtifactRef(ArtifactKey.scalar(A.DATASET_SELECTED_DOCS), 6)
    candidates = ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES), 7)

    with pytest.raises(ServiceError) as document_error:
        asyncio.run(service.apply_material_scan_config('thr-1', {
            'request_id': 'missing-document', 'expected_revision': _revision(source, selection, params),
            'changes': {'documents': [{'knowledge_base_id': 'kb-a', 'document_id': 'missing', 'included': False}]},
        }))
    assert document_error.value.status_code == 404

    with pytest.raises(ServiceError) as chunk_error:
        asyncio.run(service.apply_material_chunk_selection('thr-1', {
            'request_id': 'missing-chunk', 'expected_revision': _revision(docs, candidates),
            'changes': {'chunk_selection_changes': [
                {'knowledge_base_id': 'kb-a', 'document_id': 'doc-1', 'chunk_id': 'missing', 'selected': False},
            ]},
        }))
    assert chunk_error.value.status_code == 404

    topic_key = ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST)
    topic_ref = ArtifactRef(topic_key, 1)
    topic_service, _ = _service({topic_key: (1, {'topics': []})})
    with pytest.raises(ServiceError) as topic_error:
        asyncio.run(topic_service.apply_topic_names('thr-1', {
            'request_id': 'missing-topic', 'expected_revision': _revision(topic_ref),
            'changes': [{'topic_id': 'missing', 'name': 'new'}],
        }))
    assert topic_error.value.status_code == 404


def _material_scan_keys() -> tuple[ArtifactKey, ArtifactKey, ArtifactKey]:
    return (
        ArtifactKey.scalar(A.CORPUS_SOURCE_CONFIG),
        ArtifactKey.scalar(A.DATASET_SELECT_DOCS_PARAMS),
        ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_PARAMS),
    )


async def _open_material_scan_flow(tmp_path) -> ArtifactFlow:
    definition = FlowDefinition(
        (
            dataset_module.import_cases_operation,
            dataset_module.select_docs_operation,
            dataset_module.build_chunk_candidates_operation,
        ),
        (FlowStage('materials', ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)),),
    )
    flow = await ArtifactFlow.open(tmp_path / 'runtime-scan', definition)
    source_key, selection_key, chunks_key = _material_scan_keys()
    await flow.create('thr-1', ArtifactCommit(
        'seed:thr-1',
        'user:create',
        (
            ArtifactDraft(source_key, {
                'kb_id': ['kb-a'], 'knowledge_base_names': {'kb-a': 'A'},
                'csv_data': [], 'target_case_count': 3,
            }),
            ArtifactDraft(selection_key, {
                'knowledge_bases': [{'kb_id': 'kb-a', 'included': True}],
                'excluded_docs': [],
            }),
            ArtifactDraft(chunks_key, {}),
        ),
        {source_key: None, selection_key: None, chunks_key: None},
    ))
    return flow


def _service_with_flow(flow: ArtifactFlow) -> EvoService:
    service = object.__new__(EvoService)
    service.flow = flow
    service.capability_client = None

    async def _continue(_thread_id: str) -> None:
        return None

    service._continue_automatic = _continue  # type: ignore[method-assign]
    return service


def test_content_commit_is_rejected_by_structure_commit_and_accepted_by_commit_values(tmp_path) -> None:
    async def run() -> None:
        flow = await _open_material_scan_flow(tmp_path)
        try:
            source_key, selection_key, chunks_key = _material_scan_keys()
            source = ArtifactRef(source_key, 1)
            selection = ArtifactRef(selection_key, 1)
            chunks = ArtifactRef(chunks_key, 1)
            next_source = {
                'kb_id': ['kb-a'], 'knowledge_base_names': {'kb-a': 'A'},
                'csv_data': [], 'target_case_count': 7, 'min_case_count': 7,
            }
            content = ArtifactCommit(
                'dataset-materials-scan:case-count',
                'user:dataset-materials-scan',
                (ArtifactDraft(source_key, next_source),),
                {source_key: source, selection_key: selection, chunks_key: chunks},
            )
            with pytest.raises(DefinitionError, match='reserved for atomic case structure changes'):
                await flow.commit('thr-1', content)

            await flow.commit_values('thr-1', content)
            source_head = await flow.head('thr-1', source_key)
            selection_head = await flow.head('thr-1', selection_key)
            chunks_head = await flow.head('thr-1', chunks_key)
            assert source_head is not None and source_head.ref.version == 2
            assert selection_head is not None and selection_head.ref.version == 1
            assert chunks_head is not None and chunks_head.ref.version == 1
            assert (await flow.read('thr-1', source_head.ref))['target_case_count'] == 7
        finally:
            await flow.close()

    asyncio.run(run())


def test_atomic_material_selection_replaces_request_topology_in_real_flow(tmp_path) -> None:
    async def run() -> None:
        definition = FlowDefinition(
            (dataset_module.build_chunk_candidates_operation,),
            (FlowStage('materials', ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)),),
        )
        flow = await ArtifactFlow.open(tmp_path / 'runtime-selection', definition)
        candidates_key = ArtifactKey.scalar(A.DATASET_BUILD_CHUNK_CANDIDATES)
        requests_key = ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS)
        old_request_key = ArtifactKey.partition(A.DATASET_CHUNK_REQUEST, 'chunk-1')
        new_request_key = ArtifactKey.partition(A.DATASET_CHUNK_REQUEST, 'chunk-2')
        initial_candidates = {
            'chunks': [{'chunk_id': 'chunk-1', 'selected': True}],
            'quotas': [],
        }
        next_candidates = {
            'chunks': [{'chunk_id': 'chunk-2', 'selected': True}],
            'quotas': [],
        }
        try:
            await flow.create('thr-1', ArtifactCommit(
                'seed:selection',
                'user:create',
                (
                    ArtifactDraft(candidates_key, initial_candidates),
                    ArtifactDraft(requests_key, PartitionSet(('chunk-1',))),
                    ArtifactDraft(old_request_key, {'partition_key': 'chunk-1'}),
                ),
                {candidates_key: None, requests_key: None, old_request_key: None},
            ))
            await flow.commit_structure_with_values('thr-1', ArtifactCommit(
                'dataset-materials-selection:replace',
                'user:dataset-materials-selection',
                (
                    ArtifactDraft(candidates_key, next_candidates),
                    ArtifactDraft(requests_key, PartitionSet(('chunk-2',))),
                    ArtifactDraft(new_request_key, {'partition_key': 'chunk-2'}),
                ),
                {
                    candidates_key: ArtifactRef(candidates_key, 1),
                    requests_key: ArtifactRef(requests_key, 1),
                    new_request_key: None,
                },
            ), value_keys=(candidates_key,))

            requests = await flow.head('thr-1', requests_key)
            candidates = await flow.head('thr-1', candidates_key)
            new_request = await flow.head('thr-1', new_request_key)
            assert requests is not None and requests.ref.version == 2
            assert candidates is not None and candidates.ref.version == 2
            assert new_request is not None and new_request.ref.version == 1
            assert await flow.read('thr-1', requests.ref) == PartitionSet(('chunk-2',))

            # A removed partition's artifact remains in history. Re-adding the
            # same partition must atomically reactivate it against that head.
            await flow.commit_structure_with_values('thr-1', ArtifactCommit(
                'dataset-materials-selection:reselect',
                'user:dataset-materials-selection',
                (
                    ArtifactDraft(candidates_key, initial_candidates),
                    ArtifactDraft(requests_key, PartitionSet(('chunk-1',))),
                    ArtifactDraft(old_request_key, {'partition_key': 'chunk-1'}),
                ),
                {
                    candidates_key: ArtifactRef(candidates_key, 2),
                    requests_key: ArtifactRef(requests_key, 2),
                    old_request_key: ArtifactRef(old_request_key, 1),
                },
            ), value_keys=(candidates_key,))

            reselected_requests = await flow.head('thr-1', requests_key)
            reselected_request = await flow.head('thr-1', old_request_key)
            assert reselected_requests is not None and reselected_requests.ref.version == 3
            assert reselected_request is not None and reselected_request.ref.version == 2
            assert await flow.read('thr-1', reselected_requests.ref) == PartitionSet(('chunk-1',))
        finally:
            await flow.close()

    asyncio.run(run())


def test_apply_material_scan_config_commits_case_count_through_real_flow(tmp_path) -> None:
    async def run() -> None:
        flow = await _open_material_scan_flow(tmp_path)
        try:
            service = _service_with_flow(flow)
            source_key, selection_key, chunks_key = _material_scan_keys()
            source = ArtifactRef(source_key, 1)
            selection = ArtifactRef(selection_key, 1)
            chunks = ArtifactRef(chunks_key, 1)
            result = await service.apply_material_scan_config('thr-1', {
                'request_id': 'case-count-only',
                'expected_revision': _revision(source, selection, chunks),
                'changes': {'target_case_count': 7},
            })
            assert result['status'] == 'applied'
            source_head = await flow.head('thr-1', source_key)
            selection_head = await flow.head('thr-1', selection_key)
            chunks_head = await flow.head('thr-1', chunks_key)
            assert source_head is not None and source_head.ref.version == 2
            assert selection_head is not None and selection_head.ref.version == 1
            assert chunks_head is not None and chunks_head.ref.version == 1

            await flow.update_artifacts(
                'thr-1',
                (ArtifactUpdate(selection, {
                    'knowledge_bases': [{'kb_id': 'kb-a', 'included': False}],
                    'excluded_docs': [],
                }),),
                request_id='bump-selection',
            )
            with pytest.raises(ServiceError) as error:
                await service.apply_material_scan_config('thr-1', {
                    'request_id': 'stale-sibling',
                    'expected_revision': _revision(source_head.ref, selection, chunks),
                    'changes': {'target_case_count': 8},
                })
            assert error.value.status_code == 409
            assert 'stale' in str(error.value)
        finally:
            await flow.close()

    asyncio.run(run())
