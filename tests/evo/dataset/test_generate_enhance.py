import json
from types import SimpleNamespace

import pytest

from evo.operations.dataset.generate_enhance import generate_enhance


def _case(**overrides):
    value = {
        'question': 'What does the warranty cover?',
        'answer': 'The warranty covers battery defects under stated conditions.',
        'grading_guidance': 'Assess the coverage conclusion and its conditions.',
        'reference_context': {
            'chunk-1': 'The warranty covers battery defects.',
            'chunk-2': 'Coverage applies under stated conditions.',
        },
        'reference_chunk_ids': ['chunk-1', 'chunk-2'],
    }
    value.update(overrides)
    return value


def _context():
    return SimpleNamespace(output_key_by_name={'case_enhance': SimpleNamespace(partition='case_0001')})


def _inputs(case=None, run_config=None):
    return {
        'case': case if case is not None else _case(),
        'run_config': run_config if run_config is not None else {'llm_config': {'evo_llm': {'model': 'enhance-test'}}},
    }


def _key_points_response(**overrides):
    value = {
        'key_points': [
            {'statement': 'The warranty covers battery defects.', 'evidence_reference_ids': ['ref_1']},
        ],
    }
    value.update(overrides)
    return value


def _forbidden_claims_response(**overrides):
    value = {'forbidden_claims': ['The warranty covers every defect without conditions.']}
    value.update(overrides)
    return value


def _enhance(*, responses, case=None, captured=None):
    replies = iter(responses)

    def complete(prompt):
        if captured is not None:
            captured.append(prompt)
        return next(replies)

    return generate_enhance(_context(), _inputs(case=case), llm_complete=complete)['case_enhance']


def test_generate_enhance_uses_two_independent_prompts_without_guidance():
    prompts = []

    _enhance(
        responses=(json.dumps(_key_points_response()), json.dumps(_forbidden_claims_response())),
        captured=prompts,
    )

    assert len(prompts) == 2
    for prompt in prompts:
        assert 'What does the warranty cover?' in prompt
        assert 'The warranty covers battery defects under stated conditions.' in prompt
        assert '<reference id="ref_1">' in prompt
        assert '<reference id="ref_2">' in prompt
        assert 'Assess the coverage conclusion and its conditions.' not in prompt
    assert 'key_points' in prompts[0]
    assert 'forbidden_claims' not in prompts[0]
    assert 'forbidden_claims' in prompts[1]
    assert 'key_points' not in prompts[1]


def test_generate_enhance_returns_incremental_fields_with_stable_ids():
    result = _enhance(responses=(
        json.dumps(_key_points_response(key_points=[
            {'statement': 'The warranty covers battery defects.', 'evidence_reference_ids': ['ref_1']},
            {'statement': 'Coverage applies under stated conditions.', 'evidence_reference_ids': ['ref_2']},
        ])),
        json.dumps(_forbidden_claims_response()),
    ))

    assert result == {
        'key_points': [
            {
                'id': 'key_point_1',
                'statement': 'The warranty covers battery defects.',
                'evidence_chunk_ids': ['chunk-1'],
            },
            {
                'id': 'key_point_2',
                'statement': 'Coverage applies under stated conditions.',
                'evidence_chunk_ids': ['chunk-2'],
            },
        ],
        'forbidden_claims': ['The warranty covers every defect without conditions.'],
    }


def test_generate_enhance_does_not_require_key_points_to_cover_all_references():
    result = _enhance(responses=(
        json.dumps(_key_points_response()),
        json.dumps(_forbidden_claims_response(forbidden_claims=[])),
    ))

    assert result['key_points'][0]['evidence_chunk_ids'] == ['chunk-1']
    assert result['forbidden_claims'] == []


def test_generate_enhance_repairs_invalid_key_points_once():
    prompts = []
    result = _enhance(
        responses=(
            json.dumps(_key_points_response(key_points=[])),
            json.dumps(_key_points_response()),
            json.dumps(_forbidden_claims_response(forbidden_claims=[])),
        ),
        captured=prompts,
    )

    assert len(prompts) == 3
    assert 'key_points must contain 1 to 5 items' in prompts[1]
    assert result['forbidden_claims'] == []


def test_generate_enhance_fails_without_partial_output_when_forbidden_claims_fail():
    with pytest.raises(ValueError, match='LLM JSON call failed after 2 attempts'):
        _enhance(responses=(
            json.dumps(_key_points_response()),
            json.dumps(_forbidden_claims_response(forbidden_claims=['', '', '', ''])),
            json.dumps(_forbidden_claims_response(forbidden_claims=['', '', '', ''])),
        ))


@pytest.mark.parametrize(
    'case',
    [
        _case(question=''),
        _case(reference_context={}),
        _case(reference_chunk_ids=['chunk-3']),
    ],
)
def test_generate_enhance_rejects_invalid_base_case(case):
    with pytest.raises(ValueError):
        _enhance(responses=(), case=case)
