from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ... import validate_id
from .kb_client import KnowledgeBaseClient
from .models import chunk_from_docnode


DEFAULT_ALLOWED_TYPES = ('text', 'paragraph', 'table', 'formula', 'equation', 'unknown')
DEFAULT_MAX_SCAN_DOCS_PER_KB = 10_000
DEFAULT_MAX_SCAN_CHUNKS = 100_000


@dataclass(frozen=True)
class BuildChunksParams:
    groups: list[str]
    allowed_types: list[str]
    max_scan_docs_per_kb: int
    max_scan_chunks: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'BuildChunksParams':
        if 'excluded_chunks' in data:
            raise ValueError('excluded_chunks is replaced by editable candidate selection')
        groups = _ids(data.get('groups', ['block']), 'groups')
        allowed_types = _ids(data.get('allowed_types', DEFAULT_ALLOWED_TYPES), 'allowed_types')
        return cls(groups, allowed_types, _positive(data.get('max_scan_docs_per_kb', DEFAULT_MAX_SCAN_DOCS_PER_KB), 'max_scan_docs_per_kb'),
                   _positive(data.get('max_scan_chunks', DEFAULT_MAX_SCAN_CHUNKS), 'max_scan_chunks'))

    def to_dict(self) -> dict[str, Any]:
        return {'groups': list(self.groups), 'allowed_types': list(self.allowed_types),
                'max_scan_docs_per_kb': self.max_scan_docs_per_kb, 'max_scan_chunks': self.max_scan_chunks}


def build_chunk_candidates(ctx: Any, inputs: Mapping[str, object], kb_client: KnowledgeBaseClient | None = None) -> Mapping[str, object]:
    params = BuildChunksParams.from_dict(_mapping(inputs.get('build_chunks_params'), 'build_chunks_params'))
    auto_count = _auto_count(inputs.get('import_cases_manifest'))
    if auto_count == 0:
        return {'build_chunk_candidates': _empty_candidates()}
    docs = [item for item in _documents(inputs.get('selected_docs')) if item['included']]
    by_kb: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        by_kb.setdefault(doc['kb_id'], []).append(doc)
    client = kb_client or KnowledgeBaseClient()
    counts = {}
    for kb_id, values in by_kb.items():
        if len(values) > params.max_scan_docs_per_kb:
            raise ValueError(f'max_scan_docs_per_kb exceeded for {kb_id}')
        counts[kb_id] = client.count_valid_chunks(kb_id, [item['doc_id'] for item in values], params.groups,
                                                   params.allowed_types, params.max_scan_chunks)
    scanned = sum(_integer(value.get('scanned_count'), 'scanned_count') for value in counts.values())
    if scanned > params.max_scan_chunks:
        raise ValueError(f'max_scan_chunks exceeded: {scanned} > {params.max_scan_chunks}')
    effective = sum(_integer(value.get('effective_count'), 'effective_count') for value in counts.values())
    if effective == 0:
        raise ValueError('dataset.build_chunk_candidates effective capacity is zero')
    full = _read_effective_chunks(client, docs, counts, params)
    limit = (auto_count * 3 + 1) // 2
    quotas, selected_keys = _initial_selection(docs, counts, full, params.groups, limit)
    selected_index = {key: index for index, key in enumerate(selected_keys)}
    chunks = []
    for doc in docs:
        for group in params.groups:
            for item in full.get((doc['kb_id'], doc['doc_id'], group), []):
                key = item['kb_id'], item['chunk_id']
                chunks.append({**item, 'discovery_index': len(chunks), 'selected': key in selected_index,
                               'selection_index': selected_index.get(key)})
    return {'build_chunk_candidates': {
        'chunks': chunks,
        'quotas': quotas,
        'summary': {'scanned_chunk_count': scanned, 'effective_count': effective,
                    'selected_count': len(selected_keys), 'shortfall_count': max(limit - len(selected_keys), 0)},
    }}


