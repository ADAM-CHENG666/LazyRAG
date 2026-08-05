from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ... import validate_id
from .kb_client import KnowledgeBaseClient


@dataclass(frozen=True)
class SelectDocsParams:
    kb_ids: list[str]
    excluded_docs: list[dict[str, str]]

    @classmethod
    def from_dict(cls, source: Mapping[str, Any], raw: Mapping[str, Any] | None = None) -> 'SelectDocsParams':
        values = source.get('kb_ids')
        if not isinstance(values, list) or not values:
            raise ValueError('kb_ids must be a non-empty list')
        try:
            kb_ids = [validate_id(str(value).strip(), 'kb_id') for value in values]
        except ValueError as exc:
            raise ValueError('kb_ids contains an invalid value') from exc
        if len(set(kb_ids)) != len(kb_ids):
            raise ValueError('kb_ids must be unique')
        data = raw or {}
        values = data.get('excluded_docs', [])
        if not isinstance(values, list):
            raise ValueError('excluded_docs must be a list')
        excluded, seen = [], set()
        for index, value in enumerate(values):
            if not isinstance(value, Mapping):
                raise ValueError(f'excluded_docs[{index}] must be a mapping')
            try:
                item = {
                    'kb_id': validate_id(str(value.get('kb_id') or '').strip(), 'excluded_docs.kb_id'),
                    'doc_id': validate_id(str(value.get('doc_id') or '').strip(), 'excluded_docs.doc_id'),
                }
            except ValueError as exc:
                raise ValueError(f'excluded_docs[{index}] contains an invalid reference') from exc
            key = item['kb_id'], item['doc_id']
            if key in seen:
                raise ValueError('excluded_docs must contain unique (kb_id, doc_id) references')
            seen.add(key)
            excluded.append(item)
        return cls(kb_ids, excluded)


def select_docs(ctx: Any, inputs: Mapping[str, object], kb_client: KnowledgeBaseClient | None = None) -> Mapping[str, object]:
    source = inputs.get('source_config')
    if not isinstance(source, Mapping):
        raise ValueError('source_config must be a mapping')
    raw = inputs.get('select_docs_params', {})
    if not isinstance(raw, Mapping):
        raise ValueError('select_docs_params must be a mapping')
    params = SelectDocsParams.from_dict(source, raw)
    excluded = {(item['kb_id'], item['doc_id']) for item in params.excluded_docs}
    client = kb_client or KnowledgeBaseClient()
    documents = []
    for kb_id in params.kb_ids:
        for row in client.list_documents(kb_id):
            doc_id = str(row.get('doc_id') or '').strip()
            if not doc_id:
                continue
            documents.append({
                'kb_id': kb_id,
                'doc_id': doc_id,
                'filename': str(row.get('filename') or row.get('display_name') or doc_id),
                'file_type': str(row.get('file_type') or ''),
                'status': str(row.get('status') or row.get('upload_status') or ''),
                'included': (kb_id, doc_id) not in excluded,
                'discovery_index': len(documents),
            })
    included = sum(item['included'] for item in documents)
    return {'selected_docs': {'documents': documents, 'stats': {
        'discovered_count': len(documents), 'included_count': included, 'excluded_count': len(documents) - included,
    }}}
