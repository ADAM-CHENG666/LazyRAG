from evo.operations.dataset.assemble import assemble_dataset
from evo.operations.dataset.operations import _finalize_case


def test_minimal_imported_case_can_be_finalized_and_assembled() -> None:
    draft = {
        'id': 'case_0001',
        'question': 'What happened?',
        'answer': 'The service restarted.',
        'question_type': 'precision',
        'difficulty': '',
        'grading_guidance': 'The answer must identify the restart.',
        'key_points': [],
        'forbidden_claims': [],
        'reference_context': '',
        'reference_doc': [],
        'reference_doc_ids': [],
        'reference_chunk_ids': [],
        'generate_reason': '',
        'is_deleted': False,
        'source_preparation': {
            'dataset_mode': 'imported',
            'case_source': {'source': 'imported_csv'},
        },
    }

    case = _finalize_case(
        draft,
        {'key_points': [], 'forbidden_claims': []},
        'case_0001',
    )
    dataset = assemble_dataset({'case_0001': case}, run_id='run-imported')

    assert dataset['case_num'] == 1
    assert dataset['cases'][0] == {
        **dataset['cases'][0],
        'case_id': 'case_0001',
        'question': 'What happened?',
        'question_type': 'precision',
        'difficulty': '',
        'ground_truth': 'The service restarted.',
        'grading_guidance': 'The answer must identify the restart.',
        'key_points': [],
        'forbidden_claims': [],
        'reference_context': [],
        'reference_doc': [],
        'reference_doc_ids': [],
        'reference_chunk_ids': [],
        'generate_reason': '',
        'is_deleted': False,
    }


def test_imported_case_keeps_optional_grading_fields() -> None:
    draft = {
        'id': 'external-7',
        'question': 'Why did it restart?',
        'answer': 'A configuration change triggered it.',
        'question_type': 'reasoning',
        'difficulty': 'medium',
        'grading_guidance': 'Connect the change to the restart.',
        'key_points': [{'statement': 'Configuration changed', 'evidence_chunk_ids': ['chunk-1']}],
        'forbidden_claims': ['The host failed'],
        'reference_context': [{'chunk_id': 'chunk-1', 'text': 'The configuration changed.'}],
        'reference_doc': ['operations.md'],
        'reference_doc_ids': ['doc-1'],
        'reference_chunk_ids': ['chunk-1'],
        'generate_reason': 'Imported by the user',
        'is_deleted': False,
        'source_preparation': {
            'dataset_mode': 'imported',
            'case_source': {'source': 'imported_csv'},
        },
    }

    case = _finalize_case(
        draft,
        {'key_points': draft['key_points'], 'forbidden_claims': draft['forbidden_claims']},
        'external-7',
    )
    dataset = assemble_dataset({'external-7': case}, run_id='run-imported')

    result = dataset['cases'][0]
    assert result['question_type'] == 'reasoning'
    assert result['ground_truth'] == 'A configuration change triggered it.'
    assert result['key_points'] == draft['key_points']
    assert result['forbidden_claims'] == ['The host failed']
    assert result['generate_reason'] == 'Imported by the user'
