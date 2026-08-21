from __future__ import annotations

import asyncio
from types import SimpleNamespace

from evo import artifacts as A
from evo.artifact_runtime import ArtifactKey, ArtifactRecord, ArtifactRef
from evo.artifact_runtime.artifact import ArtifactSnapshot
from evo.artifact_runtime.planning import PlanReady
from evo.artifact_runtime.session import RunSession


def _topic(index: int, *, question_type: str = 'precision', chunks: int = 3) -> dict[str, object]:
    return {
        'topic_id': f'topic-{index}',
        'name': f'Topic {index}',
        'question_type': question_type,
        'chunk_ids': [f'chunk-{index}-{part}' for part in range(chunks)],
        'chunk_count': chunks,
    }


def _import_manifest(*, auto: int = 20) -> dict[str, object]:
    return {
        'stats': {
            'case_allocation': {
                'target_case_count': auto,
                'import_case_count': 0,
                'auto_case_count': auto,
                'assignments': {
                    f'case_{index:04d}': {'mode': 'generated'}
                    for index in range(1, auto + 1)
                },
            },
        },
    }


def _thr_a7f9a9d6_topics() -> list[dict[str, object]]:
    topics = [_topic(index, chunks=2) for index in range(1, 138)]
    topics.append(_topic(138, chunks=3))
    topics.extend(_topic(index, question_type='reasoning', chunks=2) for index in range(139, 163))
    return topics


class _Store:
    def __init__(self, values: dict[ArtifactRef, object]) -> None:
        self.values = values

    async def read_many(self, _run_id: str, refs: tuple[ArtifactRef, ...]) -> dict[ArtifactRef, object]:
        return {ref: self.values[ref] for ref in refs}


def test_generation_plan_gate_reads_manifest_values_from_mapping() -> None:
    async def run() -> None:
        session = object.__new__(RunSession)
        session.run_id = 'thr-a7f9a9d6'
        session._status = 'running'

        import_ref = ArtifactRef(ArtifactKey.scalar(A.DATASET_IMPORT_CASES_MANIFEST), 2)
        topic_ref = ArtifactRef(ArtifactKey.scalar(A.DATASET_TOPIC_MANIFEST), 1)
        params_ref = ArtifactRef(ArtifactKey.scalar(A.DATASET_QAPLAN_PLAN_PARAMS), 1)
        session._decision = PlanReady(ArtifactSnapshot(records={
            import_ref.key: ArtifactRecord(import_ref, 'operation:dataset.import_cases'),
            topic_ref.key: ArtifactRecord(topic_ref, 'operation:dataset.topic_manifest'),
            params_ref.key: ArtifactRecord(params_ref, 'user:create'),
        }), ())
        session._store = _Store({
            import_ref: _import_manifest(auto=20),
            topic_ref: {'topics': _thr_a7f9a9d6_topics()},
            params_ref: {},
        })

        paused = False

        async def _pause() -> None:
            nonlocal paused
            paused = True

        session._pause = _pause

        blocked = await session._pause_for_generation_plan_gate((
            SimpleNamespace(operation=SimpleNamespace(spec=SimpleNamespace(op_id='dataset.qaplan_plan'))),
        ))

        assert blocked is True
        assert paused is True

    asyncio.run(run())
