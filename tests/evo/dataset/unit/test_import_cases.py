import csv
import hashlib
from types import SimpleNamespace

import pytest

from evo.operations.dataset.import_cases import import_cases
from evo.operations.dataset.kb_client import KnowledgeBaseClient


HEADERS = ('question', 'question_type', 'ground_truth', 'grading_guidance')


class FakeNode:
    def __init__(self, uid, text):
        self.uid = uid
        self.text = text


class FakeKBClient:
    def list_documents(self, kb_id):
        return [{'doc_id': f'doc-{kb_id}'}]

    def iter_chunks(self, kb_id, doc_ids, groups, page_size, *, require_embeddings=False):
        del groups, page_size, require_embeddings
        for doc_id in doc_ids:
            yield [FakeNode(f'chunk-{kb_id}', f'evidence from {doc_id}')]


class FailingKBClient:
    def list_documents(self, kb_id):
        raise AssertionError('automatic generation must not access the knowledge base during import')


def _config(path='', target=3):
    return {
        'kb_ids': ['kb-a', 'kb-b'],
        'csv_sources': [] if not path else [{'kb_id': 'kb-a', 'path': str(path)}],
        'target_case_count': target,
    }


def _write(tmp_path, rows, headers=HEADERS):
    path = tmp_path / 'cases.csv'
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(**overrides):
    row = {
        'question': 'Q1',
        'question_type': 'precision',
        'ground_truth': 'A1',
        'grading_guidance': 'G1',
    }
    row.update(overrides)
    return row


def _manifest(path='', target=3, client=None):
    return import_cases(None, {'source_config': _config(path, target)},
                        kb_client=client or FakeKBClient())['import_cases_manifest']


def test_absent_csv_keeps_configured_automatic_target_without_kb_access():
    result = _manifest('', target=3, client=FailingKBClient())

    assert result['stats']['case_allocation'] == {
        'target_case_count': 3,
        'import_case_count': 0,
        'auto_case_count': 3,
        'assignments': {
            'case_0001': {'mode': 'generated'},
            'case_0002': {'mode': 'generated'},
            'case_0003': {'mode': 'generated'},
        },
    }


@pytest.mark.parametrize(
    'content',
    [None, b'question\xff', b'question,ground_truth\nQ,A\n', b'unknown,question,question_type,ground_truth,grading_guidance\nx,Q,precision,A,G\n'],
)
def test_unreadable_or_invalid_csv_errors(tmp_path, content):
    path = tmp_path / 'cases.csv'
    if content is not None:
        path.write_bytes(content)

    with pytest.raises(ValueError, match='csv_path is unreadable|csv header is invalid'):
        _manifest(path)


@pytest.mark.parametrize(
    ('overrides', 'error_code'),
    [
        ({'question': ''}, 'question is required'),
        ({'question_type': 'legacy'}, 'invalid_question_type'),
        ({'ground_truth': ''}, 'ground_truth is required'),
        ({'grading_guidance': ''}, 'grading_guidance is required'),
    ],
)
def test_required_case_fields_are_validated_per_row(tmp_path, overrides, error_code):
    path = _write(tmp_path, [_row(question='valid'), _row(**{'question': 'invalid', **overrides})])

    result = _manifest(path)
    assert result['details'][1]['load_status'] == 'invalid'
    assert result['details'][1]['error']['code'] == error_code
    assert result['stats']['case_allocation']['target_case_count'] == 1


def test_import_manifest_records_source_audit_without_raw_content(tmp_path):
    headers = (*HEADERS, 'case_id')
    path = _write(tmp_path, [_row(case_id='external-7')], headers=headers)
    raw = path.read_bytes()

    result = _manifest(path)
    assert result['source'] == {'csv_sources': [{
        'kb_id': 'kb-a',
        'csv_path': str(path),
        'csv_sha256': hashlib.sha256(raw).hexdigest(),
        'csv_size_bytes': len(raw),
    }]}
    assert result['details'][0]['case_id'] == 'external-7'
    assert result['details'][0]['case']['source_preparation']['case_source']['original_id'] == 'external-7'
    assert 'csv_content' not in result['source']


def test_valid_rows_are_not_truncated_or_filled_to_configured_target(tmp_path):
    path = _write(tmp_path, [_row(question='Q1'), _row(question='Q2')])

    result = _manifest(path, target=1)
    assert result['stats']['csv_reading'] == {
        'total_row_count': 2,
        'valid_row_count': 2,
        'loaded_row_count': 2,
        'invalid_row_count': 0,
        'deleted_row_count': 0,
        'truncated_row_count': 0,
    }
    assert result['stats']['case_allocation']['target_case_count'] == 2
    assert result['stats']['case_allocation']['auto_case_count'] == 0


class LookupDocument:
    def __init__(self, nodes_by_uid):
        self.nodes_by_uid = nodes_by_uid
        self.calls = []

    def get_nodes(self, **kwargs):
        self.calls.append(kwargs)
        uids = kwargs.get('uids') or []
        nodes = [self.nodes_by_uid[uid] for uid in uids if uid in self.nodes_by_uid]
        return nodes, len(nodes)


def test_import_resolves_referenced_chunks_by_uid_without_scanning_unrelated_documents(tmp_path):
    document = LookupDocument({
        'chunk-keep': SimpleNamespace(
            uid='chunk-keep', text='keep', global_metadata={'docid': 'doc-1'},
        ),
        'chunk-other': SimpleNamespace(
            uid='chunk-other', text='other', global_metadata={'docid': 'doc-2'},
        ),
    })

    class Client(KnowledgeBaseClient):
        def list_documents(self, kb_id):
            return [{'doc_id': 'doc-1'}, {'doc_id': 'doc-2'}]

    headers = (*HEADERS, 'reference_doc_ids', 'reference_chunk_ids')
    path = _write(tmp_path, [_row(reference_doc_ids='doc-1', reference_chunk_ids='chunk-keep')], headers)
    client = Client(document=document)

    result = import_cases(None, {'source_config': {
        'kb_ids': ['kb-a'],
        'csv_sources': [{'kb_id': 'kb-a', 'path': str(path)}],
        'target_case_count': 1,
    }}, kb_client=client)['import_cases_manifest']

    assert result['details'][0]['load_status'] == 'loaded'
    assert result['details'][0]['case']['reference_chunk_ids'] == ['chunk-keep']
    assert document.calls
    assert all(call.get('uids') == ['chunk-keep'] for call in document.calls)
    assert all(not call.get('doc_ids') for call in document.calls)
