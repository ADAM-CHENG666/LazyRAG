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
    def from_dict(
        cls,
        source_config: Mapping[str, Any],
        select_docs_params: Mapping[str, Any] | None = None,
    ) -> 'SelectDocsParams':
        raw_ids = source_config.get('kb_ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError('kb_ids must be a non-empty list')
        if not all(isinstance(value, str) for value in raw_ids):
            raise ValueError('kb_ids must contain only strings')
        kb_ids = [validate_id(value.strip(), 'kb_id') for value in raw_ids]
        if len(set(kb_ids)) != len(kb_ids):
            raise ValueError('kb_ids must be unique')
        if 'max_docs' in source_config:
            raise ValueError('max_docs is not supported')
        params = select_docs_params or {}
        raw_excluded = params.get('excluded_docs', [])
        if not isinstance(raw_excluded, list):
            raise ValueError('excluded_docs must be a list')
        excluded_docs: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(raw_excluded):
            if not isinstance(item, Mapping):
                raise ValueError(f'excluded_docs[{index}] must be a mapping')
            try:
                kb_id = validate_id(str(item.get('kb_id') or '').strip(), 'excluded_docs.kb_id')
                doc_id = validate_id(str(item.get('doc_id') or '').strip(), 'excluded_docs.doc_id')
            except ValueError as exc:
                raise ValueError(f'excluded_docs[{index}] contains an invalid reference') from exc
            key = (kb_id, doc_id)
            if key in seen:
                raise ValueError('excluded_docs must contain unique (kb_id, doc_id) references')
            seen.add(key)
            excluded_docs.append({'kb_id': kb_id, 'doc_id': doc_id})
        return cls(kb_ids=kb_ids, excluded_docs=excluded_docs)

    def to_dict(self) -> dict[str, Any]:
        return {'kb_ids': list(self.kb_ids), 'excluded_docs': [dict(item) for item in self.excluded_docs]}


def select_docs(
    ctx: Any,
    inputs: Mapping[str, object],
    kb_client: KnowledgeBaseClient | None = None,
) -> Mapping[str, object]:
    source_config = inputs.get('source_config')
    if not isinstance(source_config, Mapping):
        raise ValueError('source_config must be a mapping')
    raw_select_params = inputs.get('select_docs_params', {})
    if not isinstance(raw_select_params, Mapping):
        raise ValueError('select_docs_params must be a mapping')
    params = SelectDocsParams.from_dict(source_config, raw_select_params)
    allocation = _allocation(inputs.get('import_cases_manifest'))
    auto_case_count = allocation['auto_case_count']
    if auto_case_count == 0:
        return {'selected_docs': _payload(params, [], [], 0)}

    client = kb_client or KnowledgeBaseClient()
    rows_by_kb = {kb_id: list(client.list_documents(kb_id)) for kb_id in params.kb_ids}
    if not any(rows_by_kb.values()):
        raise ValueError('dataset.select_docs discovered no documents')
    discovered = [
        {'kb_id': kb_id, **_doc_payload(row)}
        for kb_id in params.kb_ids
        for row in rows_by_kb[kb_id]
    ]
    excluded_keys = {(item['kb_id'], item['doc_id']) for item in params.excluded_docs}
    docs = [doc for doc in discovered if (doc['kb_id'], doc['doc_id']) not in excluded_keys]
    excluded_docs = [doc for doc in discovered if (doc['kb_id'], doc['doc_id']) in excluded_keys]
    return {'selected_docs': _payload(params, docs, excluded_docs, len(discovered))}


def _payload(
    params: SelectDocsParams,
    docs: list[dict[str, Any]],
    excluded_docs: list[dict[str, Any]],
    discovered_count: int,
) -> dict[str, Any]:
    return {
        'kb_ids': list(params.kb_ids),
        'docs': docs,
        'excluded_docs': excluded_docs,
        'stats': {
            'discovered_count': discovered_count,
            'selected_count': len(docs),
            'excluded_count': len(excluded_docs),
        },
        'params': params.to_dict(),
    }


def _allocation(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError('import_cases_manifest must be a mapping')
    stats = value.get('stats')
    if not isinstance(stats, Mapping):
        raise ValueError('import_cases_manifest.stats must be a mapping')
    allocation = stats.get('case_allocation')
    if not isinstance(allocation, Mapping):
        raise ValueError('import_cases_manifest.stats.case_allocation must be a mapping')
    auto_case_count = allocation.get('auto_case_count')
    if isinstance(auto_case_count, bool) or not isinstance(auto_case_count, int) or auto_case_count < 0:
        raise ValueError('import_cases_manifest.stats.case_allocation.auto_case_count must be non-negative')
    return {'auto_case_count': auto_case_count}


def _doc_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'doc_id': str(row.get('doc_id') or ''),
        'filename': str(row.get('filename') or row.get('display_name') or row.get('doc_id') or ''),
        'file_type': str(row.get('file_type') or ''),
        'status': str(row.get('status') or row.get('upload_status') or ''),
    }
