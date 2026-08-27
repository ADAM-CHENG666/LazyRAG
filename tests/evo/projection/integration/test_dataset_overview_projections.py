from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo import artifacts as A
from evo.artifact_flow import StageProgress, StageSnapshot
from evo.artifact_runtime import ArtifactKey, ArtifactRecord, ArtifactRef, PartitionSet
from evo.service.api import create_app
from evo.service.projections import ProjectionService


_MATERIAL_STAGE = 'dataset.material_preparation'
_TOPIC_STAGE = 'dataset.topic_discovery'
_CASE_STAGE = 'dataset.case_generation'


class _OverviewFlow:
    def __init__(self, values: dict[ArtifactKey, dict[int, object]], statuses: dict[str, str],
                 cases: dict[str, object] | None = None) -> None:
        self.values = values
        self.statuses = statuses
        self.cases = {} if cases is None else cases

    async def has_run(self, _: str) -> bool:
        return True

    async def head(self, _: str, key: ArtifactKey) -> ArtifactRecord | None:
        versions = self.values.get(key, {})
        return None if not versions else self._record(key, max(versions))

    async def record(self, _: str, ref: ArtifactRef) -> ArtifactRecord | None:
        return self._record(ref.key, ref.version) if ref.version in self.values.get(ref.key, {}) else None

    async def read(self, _: str, ref: ArtifactRef) -> object:
        return self.values[ref.key][ref.version]

    async def stage_snapshot(self, _: str, stage: str) -> StageSnapshot:
        return StageSnapshot(StageProgress(stage, ArtifactKey.scalar('test.result'), status=self.statuses[stage]))

    async def case_snapshot(self, _: str, case_id: str) -> object:
        return self.cases[case_id]

    @staticmethod
    def _record(key: ArtifactKey, version: int) -> ArtifactRecord:
        return ArtifactRecord(ArtifactRef(key, version), producer='test')


def _service(*, values: dict[ArtifactKey, dict[int, object]], statuses: dict[str, str],
             cases: dict[str, object] | None = None) -> ProjectionService:
    return ProjectionService(_OverviewFlow(values, statuses, cases), definition=None)


def _enable_dataset_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, 'STEPS', (_MATERIAL_STAGE, _TOPIC_STAGE))


def _enable_case_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A, 'STEPS', (_CASE_STAGE,))


def test_materials_overview_projects_current_manifest_and_stage_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(
        values={ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_MANIFEST): {4: {
            'source': {'case_counts': {'target': 100, 'imported': 20, 'automatic': 80}},
            'summary': {'chunk_counts': {'scanned': 800, 'effective': 600, 'selected': 240}},
            'warnings': ['chunk candidate capacity is short by 5; selected 240'],
        }}},
        statuses={_MATERIAL_STAGE: 'completed'},
    )

    result = asyncio.run(service.materials_overview('thr-1'))

    assert result == {
        'thread_id': 'thr-1',
        'revision': service._build_revision((ArtifactRef(ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_MANIFEST), 4),)),
        'status': 'completed',
        'case_plan': {'target': 100, 'imported': 20, 'automatic': 80},
        'chunks': {
            'scanned': 800, 'effective': 600, 'selected': 240,
            'effective_rate': 0.75, 'selection_rate': 0.4,
        },
        'warnings': ['chunk candidate capacity is short by 5; selected 240'],
    }


def test_materials_overview_returns_current_status_without_a_stable_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(values={}, statuses={_MATERIAL_STAGE: 'running'})

    assert asyncio.run(service.materials_overview('thr-1')) == {
        'thread_id': 'thr-1', 'revision': None, 'status': 'running',
        'case_plan': None, 'chunks': None, 'warnings': [],
    }


