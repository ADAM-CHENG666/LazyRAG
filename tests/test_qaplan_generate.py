import json
from types import SimpleNamespace

import pytest

from evo.operations.dataset.qaplan import qaplan_generate


def _reference(index):
    return {
        'chunk_id': f'chunk-{index}',
        'doc_id': f'doc-{index}',
        'text': f'full reference text {index}',
    }


def _qaplan_spec(**overrides):
    value = {
        'id': 'case_0001',
        'question_type': 'precision',
        'difficulty': 'medium',
        'instruction': 'Generate one grounded QA from the given materials.',
        'topic': 'service level',
        'source': {'kb_id': 'kb-1'},
        'qaplan': {
            'plan_item_id': 'qaplan_item_000001',
            'lane': 'entity_precision_medium',
            'cluster_id': 'entity_000001',
            'cluster_type': 'entity',
            'selection_round': 1,
        },
        'references': [_reference(1), _reference(2)],
    }
    value.update(overrides)
    return value


def _context(case_id='case_0001'):
    return SimpleNamespace(output_key_by_name={'case': SimpleNamespace(partition=case_id)})


def _inputs(qaplan_spec=None, run_config=None):
    return {
        'qaplan_spec': qaplan_spec if qaplan_spec is not None else _qaplan_spec(),
        'run_config': run_config if run_config is not None else {'llm_config': {'evo_llm': {'model': 'qaplan-test'}}},
    }


def _response():
    return {
        'question': 'What is the target?',
        'answer': 'The target is defined in the references.',
        'key_points': [
            {'statement': 'The target is explicitly defined.', 'evidence_reference_ids': ['ref_1']},
            {'statement': 'The definition comes from the references.', 'evidence_reference_ids': ['ref_2']},
        ],
        'grading_guidance': 'The answer must state the target definition without omitting either reference.',
        'forbidden_claims': ['The target is undefined in the references.'],
    }


def _generate(*, qaplan_spec=None, case_id='case_0001', response=None, run_config=None, captured=None):
    def complete(prompt):
        if captured is not None:
            captured.append(prompt)
        if isinstance(response, Exception):
            raise response
        return response

    return qaplan_generate(
        _context(case_id),
        _inputs(qaplan_spec=qaplan_spec, run_config=run_config),
        llm_complete=complete,
    )['case']


def test_qaplan_generate_prompts_for_structured_case_output_and_short_reference_aliases():
    prompts = []

    _generate(response=json.dumps(_response()), captured=prompts)

    assert len(prompts) == 1
    assert 'Generate one grounded QA from the given materials.' in prompts[0]
    assert '<reference id="ref_1">' in prompts[0]
    assert '<reference id="ref_2">' in prompts[0]
    assert prompts[0].index('full reference text 1') < prompts[0].index('full reference text 2')
    assert 'key_points' in prompts[0]
    assert 'grading_guidance' in prompts[0]
    assert 'forbidden_claims' in prompts[0]
    assert 'evidence_reference_ids' in prompts[0]


def test_qaplan_generate_returns_canonical_case_with_real_kb_references():
    case = _generate(response=json.dumps(_response()))

    assert case == {
        'id': 'case_0001',
        'question_type': 'precision',
        'difficulty': 'medium',
        'question': 'What is the target?',
        'answer': 'The target is defined in the references.',
        'key_points': [
            {'statement': 'The target is explicitly defined.', 'evidence_chunk_ids': ['chunk-1']},
            {'statement': 'The definition comes from the references.', 'evidence_chunk_ids': ['chunk-2']},
        ],
        'grading_guidance': 'The answer must state the target definition without omitting either reference.',
        'forbidden_claims': ['The target is undefined in the references.'],
        'reference_context': {
            'chunk-1': 'full reference text 1',
            'chunk-2': 'full reference text 2',
        },
        'reference_chunk_ids': ['chunk-1', 'chunk-2'],
        'reference_doc_ids': ['doc-1', 'doc-2'],
        'source_preparation': {'kb_id': 'kb-1'},
    }


@pytest.mark.parametrize(
    'response',
    [
        'not json', '[]', '{}',
        '{"question":"question","answer":"answer","key_points":[{"statement":"one","evidence_reference_ids":["ref_1"]}],"grading_guidance":"guide"}',
        '{"question":"question","answer":"answer","key_points":[{"statement":"one","evidence_reference_ids":["ref_1"]}],"grading_guidance":"guide","forbidden_claims":[]}',
        '{"question":"question","answer":"answer","key_points":[{"statement":"one","evidence_reference_ids":["ref_1"]},{"statement":"two","evidence_reference_ids":["ref_2"]},{"statement":"three","evidence_reference_ids":["ref_1"]},{"statement":"four","evidence_reference_ids":["ref_1"]},{"statement":"five","evidence_reference_ids":["ref_1"]},{"statement":"six","evidence_reference_ids":["ref_1"]}],"grading_guidance":"guide","forbidden_claims":[]}',
        '{"question":"question","answer":"answer","key_points":[{"statement":"one","evidence_reference_ids":["ref_1"]},{"statement":"two","evidence_reference_ids":["ref_2"]}],"grading_guidance":"","forbidden_claims":[]}',
        '{"question":"question","answer":"answer","key_points":[{"statement":"one","evidence_reference_ids":["ref_3"]},{"statement":"two","evidence_reference_ids":["ref_2"]}],"grading_guidance":"guide","forbidden_claims":[]}',
        '{"question":"question","answer":"answer","key_points":[{"statement":"one","evidence_reference_ids":["ref_1"]}],"grading_guidance":"guide","forbidden_claims":[]}',
        '{"question":"question","answer":"answer","key_points":[{"statement":"one","evidence_reference_ids":["ref_1"]},{"statement":"two","evidence_reference_ids":["ref_2"]}],"grading_guidance":"guide","forbidden_claims":["a","b","c","d"]}',
    ],
)
def test_qaplan_generate_rejects_invalid_model_output(response):
    with pytest.raises(ValueError):
        _generate(response=response)


@pytest.mark.parametrize(
    'qaplan_spec',
    [
        _qaplan_spec(id='case_0002'), _qaplan_spec(source={}), _qaplan_spec(references=[]),
        _qaplan_spec(references=[{'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'text': ''}]),
    ],
)
def test_qaplan_generate_rejects_invalid_qaplan_spec(qaplan_spec):
    with pytest.raises(ValueError):
        _generate(qaplan_spec=qaplan_spec, response=json.dumps(_response()))
