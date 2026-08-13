import hashlib
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


def node(uid, *, text='chunk text', chunk_type='text', embedding=None, number=1):
    return SimpleNamespace(
        uid=uid,
        text=text,
        metadata={'type': chunk_type},
        embedding={'default': [1.0]} if embedding is None else embedding,
        number=number,
    )


def node_without_embedding(uid):
    return SimpleNamespace(uid=uid, embedding={})


class FakeMilvusClient:
    def __init__(self, rows=None, exc=None):
        self.rows = rows or []
        self.exc = exc
        self.calls = []

    def has_collection(self, collection):
        self.calls.append(('has_collection', collection))
        return True

    def load_collection(self, collection):
        self.calls.append(('load_collection', collection))

    def describe_collection(self, *, collection_name):
        self.calls.append(('describe_collection', collection_name))
        return {'fields': [{'name': 'uid'}, {'name': 'embedding_embed_main'}]}

    def query(self, **kwargs):
        self.calls.append(('query', kwargs))
        if self.exc is not None:
            raise self.exc
        return self.rows


class FakeVectorStore:
    def __init__(self, client):
        self.client = client

    def _client_context(self):
        class Context:
            def __enter__(inner_self):
                return self.client

            def __exit__(inner_self, exc_type, exc, traceback):
                return False

        return Context()


class FakeStore:
    def __init__(self, vector_store):
        self.vector_store = vector_store
        self.vector_initialized = False

    @property
    def vector_initialized_impl(self):
        self.vector_initialized = True
        return SimpleNamespace(vector_store=self.vector_store)

    @staticmethod
    def _gen_collection_name(group):
        return f'kb_{group}'


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


