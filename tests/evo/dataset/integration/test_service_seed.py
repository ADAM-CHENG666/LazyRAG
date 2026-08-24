from __future__ import annotations

import pytest

from evo import artifacts as A
from evo.service.contracts import ThreadCreate
from evo.service.core import _seed_values


def _request(*, count: int = 3, kb_ids: list[str] | None = None,
             csv_data: list[dict[str, str]] | None = None,
             knowledge_base_names: dict[str, str] | None = None) -> ThreadCreate:
    return ThreadCreate.model_validate({
        'title': 'dataset test',
        'inputs': {
            'kb_id': ['kb-1'] if kb_ids is None else kb_ids,
            'csv_data': csv_data or [],
            'knowledge_base_names': knowledge_base_names or {},
            'router_chat_url': 'http://router-chat',
            'router_admin_url': 'http://router-admin',
            'algorithm_id': 'algo-1',
            'num_case': count,
        },
        'llm_config': {'llm': {}, 'evo_llm': {}, 'embed_main': {}},
    })


def test_service_seed_contains_every_runtime_seed_and_dataset_defaults() -> None:
    seed = _seed_values('thr-1', _request())

    assert set(seed) == set(A.SEEDS)
    assert seed[A.RUN_CONFIG]['num_case'] == 3
    assert seed[A.CORPUS_SOURCE_CONFIG] == {
        'kb_id': ['kb-1'],
        'csv_data': [],
        'knowledge_base_names': {},
        'target_case_count': 3,
        'min_case_count': 3,
    }
    assert seed[A.DATASET_QAPLAN_PLAN_PARAMS] == {}


def test_service_seed_preserves_csv_source_mapping_for_dataset_normalization() -> None:
    seed = _seed_values('thr-1', _request(csv_data=[{'kb-2': '/tmp/cases.csv'}]))

    assert seed[A.CORPUS_SOURCE_CONFIG]['csv_data'] == [
        {'kb-2': '/tmp/cases.csv'},
    ]
    assert seed['dataset.select_docs_params'] == {
        'knowledge_bases': [
            {'kb_id': 'kb-1', 'included': True},
            {'kb_id': 'kb-2', 'included': True},
        ],
        'excluded_docs': [],
    }
    assert seed['dataset.build_chunks_params'] == {}


def test_service_seed_preserves_core_authoritative_knowledge_base_names() -> None:
    seed = _seed_values('thr-1', _request(
        kb_ids=['kb-1', 'kb-2'],
        knowledge_base_names={'kb-1': '产品知识库', 'kb-2': '研究资料库'},
    ))

    assert seed[A.CORPUS_SOURCE_CONFIG]['knowledge_base_names'] == {
        'kb-1': '产品知识库', 'kb-2': '研究资料库',
    }


def test_service_seed_initializes_every_selected_knowledge_base_for_materials() -> None:
    seed = _seed_values('thr-1', _request(
        kb_ids=['kb-2', 'kb-1'],
        knowledge_base_names={'kb-1': '产品知识库', 'kb-2': '研究资料库'},
    ))

    assert seed['dataset.select_docs_params']['knowledge_bases'] == [
        {'kb_id': 'kb-2', 'included': True},
        {'kb_id': 'kb-1', 'included': True},
    ]


def test_thread_create_rejects_missing_dataset_sources() -> None:
    with pytest.raises(ValueError, match='inputs.kb_id or inputs.csv_data is required'):
        _request(kb_ids=[], csv_data=[])