def build_chunks(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
    values = _mapping(inputs.get('build_chunk_candidates'), 'build_chunk_candidates')
    selected = [dict(item) for item in _list(values.get('chunks'), 'build_chunk_candidates.chunks') if item.get('selected')]
    selected.sort(key=lambda item: _integer(item.get('selection_index'), 'selection_index'))
    partition = str(getattr(getattr(ctx, 'output_key_by_name', {}).get('chunk'), 'partition', '') or '')
    index = _partition_index(partition)
    if index < len(selected):
        return {'chunk': {'available': True, **selected[index]}}
    return {'chunk': {'available': False, 'chunk_id': f'unavailable:{partition}', 'doc_id': '__unavailable__',
                      'filename': '', 'group': '', 'type': 'placeholder', 'text': '', 'embedding': {}, 'metadata': {}}}


def build_chunks_manifest(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
    allocation = _allocation(inputs.get('import_cases_manifest'))
    summary = _mapping(_mapping(inputs.get('build_chunk_candidates'), 'build_chunk_candidates').get('summary'), 'summary')
    counts = {key: _integer(summary.get(f'{key}_count'), f'{key}_count')
              for key in ('scanned_chunk', 'effective', 'selected', 'shortfall')}
    slots = _list(inputs.get('chunk'), 'chunk')
    partitions = _partitions(ctx, len(slots))
    if len(partitions) != len(slots):
        raise ValueError('chunk partition tuple mismatch')
    items = [_manifest_chunk(partition, _mapping(slot, 'chunk item')) for partition, slot in zip(partitions, slots, strict=True)]
    if sum(item['available'] for item in items) != counts['selected']:
        raise ValueError('available slot count does not match selected count')
    warning = []
    if allocation['automatic'] and counts['effective'] and counts['shortfall']:
        warning.append(f"chunk candidate capacity is short by {counts['shortfall']}; selected {counts['selected']}")
    source = _mapping(_mapping(inputs.get('import_cases_manifest'), 'import_cases_manifest').get('source'), 'source')
    return {'build_chunks_manifest': {
        'source': {'csv_present': bool(str(source.get('csv_path') or '').strip()), 'case_counts': allocation},
        'summary': {'chunk_counts': {'scanned': counts['scanned_chunk'], 'effective': counts['effective'],
                                     'selected': counts['selected'], 'shortfall': counts['shortfall']}},
        'warnings': warning, 'chunks': items,
    }}


def validate_chunk_selection(value: Mapping[str, object]) -> None:
    chunks = _list(value.get('chunks'), 'chunks')
    quotas = _list(value.get('quotas'), 'quotas')
    selected = [item for item in chunks if isinstance(item, Mapping) and item.get('selected')]
    indexes = [item.get('selection_index') for item in selected]
    if sorted(indexes) != list(range(len(selected))):
        raise ValueError('selection_index must be contiguous')
    actual = {}
    for item in selected:
        key = (str(item.get('kb_id') or ''), str(item.get('doc_id') or ''), str(item.get('group') or ''))
        actual[key] = actual.get(key, 0) + 1
    expected = {(str(item.get('kb_id') or ''), str(item.get('doc_id') or ''), str(item.get('group') or '')):
                _integer(item.get('required'), 'quota.required') for item in quotas if isinstance(item, Mapping)}
    if actual != expected:
        raise ValueError('quota selection mismatch')


def _read_effective_chunks(client: Any, docs: list[dict[str, Any]], counts: Mapping[str, Mapping[str, Any]], params: BuildChunksParams):
    result = {}
    seen = set()
    for doc in docs:
        kb_id, doc_id = doc['kb_id'], doc['doc_id']
        capacities = _mapping(counts[kb_id].get('capacities'), 'capacities')
        for group in params.groups:
            capacity = _integer(_mapping(capacities.get(group, {}), f'capacities.{group}').get(doc_id, 0), 'capacity')
            nodes = client.fetch_valid_chunks(kb_id, doc_id, group, params.allowed_types, capacity, order_by='stable_chunk_id_hash') if capacity else []
            nodes = sorted(nodes, key=lambda node: (getattr(node, 'number', 0), str(getattr(node, 'uid', ''))))
            values = []
            for node in nodes:
                payload = _chunk_payload(node, kb_id, doc_id, group, doc)
                key = payload['kb_id'], payload['chunk_id']
                if key in seen:
                    raise ValueError(f'duplicate chunk id: {payload["chunk_id"]}')
                seen.add(key)
                values.append(payload)
            if len(values) != capacity:
                raise ValueError('effective chunk fetch does not match capacity')
            result[kb_id, doc_id, group] = values
    return result


def _initial_selection(docs, counts, full, groups, limit):
    remaining, selected, quotas = limit, [], []
    for group in groups:
        capacities = {(doc['kb_id'], doc['doc_id']): len(full.get((doc['kb_id'], doc['doc_id'], group), [])) for doc in docs}
        required = _largest_remainder(min(remaining, sum(capacities.values())), capacities)
        for doc in docs:
            key = doc['kb_id'], doc['doc_id']
            amount = required[key]
            quotas.append({'kb_id': key[0], 'doc_id': key[1], 'group': group, 'required': amount})
            values = sorted(full.get((key[0], key[1], group), []), key=lambda item: _hash(item['chunk_id']))[:amount]
            selected.extend((item['kb_id'], item['chunk_id']) for item in sorted(values, key=lambda item: item['chunk_id']))
        remaining -= sum(required.values())
    return quotas, selected


def _largest_remainder(total, capacities):
    if not total:
        return dict.fromkeys(capacities, 0)
    size = sum(capacities.values())
    raw = {key: total * value / size for key, value in capacities.items()}
    result = {key: int(value) for key, value in raw.items()}
    for key in sorted(capacities, key=lambda item: (-(raw[item] - result[item]), item)):
        if sum(result.values()) == total:
            break
        result[key] += 1
    return result


def _documents(value):
    return [dict(item) for item in _list(_mapping(value, 'selected_docs').get('documents'), 'selected_docs.documents')]


def _auto_count(value):
    return _integer(_allocation(value)['automatic'], 'auto_case_count')


def _allocation(value):
    raw = _mapping(_mapping(_mapping(value, 'import_cases_manifest').get('stats'), 'stats').get('case_allocation'), 'case_allocation')
    return {'target': _integer(raw.get('target_case_count'), 'target_case_count'),
            'imported': _integer(raw.get('import_case_count'), 'import_case_count'),
            'automatic': _integer(raw.get('auto_case_count'), 'auto_case_count')}


def _chunk_payload(node, kb_id, doc_id, group, doc):
    chunk = chunk_from_docnode(node, kb_id=kb_id, doc_id=doc_id, group=group, doc=doc)
    return {'kb_id': kb_id, 'doc_id': doc_id, 'chunk_id': chunk.chunk_id, 'filename': chunk.source.filename,
            'group': chunk.group, 'type': str(chunk.type or 'unknown'), 'text': chunk.text,
            'embedding': dict(chunk.embedding), 'metadata': dict(chunk.source.metadata)}


def _manifest_chunk(partition, value):
    return {key: value.get(key, '') for key in ('available', 'kb_id', 'doc_id', 'chunk_id', 'filename', 'group', 'type')} | {'partition': partition}


def _partitions(ctx, size):
    refs = getattr(ctx, 'input_ref_by_key', {})
    values = sorted((key.partition for key in refs if getattr(key, 'artifact_id', '') == 'dataset.chunk'), key=_partition_index)
    return values or [f'chunk_{index + 1:04d}' for index in range(size)]


def _partition_index(value):
    try:
        return int(str(value).rsplit('_', 1)[1]) - 1
    except (IndexError, ValueError):
        return 0


def _empty_candidates():
    return {'chunks': [], 'quotas': [], 'summary': {'scanned_chunk_count': 0, 'effective_count': 0,
                                                     'selected_count': 0, 'shortfall_count': 0}}


def _ids(value, name):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f'{name} must be a non-empty list')
    try:
        values = [validate_id(str(item).strip(), name) for item in value]
    except ValueError as exc:
        raise ValueError(f'{name} contains an invalid value') from exc
    if len(set(values)) != len(values):
        raise ValueError(f'{name} must be unique')
    return values


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} must be positive')
    return value


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{name} must be a non-negative integer')
    return value


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} must be a mapping')
    return value


def _list(value, name):
    if not isinstance(value, (list, tuple)):
        raise ValueError(f'{name} must be a list')
    return value


def _hash(value):
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()