def test_list_documents_does_not_precount_groups_per_document():
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
    document = FakeDocument()
    client = KnowledgeBaseClient(base_url='http://doc-server:8000', http_get_json=http, document=document)

    assert client.list_documents('kb-1') == [
        {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'path': '', 'upload_status': 'success',
         'status': '',
         'row': {'doc': {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'upload_status': 'success'},
                 'relation': {}, 'snapshot': {}}},
        {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'path': '', 'upload_status': 'success',
         'status': '',
         'row': {'doc': {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf', 'upload_status': 'success'},
                 'relation': {}, 'snapshot': {}}},
    ]
    assert document.calls == []


def test_count_valid_chunks_returns_group_doc_capacity_and_aggregate_filter_stats():
    document = FakeDocument({
        ('doc-1', 'block', 0): ([
            node('valid-1'),
            node('filtered-1', chunk_type='heading'),
            node('empty-1', text='   '),
            node('missing-vector-1', embedding={}),
        ], 4),
    })
    client = KnowledgeBaseClient(document=document)

    result = client.count_valid_chunks(
        'kb-1', ['doc-1'], ['block'], ['text', 'paragraph'], max_scan_chunks=10,
    )

    assert result == {
        'scanned_count': 4,
        'effective_count': 1,
        'capacities': {'block': {'doc-1': 1}},
        'filtered_count_by_type': {'heading': 1},
        'invalid_count_by_reason': {'empty_text': 1, 'missing_embedding': 1},
        'manual_exclusions': [],
        'observed_types': ['text', 'heading'],
    }


def test_layout_type_aliases_are_normalized_before_counting_and_fetching():
    document = FakeDocument({
        ('doc-1', 'block', 0): ([
            node('image', chunk_type='image'),
            node('equation', chunk_type='equation'),
        ], 2),
    })
    client = KnowledgeBaseClient(document=document)

    result = client.count_valid_chunks(
        'kb-1', ['doc-1'], ['block'], ['figure', 'formula'], max_scan_chunks=10,
    )
    selected = client.fetch_valid_chunks(
        'kb-1', 'doc-1', 'block', ['figure', 'formula'], 2, order_by='stable_chunk_id_hash',
    )

    assert result['effective_count'] == 2
    assert result['observed_types'] == ['figure', 'formula']
    assert {item.uid for item in selected} == {'image', 'equation'}


def test_count_valid_chunks_classifies_manual_exclusions_before_content_and_embedding_validation():
    document = FakeDocument({
        ('doc-1', 'block', 0): ([
            node('keep'),
            node('excluded-empty', text=' ', embedding={}),
        ], 2),
    })

    result = KnowledgeBaseClient(document=document).count_valid_chunks(
        'kb-1',
        ['doc-1'],
        ['block'],
        ['text'],
        max_scan_chunks=10,
        excluded_chunk_ids={'excluded-empty'},
    )

    assert result['effective_count'] == 1
    assert result['invalid_count_by_reason'] == {}
    assert result['manual_exclusions'] == [{
        'kb_id': 'kb-1',
        'doc_id': 'doc-1',
        'chunk_id': 'excluded-empty',
        'group': 'block',
        'type': 'text',
    }]


def test_count_valid_chunks_rejects_scan_limit_without_returning_partial_capacity():
    document = FakeDocument({
        ('doc-1', 'block', 0): ([node('one'), node('two')], 2),
    })

    with pytest.raises(ValueError, match='max_scan_chunks'):
        KnowledgeBaseClient(document=document).count_valid_chunks(
            'kb-1', ['doc-1'], ['block'], ['text'], max_scan_chunks=1,
        )


def test_fetch_valid_chunks_selects_by_stable_chunk_id_hash_and_hydrates_only_selected_payloads():
    values = [node(f'chunk-{index}', number=index) for index in range(1, 8)]
    document = FakeDocument({('doc-1', 'block', 0): (values, len(values))})
    client = KnowledgeBaseClient(document=document)

    selected = client.fetch_valid_chunks(
        'kb-1', 'doc-1', 'block', ['text'], 3, order_by='stable_chunk_id_hash',
    )
    expected = sorted(values, key=lambda item: hashlib.sha256(item.uid.encode()).hexdigest())[:3]

    assert [item.uid for item in selected] == [item.uid for item in expected]


def test_fetch_valid_chunks_never_returns_manually_excluded_ids():
    values = [node('keep'), node('exclude')]
    client = KnowledgeBaseClient(document=FakeDocument({('doc-1', 'block', 0): (values, 2)}))

    selected = client.fetch_valid_chunks(
        'kb-1',
        'doc-1',
        'block',
        ['text'],
        2,
        order_by='stable_chunk_id_hash',
        excluded_chunk_ids={'exclude'},
    )

    assert [item.uid for item in selected] == ['keep']


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


def test_iter_chunks_initializes_vector_store_and_attaches_stored_embeddings():
    milvus = FakeMilvusClient(rows=[{'uid': 'n1', 'embedding_embed_main': [0.1, 0.2]}])
    store = FakeStore(FakeVectorStore(milvus))
    document = FakeDocument({('doc-1', 'block', 0): ([node_without_embedding('n1')], 1)})
    document._impl = SimpleNamespace(store=store)

    batches = list(KnowledgeBaseClient(document=document).iter_chunks(
        'kb', doc_ids=['doc-1'], groups=['block'], page_size=1,
    ))

    assert store.vector_initialized is True
    assert batches[0][0].embedding == {'embed_main': [0.1, 0.2]}
    assert milvus.calls[-1] == ('query', {
        'collection_name': 'kb_block',
        'filter': "uid in ['n1']",
        'output_fields': ['uid', 'embedding_embed_main'],
    })


def test_iter_chunks_raises_embedding_query_error_with_context():
    original = ValueError('Milvus unavailable')
    store = FakeStore(FakeVectorStore(FakeMilvusClient(exc=original)))
    document = FakeDocument({('doc-1', 'block', 0): ([node_without_embedding('n1')], 1)})
    document._impl = SimpleNamespace(store=store)

    with pytest.raises(RuntimeError, match='failed to read stored embeddings: kb_id=kb doc_id=doc-1 group=block') as exc_info:
        list(KnowledgeBaseClient(document=document).iter_chunks(
            'kb', doc_ids=['doc-1'], groups=['block'], page_size=1,
        ))

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
