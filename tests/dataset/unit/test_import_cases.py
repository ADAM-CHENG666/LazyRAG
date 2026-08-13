import csv
import hashlib
import json

import pytest


import_cases_module = pytest.importorskip(
    'evo.operations.dataset.import_cases',
    reason='dataset.import_cases materializer is implemented in the following code phase',
)
import_cases = import_cases_module.import_cases


HEADERS = ('question', 'answer', 'question_type', 'difficulty', 'grading_guidance', 'reference_context')


class FakeNode:
    def __init__(self, uid, text):
        self.uid = uid
        self.text = text


class FakeKBClient:
    def __init__(self, chunks=None):
        self.docs = {'kb-a': [{'doc_id': 'doc-a'}], 'kb-b': [{'doc_id': 'doc-b'}]}
        self.chunks = chunks or {
            ('kb-a', 'doc-a'): [[FakeNode('chunk-1', 'Evidence 1')]],
            ('kb-b', 'doc-b'): [[FakeNode('chunk-2', 'Evidence 2')]],
        }

    def list_documents(self, kb_id):
        return self.docs[kb_id]

    def iter_chunks(self, kb_id, doc_ids, groups, page_size, *, require_embeddings=False):
        for doc_id in doc_ids:
            yield from self.chunks.get((kb_id, doc_id), [])


class FailingKBClient:
    def list_documents(self, kb_id):
        raise AssertionError('an empty csv_path must not access the knowledge base')


def _source_config(csv_path, target=2):
    sources = [] if not csv_path else [{'kb_id': 'kb-a', 'path': str(csv_path)}]
    return {'kb_ids': ['kb-a', 'kb-b'], 'csv_sources': sources, 'target_case_count': target}


def _row(**overrides):
    row = {
        'question': 'Q1',
        'answer': 'A1',
        'question_type': 'precision',
        'difficulty': 'easy',
        'grading_guidance': 'G1',
        'reference_context': json.dumps([{'chunk_id': 'chunk-1', 'text': 'Evidence 1'}]),
    }
    row.update(overrides)
    return row


def _write_csv(tmp_path, rows, headers=HEADERS):
    source = tmp_path / 'cases.csv'
    with source.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return source


def _manifest(source, target=2, client=None):
    return import_cases(
        None,
        {'source_config': _source_config(source, target)},
        kb_client=client or FakeKBClient(),
    )['import_cases_manifest']


def test_absent_csv_creates_all_generated_assignments_without_kb_access():
    result = _manifest('', target=3, client=FailingKBClient())

    assert result == {
        'source': {'csv_sources': []},
        'stats': {
            'csv_reading': {
                'total_row_count': 0,
                'valid_row_count': 0,
                'loaded_row_count': 0,
                'invalid_row_count': 0,
                'truncated_row_count': 0,
            },
            'case_allocation': {
                'target_case_count': 3,
                'import_case_count': 0,
                'auto_case_count': 3,
                'assignments': {
                    'case_0001': {'mode': 'generated'},
                    'case_0002': {'mode': 'generated'},
                    'case_0003': {'mode': 'generated'},
                },
            },
        },
        'details': [],
    }


@pytest.mark.parametrize(
    'content',
    [
        None,
        b'question\xff',
        b'question,answer\nQ1,A1\n',
        b'legacy_question,answer,question_type,difficulty,grading_guidance,reference_context\n',
        b'question,answer,question_type,difficulty,grading_guidance,reference_context\nQ1,"unterminated\n',
    ],
)
def test_unreadable_or_invalid_csv_errors(tmp_path, content):
    source = tmp_path / 'cases.csv'
    if content is None:
        source = tmp_path / 'missing.csv'
    else:
        source.write_bytes(content)

    with pytest.raises(ValueError, match='csv_path is unreadable|csv header is invalid'):
        _manifest(source)


def test_accepts_only_new_minimal_schema(tmp_path):
    source = _write_csv(tmp_path, [_row()])

    result = _manifest(source)
    assert result['details'][0]['source_id'] == ''
    assert result['details'][0]['load_status'] == 'loaded'

    old_enums = _write_csv(tmp_path, [_row(question_type='factual', difficulty='simple')])
    result = _manifest(old_enums)
    assert result['details'][0]['error']['code'] == 'invalid_question_type'


@pytest.mark.parametrize(
    ('overrides', 'error_code'),
    [
        ({'question': ' '}, 'question is required'),
        ({'answer': ''}, 'answer is required'),
        ({'grading_guidance': ''}, 'grading_guidance is required'),
        ({'question_type': 'legacy'}, 'invalid_question_type'),
        ({'difficulty': 'simple'}, 'invalid_difficulty'),
        ({'reference_context': 'not json'}, 'invalid_reference_context'),
        ({'reference_context': json.dumps([])}, 'invalid_reference_count'),
        ({'reference_context': json.dumps([
            {'chunk_id': 'chunk-1', 'text': 'Evidence 1'},
            {'chunk_id': 'chunk-1', 'text': 'Evidence 1'},
        ]), 'difficulty': 'medium'}, 'duplicate_reference_chunk_id'),
    ],
)
def test_base_case_and_reference_contract(tmp_path, overrides, error_code):
    result = _manifest(_write_csv(tmp_path, [_row(**overrides)]))

    detail = result['details'][0]
    assert detail['load_status'] == 'invalid'
    assert detail['error']['code'] == error_code


