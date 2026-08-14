from __future__ import annotations

import asyncio
import importlib
from evo import artifacts as A
from evo.artifact_runtime import OperationContext


dataset_module = importlib.import_module('evo.operations.dataset.operations')


def test_material_parameter_artifacts_are_declared_runtime_seeds() -> None:
    assert A.DATASET_SELECT_DOCS_PARAMS in A.SEEDS
    assert A.DATASET_BUILD_CHUNKS_PARAMS in A.SEEDS


def test_select_docs_operation_declares_and_forwards_its_parameter_artifact(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_select_docs(_ctx, inputs):
        captured.update(inputs)
        return {'selected_docs': {'documents': []}}

    monkeypatch.setattr(dataset_module, 'select_docs', fake_select_docs)
    params = {
        'knowledge_bases': [{'kb_id': 'kb-a', 'included': False}],
        'excluded_docs': [{'kb_id': 'kb-a', 'doc_id': 'doc-1'}],
    }

    async def run() -> None:
        await dataset_module.select_docs_operation(
            OperationContext('run-1', 'attempt-1'),
            {'kb_ids': ['kb-a'], 'target_case_count': 1},
            {'documents': []},
            params,
        )

    asyncio.run(run())

    assert dataset_module.select_docs_operation.spec.inputs['select_docs_params'].artifact_id == (
        A.DATASET_SELECT_DOCS_PARAMS
    )
    assert captured['select_docs_params'] == params


def test_chunk_candidates_operation_declares_and_forwards_its_parameter_artifact(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_chunk_candidates(_ctx, inputs):
        captured.update(inputs)
        return {'build_chunk_candidates': {'chunks': []}}

    monkeypatch.setattr(dataset_module, 'build_chunk_candidates', fake_build_chunk_candidates)
    params = {
        'groups': ['section'],
        'allowed_types': ['text'],
        'max_chunk_chars': 1024,
    }

    async def run() -> None:
        await dataset_module.build_chunk_candidates_operation(
            OperationContext('run-1', 'attempt-1'),
            {'documents': []},
            {'imported_docs': []},
            params,
        )

    asyncio.run(run())

    assert dataset_module.build_chunk_candidates_operation.spec.inputs['build_chunks_params'].artifact_id == (
        A.DATASET_BUILD_CHUNKS_PARAMS
    )
    assert captured['build_chunks_params'] == params


def test_material_parameter_dependencies_have_the_expected_recompute_boundary() -> None:
    """The operation graph is the runtime invalidation boundary for parameter updates."""
    select_inputs = dataset_module.select_docs_operation.spec.inputs
    candidate_inputs = dataset_module.build_chunk_candidates_operation.spec.inputs

    assert {binding.artifact_id for binding in select_inputs.values()} == {
        A.CORPUS_SOURCE_CONFIG,
        A.DATASET_IMPORT_CASES_MANIFEST,
        A.DATASET_SELECT_DOCS_PARAMS,
    }
    assert {binding.artifact_id for binding in candidate_inputs.values()} == {
        A.DATASET_SELECTED_DOCS,
        A.DATASET_IMPORT_CASES_MANIFEST,
        A.DATASET_BUILD_CHUNKS_PARAMS,
    }
