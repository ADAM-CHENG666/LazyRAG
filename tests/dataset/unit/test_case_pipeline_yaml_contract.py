"""Case pipeline contracts derived from ``evo/operations/dataset/specs``.

These tests are the executable source of the flat-Topic, complete-reference, and
compatibility contracts declared by the YAML specs.
"""

from types import SimpleNamespace

import pytest

from evo.operations.dataset.generate import generate, generate_manifest
from evo.operations.dataset.generate_enhance import generate_enhance
from evo.operations.dataset.qaplan import qaplan_plan, qaplan_spec


LANES = (
    'precision_easy', 'precision_medium', 'precision_hard',
    'reasoning_easy', 'reasoning_medium', 'reasoning_hard',
)


def _case_context(case_id='case_0001'):
    return SimpleNamespace(
        case_ids=('case_0001',),
        output_key_by_name={
            'qaplan_spec': SimpleNamespace(partition=case_id),
            'case': SimpleNamespace(partition=case_id),
        },
    )


def _assignment(mode='generated'):
    return {
        'stats': {'case_allocation': {
            'target_case_count': 1,
            'import_case_count': int(mode == 'imported'),
            'auto_case_count': int(mode == 'generated'),
            'assignments': {'case_0001': {'mode': mode}},
        }},
        'details': [],
    }


def _topic(topic_id='topic-1', *, name='Warranty scope', question_type='precision', chunk_ids=None):
    chunk_ids = chunk_ids or ['chunk-1']
    return {
        'topic_id': topic_id,
        'name': name,
        'question_type': question_type,
        'chunk_ids': chunk_ids,
        'chunk_count': len(chunk_ids),
    }


def _chunk(chunk_id='chunk-1', *, text='The warranty covers battery defects.'):
    return {
        'available': True,
        'kb_id': 'kb-1',
        'doc_id': 'doc-1',
        'chunk_id': chunk_id,
        'text': text,
    }


def _generated_spec():
    return {
        'id': 'case_0001',
        'mode': 'generated',
        'question_type': 'precision',
        'difficulty': 'easy',
        'topic': {'topic_id': 'topic-1', 'name': 'Warranty scope'},
        'instruction': 'Use only the references.',
        'qaplan': {'plan_item_id': 'qaplan_item_000001', 'lane': 'precision_easy'},
        'references': [{
            'kb_id': 'kb-1', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1',
            'text': 'The warranty covers battery defects.',
        }],
    }


def _complete_response():
    return '{"question":"What is covered?","answer":"Battery defects are covered.","grading_guidance":"Check the covered defect."}'


def test_qaplan_plan_initializes_six_integer_lanes_without_ratio_or_cluster_fields():
    """qaplan_plan.yaml: default allocation is quotient/remainder 1:1 and stores only topic_id."""
    output = qaplan_plan(_case_context(), {
        'import_cases_manifest': _assignment(),
        'topic_discovery_manifest': {'topics': [_topic()]},
        'qaplan_plan_params': {'lane_case_counts': {
            'precision_easy': 1, 'precision_medium': 0, 'precision_hard': 0,
            'reasoning_easy': 0, 'reasoning_medium': 0, 'reasoning_hard': 0,
        }},
    })['qaplan_plan']

    assert output['params']['lane_case_counts']['precision_easy'] == 1
    assert output['items'] == [{
        'case_id': 'case_0001', 'plan_item_id': 'qaplan_item_000001',
        'lane': 'precision_easy', 'question_type': 'precision', 'difficulty': 'easy',
        'topic_id': 'topic-1',
    }]
    assert 'lane_ratios' not in output['params']
    assert 'cluster_id' not in output['items'][0]


