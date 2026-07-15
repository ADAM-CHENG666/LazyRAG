from types import SimpleNamespace

import pytest

from evo.operations.dataset.kb_client import KnowledgeBaseClient


class FakeDocument:
    def __init__(self, pages=None, exc=None):
        self.pages = pages or {}
        self.exc = exc
        self.calls = []

    def get_nodes(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        key = (kwargs['doc_ids'][0], kwargs['group'], kwargs['offset'])
        return self.pages.get(key, ([], 0))


class FakeKnowledgeBaseClient(KnowledgeBaseClient):
    def __init__(self, documents, **kwargs):
        super().__init__(**kwargs)
        self.documents = documents
        self.list_calls = []

    def list_documents(self, kb_id):
        self.list_calls.append(kb_id)
        return self.documents


def node(uid):
    return SimpleNamespace(uid=uid, embedding={'default': [1.0]})


class FakeDocServer:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        page = int(url.split('page=', 1)[1].split('&', 1)[0])
        return {
            'code': 200,
            'msg': 'success',
            'data': self.pages.get(page, {'items': [], 'total': 0, 'page': page, 'page_size': 100}),
        }


def test_list_documents_uses_doc_server_and_normalizes_rows_without_real_db():
    http = FakeDocServer({
        1: {
            'items': [
                {
                    'doc': {'doc_id': 'doc-1', 'filename': 'file.pdf', 'file_type': 'pdf',
                            'path': '/docs/file.pdf', 'upload_status': 'success'},
                    'relation': {'kb_id': 'kb-1'},
                    'snapshot': {'status': 'SUCCESS'},
                },
                {'doc': {'doc_id': '', 'filename': 'missing.pdf'}, 'relation': {}, 'snapshot': {}},
            ],
            'total': 2,
            'page': 1,
            'page_size': 100,
        },
    })
    client = KnowledgeBaseClient(base_url='http://doc-server:8000', http_get_json=http, document=FakeDocument())

    assert client.list_documents('kb-1') == [{
        'doc_id': 'doc-1',
        'filename': 'file.pdf',
        'file_type': 'pdf',
        'path': '/docs/file.pdf',
        'upload_status': 'success',
        'status': 'SUCCESS',
        'group_counts': {'block': 0, 'line': 0},
        'row': {
            'doc': {
                'doc_id': 'doc-1',
                'filename': 'file.pdf',
                'file_type': 'pdf',
                'path': '/docs/file.pdf',
                'upload_status': 'success',
            },
            'relation': {'kb_id': 'kb-1'},
            'snapshot': {'status': 'SUCCESS'},
        },
    }]
    assert http.urls == [
        'http://doc-server:8000/v1/docs?kb_id=kb-1&include_deleted_or_canceled=false&page=1&page_size=100'
    ]


def test_list_documents_counts_block_and_line_groups_from_document():
    http = FakeDocServer({
        1: {
            'items': [
                {'doc': {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf',
                         'upload_status': 'success'}},
                {'doc': {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf',
                         'upload_status': 'success'}},
            ],
            'total': 2,
            'page': 1,
            'page_size': 100,
        },
    })
    document = FakeDocument({
        ('doc-1', 'block', 0): ([], 8),
        ('doc-1', 'line', 0): ([], 2),
        ('doc-2', 'block', 0): ([], 5),
        ('doc-2', 'line', 0): ([], 0),
    })
    client = KnowledgeBaseClient(base_url='http://doc-server:8000', http_get_json=http, document=document)

    assert client.list_documents('kb-1') == [
        {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'path': '', 'upload_status': 'success',
         'status': '', 'group_counts': {'block': 8, 'line': 2},
         'row': {'doc': {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'upload_status': 'success'},
                 'relation': {}, 'snapshot': {}}},
        {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'path': '', 'upload_status': 'success',
         'status': '', 'group_counts': {'block': 5, 'line': 0},
         'row': {'doc': {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'upload_status': 'success'},
                 'relation': {}, 'snapshot': {}}},
    ]
    assert document.calls == [
        {'doc_ids': ['doc-1'], 'group': 'block', 'kb_id': 'kb-1', 'limit': 1, 'offset': 0,
         'return_total': True, 'sort_by_number': True},
        {'doc_ids': ['doc-1'], 'group': 'line', 'kb_id': 'kb-1', 'limit': 1, 'offset': 0,
         'return_total': True, 'sort_by_number': True},
        {'doc_ids': ['doc-2'], 'group': 'block', 'kb_id': 'kb-1', 'limit': 1, 'offset': 0,
         'return_total': True, 'sort_by_number': True},
        {'doc_ids': ['doc-2'], 'group': 'line', 'kb_id': 'kb-1', 'limit': 1, 'offset': 0,
         'return_total': True, 'sort_by_number': True},
    ]


def test_iter_chunks_empty_doc_ids_yields_nothing_and_does_not_read_document():
    document = FakeDocument()
    client = KnowledgeBaseClient(document=document)

    assert list(client.iter_chunks('kb', doc_ids=[], groups=['block'], page_size=1)) == []
    assert document.calls == []


def test_iter_chunks_doc_ids_none_uses_list_documents_and_preserves_order():
    document = FakeDocument({
        ('doc-b', 'block', 0): ([node('b1')], 1),
        ('doc-a', 'block', 0): ([node('a1')], 1),
    })
    client = FakeKnowledgeBaseClient(
        [{'doc_id': 'doc-b'}, {'doc_id': 'doc-a'}],
        document=document,
    )

    batches = list(client.iter_chunks('kb', doc_ids=None, groups=['block'], page_size=1))

    assert [[item.uid for item in batch] for batch in batches] == [['b1'], ['a1']]
    assert client.list_calls == ['kb']
    assert [call['doc_ids'][0] for call in document.calls] == ['doc-b', 'doc-a']


def test_iter_chunks_rejects_missing_groups_and_invalid_page_size():
    client = KnowledgeBaseClient(document=FakeDocument())

    with pytest.raises(ValueError, match='groups is required'):
        list(client.iter_chunks('kb', doc_ids=['doc'], groups=[], page_size=1))

    with pytest.raises(ValueError, match='page_size must be positive'):
        list(client.iter_chunks('kb', doc_ids=['doc'], groups=['block'], page_size=0))


def test_iter_chunks_pages_single_doc_group_and_uses_stable_get_nodes_options():
    document = FakeDocument({
        ('doc-1', 'block', 0): ([node('n1'), node('n2')], 3),
        ('doc-1', 'block', 2): ([node('n3')], 3),
    })
    client = KnowledgeBaseClient(document=document)

    batches = list(client.iter_chunks('kb', doc_ids=['doc-1'], groups=['block'], page_size=2))

    assert [[item.uid for item in batch] for batch in batches] == [['n1', 'n2'], ['n3']]
    assert document.calls == [
        {
            'doc_ids': ['doc-1'],
            'group': 'block',
            'kb_id': 'kb',
            'limit': 2,
            'offset': 0,
            'return_total': True,
            'sort_by_number': True,
        },
        {
            'doc_ids': ['doc-1'],
            'group': 'block',
            'kb_id': 'kb',
            'limit': 2,
            'offset': 2,
            'return_total': True,
            'sort_by_number': True,
        },
    ]


def test_iter_chunks_wraps_read_errors_with_context():
    original = ValueError('service down')
    client = KnowledgeBaseClient(document=FakeDocument(exc=original))

    with pytest.raises(RuntimeError, match='kb_id=kb doc_id=doc-1 group=block') as exc_info:
        list(client.iter_chunks('kb', doc_ids=['doc-1'], groups=['block'], page_size=1))

    assert exc_info.value.__cause__ is original


def test_document_row_returns_minimal_stable_shape_and_skips_missing_doc_id():
    client = KnowledgeBaseClient(document=FakeDocument())

    assert client._document_row({'filename': 'missing.pdf'}) is None
    assert client._document_row({
        'doc': {
            'doc_id': 'doc-1',
            'display_name': 'Display Name',
            'file_type': 'pdf',
            'path': '/tmp/doc.pdf',
            'upload_status': 'success',
            'extra': 'kept',
        },
        'relation': {'kb_id': 'kb-1'},
        'snapshot': {'status': 'SUCCESS'},
    }) == {
        'doc_id': 'doc-1',
        'filename': 'Display Name',
        'file_type': 'pdf',
        'path': '/tmp/doc.pdf',
        'upload_status': 'success',
        'status': 'SUCCESS',
        'row': {
            'doc': {
                'doc_id': 'doc-1',
                'display_name': 'Display Name',
                'file_type': 'pdf',
                'path': '/tmp/doc.pdf',
                'upload_status': 'success',
                'extra': 'kept',
            },
            'relation': {'kb_id': 'kb-1'},
            'snapshot': {'status': 'SUCCESS'},
        },
    }