def test_materials_overview_projects_import_plan_before_chunk_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    key = ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST)
    service = _service(
        values={key: {2: {'stats': {'case_allocation': {
            'target_case_count': 2, 'import_case_count': 2, 'auto_case_count': 0,
            'assignments': {'external-1': {'mode': 'imported'}, 'external-2': {'mode': 'imported'}},
        }}}}},
        statuses={_MATERIAL_STAGE: 'running'},
    )

    result = asyncio.run(service.materials_overview('thr-1'))

    assert result['case_plan'] == {'target': 2, 'imported': 2, 'automatic': 0}
    assert result['chunks'] is None
    assert result['revision'] == service._build_revision((ArtifactRef(key, 2),))


def test_materials_overview_uses_null_rates_when_their_denominators_are_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(
        values={ArtifactKey.scalar(A.DATASET_BUILD_CHUNKS_MANIFEST): {1: {
            'source': {'case_counts': {'target': 0, 'imported': 0, 'automatic': 0}},
            'summary': {'chunk_counts': {'scanned': 0, 'effective': 0, 'selected': 0}},
            'warnings': [],
        }}},
        statuses={_MATERIAL_STAGE: 'completed'},
    )

    assert asyncio.run(service.materials_overview('thr-1'))['chunks'] == {
        'scanned': 0, 'effective': 0, 'selected': 0,
        'effective_rate': None, 'selection_rate': None,
    }


def test_topics_overview_projects_current_manifest_and_calculates_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(
        values={
            ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {3: {
                'stats': {
                    'total_topic_count': 30,
                    'question_types': {'precision': {'count': 18}, 'reasoning': {'count': 12}},
                },
            }},
            ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS): {1: PartitionSet(('chunk-1', 'chunk-2'))},
            ArtifactKey.scalar(A.DATASET_EMBEDDING_LABEL_REQUESTS): {1: PartitionSet(('cluster-1',))},
        },
        statuses={_TOPIC_STAGE: 'completed'},
    )

    result = asyncio.run(service.topics_overview('thr-1'))

    assert result == {
        'thread_id': 'thr-1',
        'revision': service._build_revision((ArtifactRef(ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST), 3),)),
        'status': 'completed',
        'total_topics': 30,
        'question_types': {
            'precision': {'count': 18, 'rate': 0.6},
            'reasoning': {'count': 12, 'rate': 0.4},
        },
        'stages': {
            'entities': {'status': 'completed', 'completed': 2, 'total': 2},
            'semantic': {'status': 'completed', 'completed': 1, 'total': 1},
            'topics': {'status': 'completed', 'completed': 30, 'total': 30},
        },
    }


