import csv
import json
from types import SimpleNamespace

import pytest

from evo.operations.dataset.generate import generate
from evo.operations.dataset.generate_enhance import generate_enhance
from evo.operations.dataset.import_cases import import_cases


HEADERS = (
    'case_id', 'question', 'question_type', 'difficulty', 'ground_truth',
    'grading_guidance', 'key_points', 'forbidden_claims', 'reference_context',
    'reference_doc', 'reference_doc_ids', 'reference_chunk_ids',
    'generate_reason', 'is_deleted',
)


class FakeNode:
    def __init__(self, uid: str, text: str) -> None:
        self.uid = uid
        self.text = text


class FakeKBClient:
    def list_documents(self, kb_id: str):
        return [{'doc_id': f'doc-{kb_id}'}]

    def iter_chunks(self, kb_id, doc_ids, groups, page_size, *, require_embeddings=False):
        del groups, page_size, require_embeddings
        for doc_id in doc_ids:
            yield [[FakeNode(f'chunk-{kb_id}', f'text from {doc_id}')][0]]


def _row(**overrides):
    row = {
        'case_id': '',
        'question': 'Q1',
        'question_type': 'precision',
        'difficulty': '',
        'ground_truth': 'A1',
        'grading_guidance': 'G1',
        'key_points': '',
        'forbidden_claims': '',
        'reference_context': '',
        'reference_doc': '',
        'reference_doc_ids': '',
        'reference_chunk_ids': '',
        'generate_reason': '',
        'is_deleted': '',
    }
    row.update(overrides)
    return row


def _write_csv(tmp_path, rows):
    path = tmp_path / 'cases.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _manifest(path, *, target=99):
    return import_cases(None, {'source_config': {
        'kb_ids': ['kb-a', 'kb-b'],
        'csv_sources': [{'kb_id': 'kb-a', 'path': str(path)}],
        'target_case_count': target,
    }}, kb_client=FakeKBClient())['import_cases_manifest']


def test_inline_eval_set_cases_use_the_same_import_contract():
    manifest = import_cases(None, {'source_config': {
        'kb_ids': ['kb-a'],
        'imported_cases': [_row(case_id='external-1')],
        'target_case_count': 99,
    }}, kb_client=FakeKBClient())['import_cases_manifest']

    assert manifest['stats']['case_allocation']['target_case_count'] == 1
    assert manifest['details'][0]['case']['id'] == 'external-1'
    assert manifest['details'][0]['case']['source_preparation']['case_source']['source'] == 'imported_eval_set'


def test_supplemented_eval_set_allocates_generated_cases_after_imported_cases():
    manifest = import_cases(None, {'source_config': {
        'kb_ids': ['kb-a'],
        'imported_cases': [_row(case_id='external-1'), _row(case_id='external-2', question='Q2')],
        'target_case_count': 5,
        'supplement_existing_eval_set': True,
    }}, kb_client=FakeKBClient())['import_cases_manifest']

    assert manifest['stats']['case_allocation'] == {
        'target_case_count': 5,
        'import_case_count': 2,
        'auto_case_count': 3,
        'assignments': {
            'external-1': {'mode': 'imported', 'source_row_number': 1},
            'external-2': {'mode': 'imported', 'source_row_number': 2},
            'case_0001': {'mode': 'generated'},
            'case_0002': {'mode': 'generated'},
            'case_0003': {'mode': 'generated'},
        },
    }


def test_minimal_imported_case_uses_import_count_as_target(tmp_path):
    manifest = _manifest(_write_csv(tmp_path, [_row(), _row(question='Q2')]))

    allocation = manifest['stats']['case_allocation']
    assert allocation == {
        'target_case_count': 2,
        'import_case_count': 2,
        'auto_case_count': 0,
        'assignments': {
            'case_0001': {'mode': 'imported', 'source_row_number': 1},
            'case_0002': {'mode': 'imported', 'source_row_number': 2},
        },
    }
    case = manifest['details'][0]['case']
    assert case['question'] == 'Q1'
    assert case['answer'] == 'A1'
    assert case['difficulty'] == ''
    assert case['key_points'] == []
    assert case['forbidden_claims'] == []
    assert case['reference_chunk_ids'] == []


def test_import_excludes_deleted_rows_and_rejects_duplicate_case_ids(tmp_path):
    manifest = _manifest(_write_csv(tmp_path, [
        _row(case_id='external-1'),
        _row(case_id='deleted', question='deleted', is_deleted='true'),
        _row(case_id='external-1', question='duplicate id'),
    ]))

    assert manifest['stats']['case_allocation']['target_case_count'] == 1
    assert manifest['stats']['case_allocation']['assignments'] == {
        'external-1': {'mode': 'imported', 'source_row_number': 1},
    }
    assert [item['load_status'] for item in manifest['details']] == [
        'loaded', 'deleted', 'invalid',
    ]
    assert manifest['details'][2]['error']['code'] == 'duplicate_case_id'


def test_optional_reference_ids_are_validated_without_matching_reference_text(tmp_path):
    manifest = _manifest(_write_csv(tmp_path, [_row(
        difficulty='',
        reference_context='user supplied context',
        reference_doc_ids='doc-kb-a',
        reference_chunk_ids='chunk-kb-a',
    )]))

    case = manifest['details'][0]['case']
    assert case['difficulty'] == 'easy'
    assert case['reference_context'] == 'user supplied context'
    assert case['reference_doc_ids'] == ['doc-kb-a']
    assert case['reference_chunk_ids'] == ['chunk-kb-a']

    invalid = _manifest(_write_csv(tmp_path, [
        _row(question='valid'),
        _row(question='invalid', reference_chunk_ids='missing'),
    ]))
    assert invalid['details'][1]['error']['code'] == 'reference_chunk_not_found'


def test_zero_valid_imported_cases_fails(tmp_path):
    with pytest.raises(ValueError, match='no valid imported cases'):
        _manifest(_write_csv(tmp_path, [_row(is_deleted='true')]))


def test_imported_generate_and_grading_are_pass_through_without_llm():
    imported = {
        'id': 'case_0001',
        'question': 'Q',
        'answer': 'A',
        'question_type': 'precision',
        'difficulty': '',
        'grading_guidance': 'G',
        'key_points': [],
        'forbidden_claims': [],
        'reference_context': '',
        'reference_chunk_ids': [],
        'reference_doc_ids': [],
        'source_preparation': {'dataset_mode': 'imported'},
    }
    generate_context = SimpleNamespace(
        output_key_by_name={'case': SimpleNamespace(partition='case_0001')},
    )
    generated = generate(generate_context, {
        'qaplan_spec': {
            'id': 'case_0001', 'mode': 'imported', 'imported_case': imported,
        },
        'run_config': {'llm_config': {}},
    }, llm_complete=lambda _: pytest.fail('imported generate must not call LLM'))['case']

    enhance_context = SimpleNamespace(
        output_key_by_name={'case_enhance': SimpleNamespace(partition='case_0001')},
    )
    enhancement = generate_enhance(enhance_context, {
        'case': generated,
        'run_config': {'llm_config': {}},
    }, llm_complete=lambda _: pytest.fail('imported grading must not call LLM'))['case_enhance']

    assert enhancement == {'key_points': [], 'forbidden_claims': []}
