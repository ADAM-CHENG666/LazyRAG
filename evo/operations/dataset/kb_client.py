import json
import os
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_DOCUMENTS: dict[tuple[str, ...], Any] = {}
GROUP_COUNT_GROUPS = ('block', 'line')
DOCS_PAGE_SIZE = 100


class KnowledgeBaseClient:
    """Read document indexes and DocNode batches from a knowledge base."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_get_json: Callable[[str], dict[str, Any]] | None = None,
        document: Any | None = None,
        document_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self._http_get_json = http_get_json
        self._document = document
        self._document_factory = document_factory

    def list_documents(self, kb_id: str) -> list[dict[str, Any]]:
        docs = self._list_documents_from_doc_server(kb_id)
        group_counts = self._group_counts_by_doc(kb_id, [doc['doc_id'] for doc in docs])
        return [{**doc, 'group_counts': group_counts.get(doc['doc_id'], {})} for doc in docs]

    def iter_chunks(
        self,
        kb_id: str,
        doc_ids: list[str] | None,
        groups: list[str],
        page_size: int,
    ) -> Iterator[list[Any]]:
        if not groups:
            raise ValueError('groups is required')
        if page_size <= 0:
            raise ValueError('page_size must be positive')

        resolved_doc_ids = [doc['doc_id'] for doc in self.list_documents(kb_id)] if doc_ids is None else doc_ids
        if not resolved_doc_ids:
            return

        document = self._get_document()
        for doc_id in resolved_doc_ids:
            for group in groups:
                offset = 0
                while True:
                    try:
                        nodes, total = document.get_nodes(
                            doc_ids=[doc_id],
                            group=group,
                            kb_id=kb_id,
                            limit=page_size,
                            offset=offset,
                            return_total=True,
                            sort_by_number=True,
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f'failed to read chunks: kb_id={kb_id} doc_id={doc_id} group={group}'
                        ) from exc
                    batch = list(nodes or [])
                    if not batch:
                        break
                    self._attach_stored_embeddings(
                        document, batch, kb_id=kb_id, doc_id=doc_id, group=group,
                    )
                    self._require_embeddings(batch, kb_id=kb_id, doc_id=doc_id, group=group)
                    yield batch
                    offset += len(batch)
                    if offset >= int(total or offset):
                        break

    def _get_document(self) -> Any:
        if self._document is not None:
            return self._document
        if self._document_factory is not None:
            self._document = self._document_factory()
            return self._document

        self._document = _build_document()
        return self._document

    @staticmethod
    def _require_embeddings(nodes: list[Any], *, kb_id: str, doc_id: str, group: str) -> None:
        missing = [
            str(getattr(node, 'uid', '') or getattr(node, '_uid', '') or '')
            for node in nodes
            if not _has_embedding(node)
        ]
        if missing:
            raise RuntimeError(
                f'chunk embeddings are unavailable in Milvus: kb_id={kb_id} doc_id={doc_id} '
                f'group={group} chunk_ids={missing}'
            )

    @staticmethod
    def _attach_stored_embeddings(
        document: Any,
        nodes: list[Any],
        *,
        kb_id: str,
        doc_id: str,
        group: str,
    ) -> None:
        '''Read vectors explicitly because the LazyLLM UID lookup omits vector output fields.

        The installed LazyLLM version passes `output_fields=None` for UID lookups.
        Milvus then returns only the UID even though `embedding_embed_main` exists.
        '''
        missing = [node for node in nodes if not _has_embedding(node)]
        if not missing:
            return

        try:
            store, vector_store = _milvus_store(document)
            uids = [str(getattr(node, 'uid', '') or getattr(node, '_uid', '') or '') for node in missing]
            uids = [uid for uid in uids if uid]
            if not uids:
                return
            collection = store._gen_collection_name(group)
            with vector_store._client_context() as client:
                if not client.has_collection(collection):
                    return
                client.load_collection(collection)
                fields = [
                    field.get('name')
                    for field in client.describe_collection(collection_name=collection).get('fields', [])
                    if str(field.get('name') or '').startswith('embedding_')
                ]
                if not fields:
                    return
                rows = client.query(
                    collection_name=collection,
                    filter=f'uid in {uids!r}',
                    output_fields=['uid', *fields],
                )
            embeddings = {
                str(row.get('uid') or ''): {
                    key.removeprefix('embedding_'): list(value)
                    for key, value in row.items()
                    if key.startswith('embedding_') and value is not None
                }
                for row in rows
            }
            for node in missing:
                uid = str(getattr(node, 'uid', '') or getattr(node, '_uid', '') or '')
                if embedding := embeddings.get(uid):
                    setattr(node, 'embedding', embedding)
        except Exception as exc:
            raise RuntimeError(
                f'failed to read stored embeddings: kb_id={kb_id} doc_id={doc_id} group={group}'
            ) from exc

    def _list_documents_from_doc_server(self, kb_id: str) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get_docs_page(kb_id, page)
            items = data.get('items') or []
            docs.extend(doc for item in items if (doc := self._document_row(item)))

            page_size = _int(data.get('page_size')) or DOCS_PAGE_SIZE
            total = _int(data.get('total'))
            if not items or len(docs) >= total or len(items) < page_size:
                break
            page += 1
        return docs

    def _get_docs_page(self, kb_id: str, page: int) -> dict[str, Any]:
        query = urlencode({
            'kb_id': kb_id,
            'include_deleted_or_canceled': 'false',
            'page': page,
            'page_size': DOCS_PAGE_SIZE,
        })
        payload = self._get_json(f'{self._doc_server_base_url()}/v1/docs?{query}')
        if _int(payload.get('code')) != 200:
            raise RuntimeError(f'doc server /v1/docs failed: {payload.get("msg") or payload}')
        return payload['data']

    def _get_json(self, url: str) -> dict[str, Any]:
        if self._http_get_json is not None:
            return self._http_get_json(url)
        request = Request(url, headers={'Accept': 'application/json'})
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))

    def _doc_server_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip('/')
        value = os.getenv('LAZYMIND_EVO_KB_BASE_URL', '').strip()
        if not value:
            raise ValueError('LAZYMIND_EVO_KB_BASE_URL is required')
        return value.rstrip('/')

    def _document_row(self, item: dict[str, Any]) -> dict[str, Any] | None:
        doc = item.get('doc') or {}
        relation = item.get('relation') or {}
        snapshot = item.get('snapshot') or {}
        get = doc.get
        doc_id = str(get('doc_id') or '')
        if not doc_id:
            return None
        return {
            'doc_id': doc_id,
            'filename': str(get('filename') or get('display_name') or doc_id),
            'file_type': str(get('file_type') or ''),
            'path': str(get('path') or ''),
            'upload_status': get('upload_status', ''),
            'status': str(snapshot.get('status') or get('status') or ''),
            'row': {'doc': dict(doc), 'relation': dict(relation), 'snapshot': dict(snapshot)},
        }

    def _group_counts_by_doc(
        self,
        kb_id: str,
        doc_ids: list[str],
    ) -> dict[str, dict[str, int]]:
        if not doc_ids:
            return {}

        document = self._get_document()
        counts: dict[str, dict[str, int]] = {}
        for doc_id in doc_ids:
            doc_counts: dict[str, int] = {}
            for group in GROUP_COUNT_GROUPS:
                try:
                    _, total = document.get_nodes(
                        doc_ids=[doc_id],
                        group=group,
                        kb_id=kb_id,
                        limit=1,
                        offset=0,
                        return_total=True,
                        sort_by_number=True,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f'failed to count chunks: kb_id={kb_id} doc_id={doc_id} group={group}'
                    ) from exc
                doc_counts[group] = max(_int(total), 0)
            counts[doc_id] = doc_counts
        return counts


def _build_document() -> Any:
    from lazymind.config import config
    from lazymind.parsing.service.build_document import build_document

    algo_id = str(config['algo_id'] or config['agentic_kb_name'] or '').strip()
    if not algo_id:
        raise ValueError('algo_id is required')
    key = ('local', algo_id)
    if key not in _DOCUMENTS:
        _DOCUMENTS[key] = build_document(algo_id, serve=False)
    return _DOCUMENTS[key]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _has_embedding(node: Any) -> bool:
    value = getattr(node, 'embedding', None)
    return isinstance(value, Mapping) and any(bool(vector) for vector in value.values())


def _milvus_store(document: Any) -> tuple[Any, Any]:
    impl = getattr(document, '_impl', None)
    store = getattr(impl, 'store', None)
    store_impl = getattr(store, 'vector_initialized_impl', None)
    vector_store = getattr(store_impl, 'vector_store', None)
    if store is None or vector_store is None:
        raise RuntimeError('Milvus vector store is unavailable')
    return store, vector_store