def test_topics_overview_calibrates_stage_counts_when_manifest_exists_at_gate(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dataset_stages(monkeypatch)
    service = _service(
        values={
            ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {3: {
                'stats': {
                    'total_topic_count': 184,
                    'question_types': {'precision': {'count': 100}, 'reasoning': {'count': 84}},
                },
            }},
            ArtifactKey.scalar(A.DATASET_CHUNK_REQUESTS): {1: PartitionSet(tuple(f'chunk-{i}' for i in range(30)))},
            ArtifactKey.scalar(A.DATASET_EMBEDDING_LABEL_REQUESTS): {1: PartitionSet(('cluster-1', 'cluster-2'))},
        },
        statuses={_TOPIC_STAGE: 'paused'},
    )

    result = asyncio.run(service.topics_overview('thr-1'))

    assert result['status'] == 'paused'
    assert result['total_topics'] == 184
    assert result['stages'] == {
        'entities': {'status': 'completed', 'completed': 30, 'total': 30},
        'semantic': {'status': 'completed', 'completed': 2, 'total': 2},
        'topics': {'status': 'completed', 'completed': 184, 'total': 184},
    }


def test_topics_overview_keeps_a_valid_empty_manifest_distinct_from_no_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_dataset_stages(monkeypatch)
    empty = _service(
        values={ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {1: {
            'stats': {'total_topic_count': 0, 'question_types': {'precision': {'count': 0}, 'reasoning': {'count': 0}}},
        }}},
        statuses={_TOPIC_STAGE: 'completed'},
    )
    missing = _service(values={}, statuses={_TOPIC_STAGE: 'running'})

    assert asyncio.run(empty.topics_overview('thr-1'))['question_types'] == {
        'precision': {'count': 0, 'rate': None},
        'reasoning': {'count': 0, 'rate': None},
    }
    assert asyncio.run(missing.topics_overview('thr-1')) == {
        'thread_id': 'thr-1', 'revision': None, 'status': 'running',
        'total_topics': None, 'question_types': None,
        'stages': {
            'entities': {'status': 'pending', 'completed': 0, 'total': None},
            'semantic': {'status': 'pending', 'completed': 0, 'total': None},
            'topics': {'status': 'pending', 'completed': 0, 'total': None},
        },
    }


def test_cases_overview_projects_manifest_and_three_runtime_operation_counts(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_case_stage(monkeypatch)
    params_ref = ArtifactRef(ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS), 2)
    service = _service(
        values={
            params_ref.key: {2: {'lane_case_counts': {}}},
            ArtifactKey.scalar(A.DATASET_QAPLAN_MANIFEST): {4: {
                'stats': {
                    'target_case_count': 3,
                    'import_case_count': 1,
                    'auto_case_count': 2,
                    'planned_case_count': 2,
                },
                'lane_summaries': [
                    {'lane': 'precision_easy', 'question_type': 'precision', 'difficulty': 'easy',
                     'allocated_case_count': 1, 'eligible_topic_count': 1},
                    {'lane': 'precision_medium', 'question_type': 'precision', 'difficulty': 'medium',
                     'allocated_case_count': 0, 'eligible_topic_count': 0},
                    {'lane': 'precision_hard', 'question_type': 'precision', 'difficulty': 'hard',
                     'allocated_case_count': 0, 'eligible_topic_count': 0},
                    {'lane': 'reasoning_easy', 'question_type': 'reasoning', 'difficulty': 'easy',
                     'allocated_case_count': 0, 'eligible_topic_count': 0},
                    {'lane': 'reasoning_medium', 'question_type': 'reasoning', 'difficulty': 'medium',
                     'allocated_case_count': 0, 'eligible_topic_count': 0},
                    {'lane': 'reasoning_hard', 'question_type': 'reasoning', 'difficulty': 'hard',
                     'allocated_case_count': 1, 'eligible_topic_count': 1},
                ],
            }},
            ArtifactKey.scalar(A.EVAL_CASE_REQUESTS): {3: PartitionSet(('case-1', 'case-2', 'case-3'))},
            ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {5: {'topics': [
                {'topic_id': 'topic-p-e', 'name': 'PE', 'question_type': 'precision',
                 'chunk_ids': ['c-1'], 'chunk_count': 1},
                {'topic_id': 'topic-r-h', 'name': 'RH', 'question_type': 'reasoning',
                 'chunk_ids': ['c-2', 'c-3', 'c-4'], 'chunk_count': 3},
            ]}},
            ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST): {6: {
                'stats': {
                    'case_allocation': {
                        'target_case_count': 2,
                        'import_case_count': 0,
                        'auto_case_count': 2,
                        'assignments': {
                            'case-1': {'mode': 'generated'},
                            'case-2': {'mode': 'generated'},
                        },
                    },
                },
            }},
        },
        statuses={_CASE_STAGE: 'failed'},
        cases={
            'case-1': {'runtime': {'operations': [
                {'operation_id': 'dataset.qaplan_spec', 'status': 'succeeded'},
                {'operation_id': 'dataset.generate_case', 'status': 'succeeded'},
                {'operation_id': 'dataset.enhance_case', 'status': 'succeeded'},
            ]}},
            'case-2': {'runtime': {'operations': [
                {'operation_id': 'dataset.qaplan_spec', 'status': 'succeeded'},
                {'operation_id': 'dataset.generate_case', 'status': 'running'},
                {'operation_id': 'dataset.enhance_case', 'status': 'pending'},
            ]}},
            'case-3': {'runtime': {'operations': [
                {'operation_id': 'dataset.qaplan_spec', 'status': 'failed'},
                {'operation_id': 'dataset.generate_case', 'status': 'pending'},
                {'operation_id': 'dataset.enhance_case', 'status': 'pending'},
            ]}},
        },
    )

    assert asyncio.run(service.cases_overview('thr-1')) == {
        'thread_id': 'thr-1',
        'revision': service._build_revision((params_ref,)),
        'execution_revision': service._build_execution_revision({
            'case-1': {
                'dataset.qaplan_spec': 'completed',
                'dataset.generate_case': 'completed',
                'dataset.enhance_case': 'completed',
            },
            'case-2': {
                'dataset.qaplan_spec': 'completed',
                'dataset.generate_case': 'running',
                'dataset.enhance_case': 'pending',
            },
            'case-3': {
                'dataset.qaplan_spec': 'failed',
                'dataset.generate_case': 'pending',
                'dataset.enhance_case': 'pending',
            },
        }),
        'status': 'failed',
        'stages': {
            'plan': {
                'status': 'failed', 'completed': 2, 'total': 3,
                'status_counts': {'pending': 0, 'running': 0, 'completed': 2, 'failed': 1, 'canceled': 0},
            },
            'generate': {
                'status': 'running', 'completed': 1, 'total': 3,
                'status_counts': {'pending': 1, 'running': 1, 'completed': 1, 'failed': 0, 'canceled': 0},
            },
            'grading': {
                'status': 'pending', 'completed': 1, 'total': 3,
                'status_counts': {'pending': 2, 'running': 0, 'completed': 1, 'failed': 0, 'canceled': 0},
            },
        },
        'automatic_plan': {
            'total': 2,
            'question_types': {
                'precision': {
                    'total': 1,
                    'difficulties': {'easy': 1, 'medium': 0, 'hard': 0},
                    'capacities': {'easy': 1, 'medium': 0, 'hard': 0},
                },
                'reasoning': {
                    'total': 1,
                    'difficulties': {'easy': 0, 'medium': 0, 'hard': 1},
                    'capacities': {'easy': 1, 'medium': 1, 'hard': 1},
                },
            },
        },
    }


