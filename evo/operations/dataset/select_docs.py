from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ... import validate_id
from .kb_client import KnowledgeBaseClient


@dataclass(frozen=True)
class SelectDocsParams:
    kb_id: str
    max_docs: int = 100
    target_case_count: int = 100

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'SelectDocsParams':
        kb_id = validate_id(str(data.get('kb_id') or '').strip(), 'kb_id')
        return cls(
            kb_id=kb_id,
            max_docs=_positive_int(data.get('max_docs'), 100, 100000, 'max_docs'),
            target_case_count=_positive_int(data.get('target_case_count'), 100, 100000, 'target_case_count'),
        )

    def to_dict(self) -> dict[str, Any]:
        return {'kb_id': self.kb_id, 'max_docs': self.max_docs, 'target_case_count': self.target_case_count}


def select_docs(
    ctx: Any,
    inputs: Mapping[str, object],
    kb_client: KnowledgeBaseClient | None = None,
) -> Mapping[str, object]:
    source_config = inputs.get('source_config')
    if not isinstance(source_config, Mapping):
        raise ValueError('source_config must be a mapping')
    params = SelectDocsParams.from_dict(dict(source_config))
    client = kb_client or KnowledgeBaseClient()
    rows = client.list_documents(params.kb_id)
    selected = select_doc_rows(rows, params)
    return {'selected_docs': selected_docs_payload(params, rows, selected)}


def select_doc_rows(rows: list[dict[str, Any]], params: SelectDocsParams) -> list[dict[str, Any]]:
    selected = rows[:params.max_docs]
    if not selected:
        raise ValueError('dataset.select_docs selected no documents')
    return selected


def selected_docs_payload(
    params: SelectDocsParams,
    rows: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    docs = [_doc_payload(row) for row in selected]
    return {
        'kb_id': params.kb_id,
        'docs': docs,
        'stats': {'matched': len(rows), 'selected': len(docs)},
        'params': params.to_dict(),
    }


def _doc_payload(row: dict[str, Any]) -> dict[str, Any]:
    group_counts = _int_map(row.get('group_counts'))
    return {
        'doc_id': str(row.get('doc_id') or ''),
        'filename': str(row.get('filename') or row.get('display_name') or row.get('doc_id') or ''),
        'file_type': str(row.get('file_type') or ''),
        'status': str(row.get('status') or row.get('upload_status') or ''),
        'group_counts': group_counts,
    }


def _int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, int] = {}
    for key, item in value.items():
        group = str(key or '').strip()
        if not group:
            continue
        try:
            count = int(item or 0)
        except (TypeError, ValueError):
            count = 0
        output[group] = max(count, 0)
    return output


def _positive_int(value: Any, default: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    try:
        output = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a positive integer') from exc
    if output < 1:
        raise ValueError(f'{name} must be a positive integer')
    if output > maximum:
        raise ValueError(f'{name} must be <= {maximum}')
    return output