def test_qaplan_spec_materializes_current_topic_and_complete_references_from_topic_id():
    """qaplan_spec.yaml: the ID is the only selection fact; name, references, and instruction are derived."""
    spec = qaplan_spec(_case_context(), {
        'qaplan_plan': {'items': [{
            'case_id': 'case_0001', 'plan_item_id': 'qaplan_item_000001',
            'lane': 'precision_easy', 'question_type': 'precision', 'difficulty': 'easy',
            'topic_id': 'topic-1',
        }]},
        'topic_discovery_manifest': {'topics': [_topic()]},
        'chunk': (_chunk(),),
        'import_cases_manifest': _assignment(),
    })['qaplan_spec']

    assert spec['topic'] == {'topic_id': 'topic-1', 'name': 'Warranty scope'}
    assert spec['references'] == [{
        'kb_id': 'kb-1', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1',
        'text': 'The warranty covers battery defects.',
    }]
    assert 'cluster_id' not in spec['qaplan']


def test_qaplan_spec_rejects_topic_id_that_is_wrong_type_or_insufficient_for_difficulty():
    """qaplan_spec.yaml: a selected Topic must satisfy the planned type and required Chunk count."""
    with pytest.raises(ValueError, match='topic_id.*question_type|topic_id.*chunk_count'):
        qaplan_spec(_case_context(), {
            'qaplan_plan': {'items': [{
                'case_id': 'case_0001', 'plan_item_id': 'qaplan_item_000001',
                'lane': 'precision_hard', 'question_type': 'precision', 'difficulty': 'hard',
                'topic_id': 'topic-1',
            }]},
            'topic_discovery_manifest': {'topics': [_topic(question_type='reasoning')]},
            'chunk': (_chunk(),),
            'import_cases_manifest': _assignment(),
        })


def test_generate_adds_complete_references_without_changing_legacy_case_fields():
    """generate.yaml: references is additive; existing context, IDs, and KB summary remain byte-for-byte compatible."""
    result = generate(
        _case_context(),
        {'qaplan_spec': _generated_spec(), 'run_config': {'llm_config': {}}},
        llm_complete=lambda _: _complete_response(),
    )['case']

    assert result['references'] == _generated_spec()['references']
    assert result['reference_context'] == [{'chunk_id': 'chunk-1', 'text': 'The warranty covers battery defects.'}]
    assert result['reference_chunk_ids'] == ['chunk-1']
    assert result['reference_doc_ids'] == ['doc-1']
    assert result['source_preparation'] == {'kb_ids': ['kb-1']}


def test_generate_manifest_rejects_complete_reference_ids_that_disagree_with_legacy_ids():
    """generate_manifest.yaml: optional complete references cannot contradict the established reference_chunk_ids contract."""
    with pytest.raises(ValueError, match='references.*reference_chunk_ids'):
        generate_manifest(None, {
            'cases': ({
                'id': 'case_0001', 'question_type': 'precision', 'difficulty': 'easy',
                'reference_chunk_ids': ['chunk-1'],
                'references': [{'kb_id': 'kb-1', 'doc_id': 'doc-1', 'chunk_id': 'other', 'text': 'text'}],
            },),
            'import_cases_manifest': {'stats': {'case_allocation': {'import_case_count': 0, 'auto_case_count': 1}}},
        })


def test_generate_enhance_validates_optional_complete_references_against_legacy_prompt_inputs():
    """generate_enhance.yaml: legacy prompt inputs remain primary, but a supplied complete reference must be consistent."""
    case = {
        'question': 'What is covered?', 'answer': 'Battery defects are covered.',
        'reference_context': [{'chunk_id': 'chunk-1', 'text': 'The warranty covers battery defects.'}],
        'reference_chunk_ids': ['chunk-1'],
        'references': [{'kb_id': 'kb-1', 'doc_id': 'doc-1', 'chunk_id': 'other', 'text': 'wrong'}],
    }

    with pytest.raises(ValueError, match='references.*reference_context'):
        generate_enhance(
            _case_context(),
            {'case': case, 'run_config': {'llm_config': {}}},
            llm_complete=lambda _: '{"key_points":[{"statement":"Battery defects are covered.","evidence_reference_ids":["ref_1"]}]}'
        )