def test_cases_overview_projects_capacities_before_qaplan_manifest(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_case_stage(monkeypatch)
    params_ref = ArtifactRef(ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS), 1)
    service = _service(
        values={
            params_ref.key: {1: {}},
            ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {2: {'topics': [
                {'topic_id': 'topic-p-h', 'name': 'PH', 'question_type': 'precision',
                 'chunk_ids': ['c-1', 'c-2', 'c-3'], 'chunk_count': 3},
                {'topic_id': 'topic-r-h', 'name': 'RH', 'question_type': 'reasoning',
                 'chunk_ids': ['c-4', 'c-5', 'c-6'], 'chunk_count': 3},
            ]}},
            ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST): {3: {
                'stats': {
                    'case_allocation': {
                        'target_case_count': 2,
                        'import_case_count': 0,
                        'auto_case_count': 2,
                        'assignments': {
                            'case_0001': {'mode': 'generated'},
                            'case_0002': {'mode': 'generated'},
                        },
                    },
                },
            }},
        },
        statuses={_CASE_STAGE: 'paused'},
    )

    assert asyncio.run(service.cases_overview('thr-1')) == {
        'thread_id': 'thr-1',
        'revision': service._build_revision((params_ref,)),
        'execution_revision': service._build_execution_revision({}),
        'status': 'paused',
        'stages': {
            name: {'status': 'pending', 'completed': 0, 'total': 2, 'status_counts': {
                'pending': 2, 'running': 0, 'completed': 0, 'failed': 0, 'canceled': 0,
            }}
            for name in ('plan', 'generate', 'grading')
        },
        'automatic_plan': {
            'total': 2,
            'question_types': {
                'precision': {
                    'total': 2,
                    'difficulties': {'easy': 1, 'medium': 1, 'hard': 0},
                    'capacities': {'easy': 1, 'medium': 1, 'hard': 1},
                },
                'reasoning': {
                    'total': 0,
                    'difficulties': {'easy': 0, 'medium': 0, 'hard': 0},
                    'capacities': {'easy': 1, 'medium': 1, 'hard': 1},
                },
            },
        },
    }


