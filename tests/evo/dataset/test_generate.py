import json
from types import SimpleNamespace

import pytest

from evo.operations.dataset.generate import generate, generate_manifest


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
        'run_config': run_config if run_config is not None else {'llm_config': {'evo_llm': {'model': 'generate-test'}}},
    }


def _response(**overrides):
    value = {
        'question': 'What is the target?',
        'answer': 'The target is defined in the references.',
        'grading_guidance': 'Assess whether the answer identifies the reference-grounded target definition.',
    }
    value.update(overrides)
    return value


def _generate(*, qaplan_spec=None, case_id='case_0001', response=None, run_config=None, captured=None):
    def complete(prompt):
        if captured is not None:
            captured.append(prompt)
        if isinstance(response, Exception):
            raise response
        return response

    return generate(
        _context(case_id),
        _inputs(qaplan_spec=qaplan_spec, run_config=run_config),
        llm_complete=complete,
    )['case']


def test_generate_prompts_for_three_field_output_and_short_reference_aliases():
    prompts = []

    _generate(response=json.dumps(_response()), captured=prompts)

    assert len(prompts) == 1
    assert 'Generate one grounded QA from the given materials.' in prompts[0]
    assert '<reference id="ref_1">' in prompts[0]
    assert '<reference id="ref_2">' in prompts[0]
    assert prompts[0].index('你将基于给定的 topic') < prompts[0].index('Generate one grounded QA from the given materials.')
    assert prompts[0].index('Generate one grounded QA from the given materials.') < prompts[0].index('Topic: service level')
    assert prompts[0].index('full reference text 1') < prompts[0].index('full reference text 2')
    assert 'question' in prompts[0]
    assert 'answer' in prompts[0]
    assert 'grading_guidance' in prompts[0]
    assert 'key_points' not in prompts[0]
    assert 'forbidden_claims' not in prompts[0]
    assert 'evidence_reference_ids' not in prompts[0]


def test_generate_returns_base_case_with_real_kb_references():
    case = _generate(response=json.dumps(_response()))

    assert case == {
        'id': 'case_0001',
        'question_type': 'precision',
        'difficulty': 'medium',
        'question': 'What is the target?',
        'answer': 'The target is defined in the references.',
        'grading_guidance': 'Assess whether the answer identifies the reference-grounded target definition.',
        'reference_context': {
            'chunk-1': 'full reference text 1',
            'chunk-2': 'full reference text 2',
        },
        'reference_chunk_ids': ['chunk-1', 'chunk-2'],
        'reference_doc_ids': ['doc-1', 'doc-2'],
        'source_preparation': {'kb_id': 'kb-1'},
    }


def test_generate_retries_once_for_invalid_three_field_output():
    prompts = []
    responses = iter((json.dumps(_response(grading_guidance='')), json.dumps(_response())))

    def complete(prompt):
        prompts.append(prompt)
        return next(responses)

    case = generate(_context(), _inputs(), llm_complete=complete)['case']

    assert case['id'] == 'case_0001'
    assert len(prompts) == 2
    assert 'generated grading_guidance' in prompts[1]
    assert 'key_points' not in prompts[1]


@pytest.mark.parametrize(
    'response',
    [
        'not json', '[]', '{}',
        '{"question":"question","answer":"answer"}',
        '{"question":"question","answer":"answer","grading_guidance":""}',
        '{"question":"question","answer":[],"grading_guidance":"guide"}',
    ],
)
def test_generate_rejects_invalid_model_output(response):
    with pytest.raises(ValueError):
        _generate(response=response)


@pytest.mark.parametrize(
    'qaplan_spec',
    [
        _qaplan_spec(id='case_0002'), _qaplan_spec(source={}), _qaplan_spec(references=[]),
        _qaplan_spec(references=[{'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'text': ''}]),
    ],
)
def test_generate_rejects_invalid_qaplan_spec(qaplan_spec):
    with pytest.raises(ValueError):
        _generate(qaplan_spec=qaplan_spec, response=json.dumps(_response()))


def test_generate_manifest_summarizes_base_cases_without_enhancement_fields():
    result = generate_manifest(None, {'cases': (
        {
            'id': 'case_0001',
            'question_type': 'precision',
            'difficulty': 'easy',
            'reference_chunk_ids': ['chunk-1'],
        },
        {
            'id': 'case_0002',
            'question_type': 'reasoning',
            'difficulty': 'medium',
            'reference_chunk_ids': ['chunk-2', 'chunk-3'],
        },
    )})

    assert result == {
        'generate_manifest': {
            'cases': [
                {'id': 'case_0001', 'question_type': 'precision', 'difficulty': 'easy', 'reference_count': 1},
                {'id': 'case_0002', 'question_type': 'reasoning', 'difficulty': 'medium', 'reference_count': 2},
            ],
            'stats': {
                'case_count': 2,
                'question_type_counts': {'precision': 1, 'reasoning': 1},
                'difficulty_counts': {'easy': 1, 'medium': 1, 'hard': 0},
            },
        },
    }


def test_generate_manifest_rejects_duplicate_case_ids():
    case = {
        'id': 'case_0001',
        'question_type': 'precision',
        'difficulty': 'easy',
        'reference_chunk_ids': ['chunk-1'],
    }

    with pytest.raises(ValueError, match='id values must be unique'):
        generate_manifest(None, {'cases': (case, case)})