def test_kb_reference_resolution_and_text_match(tmp_path):
    source = _write_csv(tmp_path, [
        _row(question='Missing', reference_context=json.dumps([{'chunk_id': 'missing', 'text': 'Evidence'}])),
        _row(question='Mismatch', reference_context=json.dumps([{'chunk_id': 'chunk-1', 'text': 'Different'}])),
    ])

    result = _manifest(source)
    assert [detail['error']['code'] for detail in result['details']] == [
        'reference_chunk_not_found', 'reference_text_mismatch',
    ]


def test_kb_reference_chunk_ids_must_be_unique_across_configured_knowledge_bases(tmp_path):
    source = _write_csv(tmp_path, [_row()])
    client = FakeKBClient({
        ('kb-a', 'doc-a'): [[FakeNode('shared', 'Evidence 1')]],
        ('kb-b', 'doc-b'): [[FakeNode('shared', 'Evidence 2')]],
    })

    with pytest.raises(ValueError, match='ambiguous_chunk_id: shared'):
        _manifest(source, client=client)


def test_duplicate_question_is_invalid_after_normalization(tmp_path):
    source = _write_csv(tmp_path, [_row(question='A  Question'), _row(question=' a question ')])

    result = _manifest(source)
    assert [detail['load_status'] for detail in result['details']] == ['loaded', 'invalid']
    assert result['details'][1]['error']['code'] == 'duplicate_question'


def test_assigns_selected_rows_to_stable_case_partitions(tmp_path):
    source = _write_csv(tmp_path, [
        _row(question='Q1'),
        _row(question='Invalid', reference_context=json.dumps([{'chunk_id': 'missing', 'text': 'Evidence'}])),
        _row(question='Q3', reference_context=json.dumps([{'chunk_id': 'chunk-2', 'text': 'Evidence 2'}])),
    ])

    result = _manifest(source)
    assert result['stats']['case_allocation']['assignments'] == {
        'case_0001': {'mode': 'imported', 'source_row_number': 1},
        'case_0002': {'mode': 'imported', 'source_row_number': 3},
    }
    assert [detail['load_status'] for detail in result['details']] == ['loaded', 'invalid', 'loaded']


def test_truncates_valid_rows_with_manifest_stats(tmp_path):
    source = _write_csv(tmp_path, [
        _row(question='Q1'),
        _row(question='Q2', reference_context=json.dumps([{'chunk_id': 'chunk-2', 'text': 'Evidence 2'}])),
        _row(question='Q3'),
    ])

    result = _manifest(source)
    assert result['stats']['csv_reading'] == {
        'total_row_count': 3,
        'valid_row_count': 3,
        'loaded_row_count': 2,
        'invalid_row_count': 0,
        'truncated_row_count': 1,
    }
    assert [detail['load_status'] for detail in result['details']] == ['loaded', 'loaded', 'truncated']
    assert 'case' not in result['details'][2]
    assert 'warnings' not in result


def test_invalid_details_do_not_consume_capacity_and_generated_slots_fill_gap(tmp_path):
    source = _write_csv(tmp_path, [
        _row(question='Invalid', reference_context=json.dumps([{'chunk_id': 'missing', 'text': 'Evidence'}])),
        _row(question='Valid'),
    ])

    result = _manifest(source, target=3)
    assert result['stats']['case_allocation'] == {
        'target_case_count': 3,
        'import_case_count': 1,
        'auto_case_count': 2,
        'assignments': {
            'case_0001': {'mode': 'imported', 'source_row_number': 2},
            'case_0002': {'mode': 'generated'},
            'case_0003': {'mode': 'generated'},
        },
    }


def test_records_reproducible_audit_metadata_without_raw_csv_blob(tmp_path):
    source = _write_csv(tmp_path, [_row(id='external-7')], headers=(*HEADERS, 'id'))
    raw = source.read_bytes()

    result = _manifest(source)
    assert result['source'] == {'csv_sources': [{
        'kb_id': 'kb-a',
        'csv_path': str(source),
        'csv_sha256': hashlib.sha256(raw).hexdigest(),
        'csv_size_bytes': len(raw),
    }]}
    detail = result['details'][0]
    assert detail['source_row_number'] == 1
    assert detail['source_id'] == 'external-7'
    assert detail['case']['reference_chunk_ids'] == ['chunk-1']
    assert detail['case']['reference_doc_ids'] == ['doc-a']
    preparation = detail['case']['source_preparation']
    assert preparation['kb_ids'] == ['kb-a']
    assert preparation['case_source'] == {
        'final_id': 'case_0001',
        'original_id': 'external-7',
        'source': 'imported_csv',
        'kb_id': 'kb-a',
        'csv_path': str(source),
    }
    assert 'csv_content' not in result['source']


def test_csv_reading_counts_reconcile_loaded_invalid_and_truncated_rows(tmp_path):
    """The review manifest must explain every CSV row with mutually reconcilable counts."""
    source = _write_csv(tmp_path, [
        _row(question='Loaded 1'),
        _row(question='Invalid', reference_context=json.dumps([{'chunk_id': 'missing', 'text': 'Evidence'}])),
        _row(question='Loaded 2', reference_context=json.dumps([{'chunk_id': 'chunk-2', 'text': 'Evidence 2'}])),
        _row(question='Truncated'),
    ])

    result = _manifest(source, target=2)
    counts = result['stats']['csv_reading']

    assert counts == {
        'total_row_count': 4,
        'valid_row_count': 3,
        'loaded_row_count': 2,
        'invalid_row_count': 1,
        'truncated_row_count': 1,
    }
    assert counts['total_row_count'] == counts['valid_row_count'] + counts['invalid_row_count']
    assert counts['valid_row_count'] == counts['loaded_row_count'] + counts['truncated_row_count']
