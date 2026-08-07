from __future__ import annotations

import pytest

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef
from evo.message_intent.config_guard import ConfigValidationError, validate_config_patch
from evo.message_intent.schemas import ConfigPatchAction
from evo.operations.dataset.kb_loader import load_corpus
from evo.service.runtime_port import RuntimePort
from evo.service.threads import _seed


def _inputs(count: int = 3) -> dict[str, object]:
    return {
        'kb_id': ['kb-1'],
        'csv_data': [],
        'router_chat_url': 'http://router-chat',
        'router_admin_url': 'http://router-admin',
        'algorithm_id': 'algo-1',
        'num_case': count,
        'case_deadline_seconds': 300.0,
    }


def test_thread_seed_keeps_target_case_count_only_in_source_config(tmp_path):
    seed = _seed('thr-1', 'interactive', 'title', _inputs(), {'llm': {}, 'evo_llm': {}})

    assert seed['run_config'] == {
        'thread_id': 'thr-1', 'mode': 'interactive', 'title': 'title', 'llm_config': {'llm': {}, 'evo_llm': {}},
    }
    assert seed['source_config']['target_case_count'] == 3
    assert 'min_case_count' not in seed['source_config']

    runtime = RuntimePort(tmp_path)
    runtime.seed('thr-1', seed, 'request-1')

    assert runtime.target_case_count('thr-1') == 3
    assert runtime.source_config('thr-1') == seed['source_config']


def test_source_config_patch_allows_target_change_and_run_config_rejects_case_count():
    source = {'kb_id': ['kb-1'], 'csv_data': [], 'target_case_count': 3}
    source_action = ConfigPatchAction(kind='config_patch', target='source_config',
                                      pointer='/target_case_count', value=5)
    ref = ArtifactRef(ArtifactKey.of(C.CORPUS_SOURCE_CONFIG), 1)

    _, pointer, value = validate_config_patch('thr-1', source_action, ref, source)

    assert (pointer, value) == ('/target_case_count', 5)
    run = {'thread_id': 'thr-1', 'mode': 'interactive', 'title': '', 'llm_config': {'llm': {}}}
    run_action = ConfigPatchAction(kind='config_patch', target='run_config', pointer='/num_case', value=5)
    with pytest.raises(ConfigValidationError, match='path does not exist'):
        validate_config_patch('thr-1', run_action, ArtifactRef(ArtifactKey.of(C.RUN_CONFIG), 1), run)


def test_corpus_loader_uses_target_case_count_even_if_legacy_minimum_is_present():
    report = load_corpus({
        'kb_id': ['kb-1'],
        'target_case_count': 2,
        'min_case_count': 9,
        'source_units': [{'doc_id': 'doc-1', 'chunk_id': 'chunk-1', 'content': 'source text'}],
    }, ('case_0001', 'case_0002'))

    assert report['stats']['target_case_count'] == 2
    assert 'min_case_count' not in report['stats']