def test_cases_overview_counts_imported_cases_as_complete_before_runtime_requests(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_case_stage(monkeypatch)
    params_key = ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS)
    import_key = ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST)
    service = _service(
        values={
            params_key: {1: {}},
            import_key: {2: {'stats': {'case_allocation': {
                'target_case_count': 2,
                'import_case_count': 2,
                'auto_case_count': 0,
                'assignments': {
                    'external-1': {'mode': 'imported', 'source_row_number': 1},
                    'external-2': {'mode': 'imported', 'source_row_number': 2},
                },
            }}, 'details': []}},
        },
        statuses={_CASE_STAGE: 'running'},
    )

    result = asyncio.run(service.cases_overview('thr-1'))

    assert all(stage['status'] == 'completed' for stage in result['stages'].values())
    assert all(stage['completed'] == 2 for stage in result['stages'].values())


def test_cases_overview_keeps_plan_revision_and_reports_pending_before_case_partitions_exist(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_case_stage(monkeypatch)
    params_ref = ArtifactRef(ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS), 1)
    service = _service(
        values={
            params_ref.key: {1: {'lane_case_counts': {}}},
            ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST): {2: {'topics': []}},
            ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST): {3: {
                'stats': {
                    'case_allocation': {
                        'target_case_count': 0,
                        'import_case_count': 0,
                        'auto_case_count': 0,
                        'assignments': {},
                    },
                },
            }},
        },
        statuses={_CASE_STAGE: 'pending'},
    )

    assert asyncio.run(service.cases_overview('thr-1')) == {
        'thread_id': 'thr-1',
        'revision': service._build_revision((params_ref,)),
        'execution_revision': service._build_execution_revision({}),
        'status': 'pending',
        'stages': {
            name: {'status': 'pending', 'completed': 0, 'total': 0, 'status_counts': {
                'pending': 0, 'running': 0, 'completed': 0, 'failed': 0, 'canceled': 0,
            }}
            for name in ('plan', 'generate', 'grading')
        },
        'automatic_plan': None,
    }


@pytest.mark.parametrize(('path', 'method'), [
    ('/threads/thr-1/dataset/materials/overview', 'materials_overview'),
    ('/threads/thr-1/dataset/topics/overview', 'topics_overview'),
    ('/threads/thr-1/dataset/cases/overview', 'cases_overview'),
])
def test_overview_handlers_delegate_without_query_parameters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                              path: str, method: str) -> None:
    calls: list[tuple[str, str]] = []

    class _Projections:
        async def materials_overview(self, thread_id: str) -> dict:
            calls.append(('materials_overview', thread_id))
            return {'thread_id': thread_id}

        async def topics_overview(self, thread_id: str) -> dict:
            calls.append(('topics_overview', thread_id))
            return {'thread_id': thread_id}

        async def cases_overview(self, thread_id: str) -> dict:
            calls.append(('cases_overview', thread_id))
            return {'thread_id': thread_id}

    class _Service:
        projections = _Projections()

        async def close(self) -> None:
            return None

    async def _open(_: Path) -> _Service:
        return _Service()

    monkeypatch.setattr('evo.service.api.EvoService.open', _open)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(path)
        unsupported = client.get(path, params={'page_size': '50'})

    assert response.status_code == 200
    assert response.json() == {'thread_id': 'thr-1'}
    assert unsupported.status_code == 422
    assert calls == [(method, 'thr-1')]
