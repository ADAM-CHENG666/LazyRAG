import pytest


import_cases_module = pytest.importorskip(
    'evo.operations.dataset.import_cases',
    reason='dataset.import_cases materializer is implemented in the following code phase',
)
import_cases = import_cases_module.import_cases


class FakeNode:
    def __init__(self, uid, text):
        self.uid = uid
        self.text = text


class FakeKBClient:
    def __init__(self):
        self.docs = {'kb-a': [{'doc_id': 'doc-a'}], 'kb-b': [{'doc_id': 'doc-b'}]}
        self.chunks = {
            ('kb-a', 'doc-a'): [[FakeNode('chunk-1', 'Evidence 1')]],
            ('kb-b', 'doc-b'): [[FakeNode('chunk-2', 'Evidence 2')]],
        }

    def list_documents(self, kb_id):
        return self.docs[kb_id]

    def iter_chunks(self, kb_id, doc_ids, groups, page_size):
        for doc_id in doc_ids:
            yield from self.chunks.get((kb_id, doc_id), [])


def _source_config(csv_path):
    return {'kb_ids': ['kb-a', 'kb-b'], 'csv_path': str(csv_path), 'target_case_count': 2}


def test_import_cases_freezes_assignments_and_reports_every_csv_row(tmp_path):
    source = tmp_path / 'cases.csv'
    source.write_text(
        'question,answer,question_type,difficulty,grading_guidance,reference_context\n'
        'Q1,A1,precision,easy,G1,"[{""chunk_id"":""chunk-1"",""text"":""Evidence 1""}]"\n'
        'Q2,A2,reasoning,easy,G2,"[{""chunk_id"":""missing"",""text"":""Evidence""}]"\n'
        'Q3,A3,precision,easy,G3,"[{""chunk_id"":""chunk-2"",""text"":""Evidence 2""}]"\n',
        encoding='utf-8',
    )
    result = import_cases(None, {'source_config': _source_config(source)}, kb_client=FakeKBClient())['import_cases_manifest']

    assert result['stats']['csv_reading'] == {
        'total_row_count': 3, 'valid_row_count': 2, 'loaded_row_count': 2,
    }
    assert result['stats']['case_allocation']['assignments'] == {
        'case_0001': {'mode': 'imported', 'source_row_number': 2},
        'case_0002': {'mode': 'imported', 'source_row_number': 4},
    }
    assert [detail['load_status'] for detail in result['details']] == ['loaded', 'invalid', 'loaded']
    assert result['details'][1]['error']['code'] == 'reference_chunk_not_found'


def test_import_cases_marks_extra_valid_rows_as_truncated_instead_of_warning(tmp_path):
    source = tmp_path / 'cases.csv'
    source.write_text(
        'question,answer,question_type,difficulty,grading_guidance,reference_context\n'
        'Q1,A1,precision,easy,G1,"[{""chunk_id"":""chunk-1"",""text"":""Evidence 1""}]"\n'
        'Q2,A2,precision,easy,G2,"[{""chunk_id"":""chunk-2"",""text"":""Evidence 2""}]"\n'
        'Q3,A3,precision,easy,G3,"[{""chunk_id"":""chunk-1"",""text"":""Evidence 1""}]"\n',
        encoding='utf-8',
    )
    result = import_cases(None, {'source_config': _source_config(source)}, kb_client=FakeKBClient())['import_cases_manifest']

    assert 'warnings' not in result
    assert all(detail['load_status'] in {'loaded', 'invalid', 'truncated'} for detail in result['details'])
