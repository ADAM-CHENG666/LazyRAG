from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import re

from ... import validate_id
from ...artifact_runtime.evo import catalog as C
from .kb_client import KnowledgeBaseClient
from .models import chunk_from_docnode

DEFAULT_ALLOWED_TYPES = ('text', 'paragraph', 'table', 'formula', 'equation', 'unknown')
DEFAULT_MAX_SCAN_DOCS_PER_KB = 10_000
DEFAULT_MAX_SCAN_CHUNKS = 100_000
CHUNK_PARTITION_PATTERN = re.compile(r'^chunk_\d{4,}$')


@dataclass(frozen=True)
class BuildChunksParams:
    groups: list[str]
    allowed_types: list[str]
    excluded_chunks: list[dict[str, str]]
    max_scan_docs_per_kb: int
    max_scan_chunks: int

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'BuildChunksParams':
        groups = _string_list(data['groups'], 'groups') if 'groups' in data else ['block']
        for group in groups:
            validate_id(group, 'group')
        allowed_types = (
            normalized_types(data['allowed_types'])
            if 'allowed_types' in data
            else list(DEFAULT_ALLOWED_TYPES)
        )
        excluded_chunks = _excluded_chunks(data.get('excluded_chunks', []))
        return cls(
            groups=groups,
            allowed_types=allowed_types,
            excluded_chunks=excluded_chunks,
            max_scan_docs_per_kb=_positive_int(
                data.get('max_scan_docs_per_kb', DEFAULT_MAX_SCAN_DOCS_PER_KB), 'max_scan_docs_per_kb',
            ),
            max_scan_chunks=_positive_int(
                data.get('max_scan_chunks', DEFAULT_MAX_SCAN_CHUNKS), 'max_scan_chunks',
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'groups': list(self.groups),
            'allowed_types': list(self.allowed_types),
            'excluded_chunks': [dict(item) for item in self.excluded_chunks],
            'max_scan_docs_per_kb': self.max_scan_docs_per_kb,
            'max_scan_chunks': self.max_scan_chunks,
        }


def build_chunk_candidates(
    ctx: Any,
    inputs: Mapping[str, object],
    kb_client: KnowledgeBaseClient | None = None,
) -> Mapping[str, object]:
    selected = _mapping(inputs.get('selected_docs'), 'selected_docs')
    params = BuildChunksParams.from_dict(_mapping(inputs.get('build_chunks_params'), 'build_chunks_params'))
    allocation = _mapping(_mapping(inputs.get('import_cases_manifest'), 'import_cases_manifest').get('stats'), 'import_cases_manifest.stats')
    case_allocation = _mapping(allocation.get('case_allocation'), 'import_cases_manifest.stats.case_allocation')
    auto_case_count = _non_negative_int(case_allocation.get('auto_case_count'), 'auto_case_count')
    candidate_limit = (auto_case_count * 3 + 1) // 2
    if candidate_limit == 0:
        return {'build_chunk_candidates': _empty_candidate_payload(params)}
    docs = _docs(selected)
    kb_ids = _string_list(selected.get('kb_ids'), 'selected_docs.kb_ids')
    docs_by_kb = {kb_id: [doc for doc in docs if str(doc.get('kb_id') or '') == kb_id] for kb_id in kb_ids}
    for kb_id, items in docs_by_kb.items():
        if len(items) > params.max_scan_docs_per_kb:
            raise ValueError(
                f'max_scan_docs_per_kb exceeded for {kb_id}: {len(items)} > {params.max_scan_docs_per_kb}'
            )

    client = kb_client or KnowledgeBaseClient()
    excluded_by_kb = {
        kb_id: {item['chunk_id'] for item in params.excluded_chunks if item['kb_id'] == kb_id}
        for kb_id in kb_ids
    }
    counts = {
        kb_id: client.count_valid_chunks(
            kb_id,
            [str(doc.get('doc_id') or '') for doc in docs_by_kb[kb_id]],
            params.groups,
            params.allowed_types,
            params.max_scan_chunks,
            excluded_chunk_ids=excluded_by_kb[kb_id],
        )
        for kb_id in kb_ids
    }
    scanned_count = sum(_non_negative_int(item.get('scanned_count'), 'scanned_count') for item in counts.values())
    if scanned_count > params.max_scan_chunks:
        raise ValueError(f'max_scan_chunks exceeded: {scanned_count} > {params.max_scan_chunks}')

    effective_count = sum(_non_negative_int(item.get('effective_count'), 'effective_count') for item in counts.values())
    if effective_count == 0:
        raise ValueError('dataset.build_chunk_candidates effective capacity is zero')
    invalid_reasons = _sum_count_maps(counts.values(), 'invalid_count_by_reason')
    invalid_counts = {
        'filtered_by_type': sum(_sum_count_maps(counts.values(), 'filtered_count_by_type').values()),
        'empty_text': invalid_reasons.get('empty_text', 0),
        'missing_embedding': invalid_reasons.get('missing_embedding', 0),
        'invalid_embedding': invalid_reasons.get('invalid_embedding', 0),
    }
    manual_chunks = _manual_exclusions(counts, docs, params.excluded_chunks)
    if scanned_count != len(manual_chunks) + sum(invalid_counts.values()) + effective_count:
        raise ValueError('scanned chunk counts do not reconcile')

    chunks, allocation_stats = _allocate_candidates(
        client, docs_by_kb, kb_ids, counts, params, candidate_limit, excluded_by_kb,
    )
    summary = {
        'candidate_limit': candidate_limit,
        'scanned_doc_count': len(docs),
        'scanned_chunk_count': scanned_count,
        'effective_count': effective_count,
        'selected_count': len(chunks),
        'unselected_effective_count': effective_count - len(chunks),
        'shortfall_count': max(candidate_limit - len(chunks), 0),
    }
    return {'build_chunk_candidates': {
        'chunks': chunks,
        'summary': summary,
        'invalid_counts': invalid_counts,
        'manual_exclusions': {'chunk_count': len(manual_chunks), 'chunks': manual_chunks},
        'filter_options': {
            'available_groups': _available_groups(client, kb_ids, params.groups),
            'available_types': _available_types(counts, params.allowed_types),
        },
        'groups': [
            {
                'group': item['group'],
                'effective_count': item['effective_count'],
                'selected_count': item['selected_count'],
            }
            for item in allocation_stats['groups']
        ],
        'documents': _document_summaries(docs, allocation_stats['groups']),
        'params': params.to_dict(),
    }}


def build_chunks(
    ctx: Any,
    inputs: Mapping[str, object],
) -> Mapping[str, object]:
    candidates = _mapping(inputs.get('build_chunk_candidates'), 'build_chunk_candidates')
    chunks = _candidate_chunks(candidates)
    params = BuildChunksParams.from_dict(_mapping(candidates.get('params'), 'build_chunk_candidates.params'))
    partition = _output_partition(ctx, 'chunk')
    index = _slot_index(partition)
    payload = (
        {'available': True, **dict(chunks[index])}
        if index < len(chunks)
        else unavailable_chunk_payload(partition, params.groups[0])
    )
    return {'chunk': payload}


def build_chunks_manifest(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
    selected = _mapping(inputs.get('selected_docs'), 'selected_docs')
    allocation = _case_allocation(inputs.get('import_cases_manifest'))
    import_manifest = _mapping(inputs.get('import_cases_manifest'), 'import_cases_manifest')
    candidates = _mapping(inputs.get('build_chunk_candidates'), 'build_chunk_candidates')
    params = BuildChunksParams.from_dict(_mapping(candidates.get('params'), 'build_chunk_candidates.params'))
    chunks = _chunk_tuple(inputs.get('chunk'))
    partitions = _runtime_partitions(ctx)
    if len(partitions) != len(chunks):
        raise ValueError('dataset.build_chunks_manifest runtime partitions do not match chunk tuple')

    summary = _candidate_summary(candidates)
    invalid_counts = _four_counts(candidates.get('invalid_counts'), 'build_chunk_candidates.invalid_counts')
    manual = _mapping(candidates.get('manual_exclusions'), 'build_chunk_candidates.manual_exclusions')
    manual_chunk_count = _non_negative_int(manual.get('chunk_count'), 'manual_exclusions.chunk_count')
    _validate_candidate_counts(summary, invalid_counts, manual_chunk_count)

    manifest_chunks = [_manifest_chunk(partition, chunk) for partition, chunk in zip(partitions, chunks, strict=True)]
    available_count = sum(1 for chunk in manifest_chunks if chunk['available'])
    if available_count != summary['selected_count']:
        raise ValueError('available slot count does not match selected chunk count')
    selected_stats = _mapping(selected.get('stats'), 'selected_docs.stats')
    document_counts = {
        'discovered': _non_negative_int(selected_stats.get('discovered_count'), 'discovered_count'),
        'scanned': _non_negative_int(selected_stats.get('selected_count'), 'selected_count'),
        'excluded': _non_negative_int(selected_stats.get('excluded_count'), 'excluded_count'),
    }
    if document_counts['discovered'] != document_counts['scanned'] + document_counts['excluded']:
        raise ValueError('selected document counts do not reconcile')
    source = _mapping(import_manifest.get('source'), 'import_cases_manifest.source')
    warnings = []
    if (
        allocation['auto_case_count'] > 0
        and summary['effective_count'] > 0
        and summary['shortfall_count'] > 0
    ):
        warnings.append(
            f"chunk candidate capacity is short by {summary['shortfall_count']}; "
            f"selected {summary['selected_count']} of {summary['candidate_limit']}"
        )
    payload = {
        'source': {
            'kb_ids': list(selected.get('kb_ids') or []),
            'csv_present': bool(str(source.get('csv_path') or '').strip()),
            'case_counts': {
                'target': _non_negative_int(allocation.get('target_case_count'), 'target_case_count'),
                'imported': _non_negative_int(allocation.get('import_case_count'), 'import_case_count'),
                'automatic': _non_negative_int(allocation.get('auto_case_count'), 'auto_case_count'),
            },
        },
        'summary': {
            'document_counts': document_counts,
            'chunk_counts': {
                'scanned': summary['scanned_chunk_count'],
                'effective': summary['effective_count'],
                'selected': summary['selected_count'],
                'unselected_effective': summary['unselected_effective_count'],
                'candidate_target': summary['candidate_limit'],
                'shortfall': summary['shortfall_count'],
            },
            'invalid_counts': invalid_counts,
            'manual_exclusions': {
                'document_count': document_counts['excluded'],
                'chunk_count': manual_chunk_count,
            },
            'slots': {
                'total': len(manifest_chunks),
                'available': available_count,
                'placeholder': len(manifest_chunks) - available_count,
            },
        },
        'filter_options': json_value(_mapping(candidates.get('filter_options'), 'build_chunk_candidates.filter_options')),
        'groups': json_value(_list_of_mappings(candidates.get('groups'), 'build_chunk_candidates.groups')),
        'documents': json_value(_list_of_mappings(candidates.get('documents'), 'build_chunk_candidates.documents')),
        'chunks': manifest_chunks,
        'params': {'groups': list(params.groups), 'allowed_types': list(params.allowed_types)},
        'warnings': warnings,
    }
    return {'build_chunks_manifest': payload}


def _allocate_candidates(
    client: KnowledgeBaseClient,
    docs_by_kb: dict[str, list[Mapping[str, Any]]],
    kb_ids: list[str],
    counts: dict[str, Mapping[str, Any]],
    params: BuildChunksParams,
    candidate_limit: int,
    excluded_by_kb: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    group_stats = []
    remaining = candidate_limit
    seen_chunk_ids: set[str] = set()

    for group in params.groups:
        kb_capacities = {
            kb_id: sum(_group_doc_capacities(counts[kb_id], group).values())
            for kb_id in kb_ids
        }
        group_capacity = sum(kb_capacities.values())
        group_quota = min(remaining, group_capacity)
        kb_quotas = largest_remainder(group_quota, kb_capacities, kb_ids)
        kb_stats = []
        group_selected = 0

        for kb_id in kb_ids:
            doc_order = [str(doc.get('doc_id') or '') for doc in docs_by_kb[kb_id]]
            doc_capacities = _group_doc_capacities(counts[kb_id], group)
            doc_quotas = largest_remainder(kb_quotas[kb_id], doc_capacities, doc_order)
            docs_by_id = {str(doc.get('doc_id') or ''): dict(doc) for doc in docs_by_kb[kb_id]}
            doc_stats = []
            kb_selected = 0

            for doc_id in doc_order:
                quota = doc_quotas[doc_id]
                nodes = client.fetch_valid_chunks(
                    kb_id, doc_id, group, params.allowed_types, quota, order_by='stable_chunk_id_hash',
                    excluded_chunk_ids=excluded_by_kb[kb_id],
                ) if quota else []
                nodes = sorted(nodes, key=lambda node: (getattr(node, 'number', 0), str(getattr(node, 'uid', ''))))
                for node in nodes:
                    chunk = chunk_payload(node, kb_id, doc_id, group, docs_by_id[doc_id])
                    chunk_id = chunk['chunk_id']
                    if chunk_id in seen_chunk_ids:
                        raise ValueError(f'duplicate chunk_id: {chunk_id}')
                    seen_chunk_ids.add(chunk_id)
                    chunks.append(chunk)
                selected_count = len(nodes)
                kb_selected += selected_count
                doc_stats.append({
                    'doc_id': doc_id,
                    'effective_count': doc_capacities.get(doc_id, 0),
                    'quota': quota,
                    'selected_count': selected_count,
                })

            group_selected += kb_selected
            kb_stats.append({
                'kb_id': kb_id,
                'effective_count': kb_capacities[kb_id],
                'quota': kb_quotas[kb_id],
                'selected_count': kb_selected,
                'documents': doc_stats,
            })

        group_stats.append({
            'group': group,
            'effective_count': group_capacity,
            'quota': group_quota,
            'selected_count': group_selected,
            'knowledge_bases': kb_stats,
        })
        remaining -= group_quota

    return chunks, {
        'fallback_used': any(item['selected_count'] > 0 for item in group_stats[1:]),
        'groups': group_stats,
    }


def chunk_payload(node: Any, kb_id: str, doc_id: str, group: str, doc: dict[str, Any]) -> dict[str, Any]:
    chunk = chunk_from_docnode(node, kb_id=kb_id, doc_id=doc_id, group=group, doc=doc)
    return {
        'kb_id': kb_id,
        'chunk_id': chunk.chunk_id,
        'doc_id': chunk.source.doc_id,
        'filename': chunk.source.filename,
        'group': chunk.group,
        'type': normalized_type(chunk.type),
        'text': chunk.text,
        'embedding': json_value(chunk.embedding),
        'metadata': json_value(chunk.source.metadata),
    }


def unavailable_chunk_payload(partition: str, group: str) -> dict[str, Any]:
    return {
        'available': False,
        'chunk_id': f'unavailable:{partition}',
        'doc_id': '__unavailable__',
        'filename': '',
        'group': group,
        'type': 'placeholder',
        'text': 'Unavailable chunk placeholder.',
        'embedding': {'model': '', 'vector': []},
        'metadata': {'partition': partition, 'available': False},
    }


def largest_remainder(total: int, capacities: dict[str, int], order: list[str]) -> dict[str, int]:
    capacities = {key: max(int(capacities.get(key, 0)), 0) for key in order}
    quota = min(max(total, 0), sum(capacities.values()))
    if quota == 0:
        return dict.fromkeys(order, 0)

    capacity = sum(capacities.values())
    raw = {key: quota * capacities[key] / capacity for key in order}
    quotas = {key: int(raw[key]) for key in order}
    remaining = quota - sum(quotas.values())
    ranked = sorted(order, key=lambda key: (-(raw[key] - quotas[key]), order.index(key)))
    for key in ranked:
        if remaining == 0:
            break
        if quotas[key] < capacities[key]:
            quotas[key] += 1
            remaining -= 1
    return quotas


def _group_doc_capacities(result: Mapping[str, Any], group: str) -> dict[str, int]:
    capacities = _mapping(result.get('capacities'), 'count_valid_chunks.capacities')
    values = _mapping(capacities.get(group, {}), f'count_valid_chunks.capacities.{group}')
    return {str(doc_id): _non_negative_int(count, 'effective_count') for doc_id, count in values.items()}


def _sum_count_maps(results: Any, key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for result in results:
        values = _mapping(result.get(key), key)
        counts.update({str(name): _non_negative_int(count, key) for name, count in values.items()})
    return dict(counts)


def _empty_candidate_payload(params: BuildChunksParams) -> dict[str, Any]:
    return {
        'chunks': [],
        'summary': {
            'candidate_limit': 0,
            'scanned_doc_count': 0,
            'scanned_chunk_count': 0,
            'effective_count': 0,
            'selected_count': 0,
            'unselected_effective_count': 0,
            'shortfall_count': 0,
        },
        'invalid_counts': {
            'filtered_by_type': 0,
            'empty_text': 0,
            'missing_embedding': 0,
            'invalid_embedding': 0,
        },
        'manual_exclusions': {'chunk_count': 0, 'chunks': []},
        'filter_options': {
            'available_groups': list(params.groups),
            'available_types': list(params.allowed_types),
        },
        'groups': [],
        'documents': [],
        'params': params.to_dict(),
    }


def _excluded_chunks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError('excluded_chunks must be a list')
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f'excluded_chunks[{index}] must be a mapping')
        try:
            normalized = {
                key: validate_id(str(item.get(key) or '').strip(), f'excluded_chunks.{key}')
                for key in ('kb_id', 'doc_id', 'chunk_id')
            }
        except ValueError as exc:
            raise ValueError(f'excluded_chunks[{index}] contains an invalid reference') from exc
        key = (normalized['kb_id'], normalized['chunk_id'])
        if key in seen:
            raise ValueError('excluded_chunks must contain unique (kb_id, chunk_id) references')
        seen.add(key)
        result.append(normalized)
    return result


def _manual_exclusions(
    counts: Mapping[str, Mapping[str, Any]],
    docs: list[Mapping[str, Any]],
    configured: list[Mapping[str, str]],
) -> list[dict[str, str]]:
    docs_by_key = {
        (str(doc.get('kb_id') or ''), str(doc.get('doc_id') or '')): doc
        for doc in docs
    }
    result = []
    seen: set[tuple[str, str]] = set()
    configured_docs = {
        (item['kb_id'], item['chunk_id']): item['doc_id']
        for item in configured
    }
    for kb_id, count in counts.items():
        values = count.get('manual_exclusions', [])
        if not isinstance(values, list):
            raise ValueError('count_valid_chunks.manual_exclusions must be a list')
        for item in values:
            value = _mapping(item, 'count_valid_chunks.manual_exclusions[]')
            chunk_id = str(value.get('chunk_id') or '')
            key = (kb_id, chunk_id)
            if key in seen:
                raise ValueError(f'duplicate chunk_id: {chunk_id}')
            seen.add(key)
            doc_id = str(value.get('doc_id') or '')
            if expected_doc_id := configured_docs.get(key):
                if doc_id != expected_doc_id:
                    raise ValueError(
                        f'excluded chunk doc_id mismatch: {chunk_id} expected {expected_doc_id}, got {doc_id}'
                    )
            doc = docs_by_key.get((kb_id, doc_id), {})
            result.append({
                'kb_id': kb_id,
                'doc_id': doc_id,
                'chunk_id': chunk_id,
                'filename': str(doc.get('filename') or ''),
                'group': str(value.get('group') or ''),
                'type': normalized_type(value.get('type')),
            })
    return result


def _available_groups(client: Any, kb_ids: list[str], configured: list[str]) -> list[str]:
    values: list[str] = []
    list_groups = getattr(client, 'list_groups', None)
    if callable(list_groups):
        for kb_id in kb_ids:
            groups = list_groups(kb_id)
            if not isinstance(groups, list):
                raise ValueError('list_groups must return a list')
            values.extend(str(group).strip() for group in groups if str(group).strip())
    return list(dict.fromkeys([*values, *configured]))


def _available_types(counts: Mapping[str, Mapping[str, Any]], configured: list[str]) -> list[str]:
    observed = []
    for count in counts.values():
        values = count.get('observed_types', [])
        if not isinstance(values, list):
            raise ValueError('count_valid_chunks.observed_types must be a list')
        observed.extend(normalized_type(value) for value in values)
    return list(dict.fromkeys([*observed, *configured]))


def _document_summaries(
    docs: list[Mapping[str, Any]],
    group_stats: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], tuple[int, int]] = {}
    group_order = [str(group.get('group') or '') for group in group_stats]
    for group in group_stats:
        group_name = str(group.get('group') or '')
        for kb in group.get('knowledge_bases', []):
            kb_value = _mapping(kb, 'allocation.knowledge_bases[]')
            kb_id = str(kb_value.get('kb_id') or '')
            for doc in kb_value.get('documents', []):
                doc_value = _mapping(doc, 'allocation.documents[]')
                by_key[(kb_id, str(doc_value.get('doc_id') or ''), group_name)] = (
                    _non_negative_int(doc_value.get('effective_count'), 'effective_count'),
                    _non_negative_int(doc_value.get('selected_count'), 'selected_count'),
                )
    result = []
    for doc in docs:
        kb_id = str(doc.get('kb_id') or '')
        doc_id = str(doc.get('doc_id') or '')
        groups = [
            {'group': group, 'effective_count': counts[0], 'selected_count': counts[1]}
            for group in group_order
            if (counts := by_key.get((kb_id, doc_id, group), (0, 0))) != (0, 0)
        ]
        result.append({
            'kb_id': kb_id,
            'doc_id': doc_id,
            'filename': str(doc.get('filename') or ''),
            'file_type': str(doc.get('file_type') or ''),
            'effective_count': sum(item['effective_count'] for item in groups),
            'selected_count': sum(item['selected_count'] for item in groups),
            'groups': groups,
        })
    return result


def _candidate_summary(candidates: Mapping[str, Any]) -> dict[str, int]:
    value = _mapping(candidates.get('summary'), 'build_chunk_candidates.summary')
    keys = (
        'candidate_limit', 'scanned_doc_count', 'scanned_chunk_count', 'effective_count',
        'selected_count', 'unselected_effective_count', 'shortfall_count',
    )
    return {key: _non_negative_int(value.get(key), f'summary.{key}') for key in keys}


def _four_counts(value: object, name: str) -> dict[str, int]:
    counts = _mapping(value, name)
    return {
        key: _non_negative_int(counts.get(key), f'{name}.{key}')
        for key in ('filtered_by_type', 'empty_text', 'missing_embedding', 'invalid_embedding')
    }


def _validate_candidate_counts(
    summary: Mapping[str, int],
    invalid_counts: Mapping[str, int],
    manual_chunk_count: int,
) -> None:
    if summary['selected_count'] + summary['unselected_effective_count'] != summary['effective_count']:
        raise ValueError('selected and unselected effective chunk counts do not reconcile')
    if summary['selected_count'] + summary['shortfall_count'] != summary['candidate_limit']:
        raise ValueError('selected and shortfall chunk counts do not reconcile')
    if summary['scanned_chunk_count'] != manual_chunk_count + sum(invalid_counts.values()) + summary['effective_count']:
        raise ValueError('scanned chunk counts do not reconcile')


def _manifest_chunk(partition: str, chunk: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'available': bool(chunk.get('available')),
        'kb_id': str(chunk.get('kb_id') or ''),
        'chunk_id': str(chunk.get('chunk_id') or ''),
        'doc_id': str(chunk.get('doc_id') or ''),
        'filename': str(chunk.get('filename') or ''),
        'group': str(chunk.get('group') or ''),
        'type': str(chunk.get('type') or ''),
        'partition': partition,
    }


def _list_of_mappings(value: object, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f'{name} must be a list')
    return [_mapping(item, f'{name}[]') for item in value]


def json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalized_types(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError('allowed_types must be a non-empty list of strings')
    normalized = [normalized_type(item) for item in value]
    if any(not item for item in normalized):
        raise ValueError('allowed_types must not contain empty values')
    if len(set(normalized)) != len(normalized):
        raise ValueError('allowed_types must contain unique values')
    return normalized


def normalized_type(value: Any) -> str:
    normalized = str(value or '').strip().lower()
    return 'unknown' if normalized in {'', 'unknown'} else normalized


def _docs(selected: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    docs = selected.get('docs')
    if not isinstance(docs, list):
        raise ValueError('selected_docs.docs must be a list')
    if not all(isinstance(doc, Mapping) for doc in docs):
        raise ValueError('selected_docs.docs must contain only mappings')
    return list(docs)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} must be a mapping')
    return value


def _output_partition(ctx: Any, output_name: str) -> str:
    key = getattr(ctx, 'output_key_by_name', {}).get(output_name) if ctx is not None else None
    partition = getattr(key, 'partition', '') or 'chunk_0001'
    _validate_chunk_partition(partition)
    return partition


def _slot_index(partition: str) -> int:
    return int(partition.rsplit('_', 1)[-1]) - 1


def _chunk_tuple(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, tuple):
        raise ValueError('chunk input must be a partitioned tuple')
    return tuple(_mapping(item, 'chunk[]') for item in value)


def _candidate_chunks(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    chunks = value.get('chunks')
    if not isinstance(chunks, list):
        raise ValueError('build_chunk_candidates.chunks must be a list')
    return tuple(_mapping(chunk, 'build_chunk_candidates.chunks[]') for chunk in chunks)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{name} must be a non-empty list')
    values = [str(item or '').strip() for item in value]
    if any(not item for item in values) or len(set(values)) != len(values):
        raise ValueError(f'{name} must contain unique non-empty strings')
    return values


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{name} must be a non-negative integer')
    return value


def _case_allocation(value: object) -> Mapping[str, Any]:
    manifest = _mapping(value, 'import_cases_manifest')
    stats = _mapping(manifest.get('stats'), 'import_cases_manifest.stats')
    allocation = _mapping(stats.get('case_allocation'), 'import_cases_manifest.stats.case_allocation')
    _non_negative_int(allocation.get('auto_case_count'), 'auto_case_count')
    return allocation


def _runtime_partitions(ctx: Any) -> tuple[str, ...]:
    partitions = sorted(
        ref.key.partition for ref in getattr(ctx, 'input_ref_by_key', {}).values()
        if getattr(ref.key, 'artifact_id', '') == C.DATASET_CHUNK and ref.key.partition
    )
    for partition in partitions:
        _validate_chunk_partition(partition)
    return tuple(partitions)


def _validate_chunk_partition(partition: str) -> None:
    if not CHUNK_PARTITION_PATTERN.fullmatch(partition):
        raise ValueError(f'invalid chunk partition: {partition!r}')
