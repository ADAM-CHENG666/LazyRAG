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

CHUNK_PAGE_SIZE = 200
DEFAULT_TARGET_CASE_COUNT = 100
DEFAULT_ALLOWED_TYPES = ('text', 'paragraph', 'table', 'formula', 'equation', 'unknown')
CHUNK_PARTITION_PATTERN = re.compile(r'^chunk_\d{4,}$')


@dataclass(frozen=True)
class DocGroupQuota:
    doc_id: str
    group: str
    quota: int


@dataclass(frozen=True)
class BuildChunksParams:
    groups: list[str]
    allowed_types: list[str]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'BuildChunksParams':
        groups = strings(data.get('groups')) or ['block']
        for group in groups:
            validate_id(group, 'group')
        allowed_types = normalized_types(data.get('allowed_types'), default=DEFAULT_ALLOWED_TYPES)
        return cls(groups=groups, allowed_types=allowed_types)

    def to_dict(self) -> dict[str, Any]:
        return {'groups': list(self.groups), 'allowed_types': list(self.allowed_types)}


def build_chunk_candidates(
    ctx: Any,
    inputs: Mapping[str, object],
    kb_client: KnowledgeBaseClient | None = None,
) -> Mapping[str, object]:
    selected = _mapping(inputs.get('selected_docs'), 'selected_docs')
    params = BuildChunksParams.from_dict(_mapping(inputs.get('build_chunks_params'), 'build_chunks_params'))
    docs = _docs(selected)
    target = target_chunk_count(selected)
    plan = sampling_plan(docs, params.groups, target)
    if not plan:
        raise ValueError('dataset.build_chunk_candidates sampling plan is empty')

    chunks, selection_stats = read_planned_chunks(
        kb_client or KnowledgeBaseClient(), selected, plan, params.allowed_types,
    )
    return {'build_chunk_candidates': {
        'chunks': chunks,
        'selection_stats': selection_stats,
        'target_chunk_count': target,
        'fallback_used': fallback_used_by_plan(plan, params.groups),
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
        dict(chunks[index])
        if index < len(chunks)
        else unavailable_chunk_payload(partition, params.groups[0])
    )
    return {'chunk': payload}


def build_chunks_manifest(ctx: Any, inputs: Mapping[str, object]) -> Mapping[str, object]:
    selected = _mapping(inputs.get('selected_docs'), 'selected_docs')
    candidates = _mapping(inputs.get('build_chunk_candidates'), 'build_chunk_candidates')
    params = BuildChunksParams.from_dict(_mapping(candidates.get('params'), 'build_chunk_candidates.params'))
    target = _positive_int(candidates.get('target_chunk_count'), 'build_chunk_candidates.target_chunk_count')

    chunks = _chunk_tuple(inputs.get('chunk'))
    partitions = _runtime_partitions(ctx)
    if len(chunks) != target:
        raise ValueError(f'dataset.build_chunks_manifest requires {target} chunk slots, got {len(chunks)}')
    if len(partitions) != len(chunks):
        raise ValueError('dataset.build_chunks_manifest runtime partitions do not match chunk tuple')

    fallback_used = bool(candidates.get('fallback_used'))
    warnings = build_warnings(sum(1 for chunk in chunks if chunk.get('available')), target, fallback_used)
    return {
        'build_chunks_manifest': built_chunks_payload(
            ctx, selected, chunks, partitions, target, fallback_used, warnings, params,
            normalize_selection_stats(_mapping(candidates.get('selection_stats'), 'build_chunk_candidates.selection_stats')),
        )
    }


def target_chunk_count(selected: Mapping[str, Any]) -> int:
    selected_params = selected.get('params') if isinstance(selected.get('params'), Mapping) else {}
    try:
        target_case_count = int(selected_params.get('target_case_count', DEFAULT_TARGET_CASE_COUNT))
    except (TypeError, ValueError):
        target_case_count = DEFAULT_TARGET_CASE_COUNT
    return (max(target_case_count, 1) * 3 + 1) // 2


def sampling_plan(docs: list[Mapping[str, Any]], groups: list[str], target: int) -> list[DocGroupQuota]:
    remaining = max(target, 0)
    plan: list[DocGroupQuota] = []
    doc_order = [str(doc.get('doc_id') or '') for doc in docs]
    for group in groups:
        if remaining <= 0:
            break
        capacities = group_capacities(docs, group)
        if not capacities:
            continue
        quotas = allocate_doc_quotas(remaining, capacities, doc_order)
        plan.extend(DocGroupQuota(doc_id, group, quota) for doc_id, quota in quotas.items() if quota > 0)
        remaining -= sum(quotas.values())
    return plan


def read_planned_chunks(
    client: KnowledgeBaseClient,
    selected: Mapping[str, Any],
    plan: list[DocGroupQuota],
    allowed_types: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kb_id = str(selected.get('kb_id') or '')
    docs = _docs(selected)
    by_id = {str(doc.get('doc_id') or ''): dict(doc) for doc in docs}
    chunks: list[dict[str, Any]] = []
    scanned_count = 0
    filtered_count_by_type: Counter[str] = Counter()
    for item in plan:
        accepted_count = 0
        for batch in client.iter_chunks(kb_id, [item.doc_id], [item.group], CHUNK_PAGE_SIZE):
            for node in batch:
                scanned_count += 1
                node_type = normalized_node_type(node)
                if node_type not in allowed_types:
                    filtered_count_by_type[node_type] += 1
                    continue
                chunks.append(chunk_payload(node, kb_id, item.doc_id, item.group, by_id.get(item.doc_id, {})))
                accepted_count += 1
                if accepted_count >= item.quota:
                    break
            if accepted_count >= item.quota:
                break
    return chunks, {
        'scanned_count': scanned_count,
        'accepted_count': len(chunks),
        'filtered_count_by_type': dict(filtered_count_by_type),
    }


def built_chunks_payload(
    ctx: Any,
    selected: Mapping[str, Any],
    chunks: tuple[Mapping[str, Any], ...],
    partitions: tuple[str, ...],
    target: int,
    fallback_used: bool,
    warnings: list[str],
    params: BuildChunksParams,
    selection_stats: dict[str, Any],
) -> dict[str, Any]:
    manifest_chunks = [
        {
            'available': bool(chunk.get('available')),
            'chunk_id': str(chunk.get('chunk_id') or ''),
            'doc_id': str(chunk.get('doc_id') or ''),
            'filename': str(chunk.get('filename') or ''),
            'group': str(chunk.get('group') or ''),
            'type': str(chunk.get('type') or ''),
            'partition': partition,
        }
        for partition, chunk in zip(partitions, chunks, strict=True)
    ]
    stats = chunk_stats(manifest_chunks)
    stats.update(selection_stats)
    stats.update({
        'target_chunk_count': target,
        'fallback_used': fallback_used,
        'warnings': list(warnings),
    })
    source = {'kb_id': str(selected.get('kb_id') or '')}
    selected_ref = selected_docs_ref(ctx)
    if selected_ref:
        source['selected_docs_ref'] = selected_ref
    return {
        'source': source,
        'chunks': manifest_chunks,
        'stats': stats,
        'params': params.to_dict(),
    }


def selected_docs_ref(ctx: Any) -> str:
    for ref in getattr(ctx, 'input_ref_by_key', {}).values():
        if getattr(getattr(ref, 'key', None), 'artifact_id', '') == C.DATASET_SELECTED_DOCS:
            return f'{ref.key.artifact_id}@v{ref.version}'
    return ''


def chunk_payload(node: Any, kb_id: str, doc_id: str, group: str, doc: dict[str, Any]) -> dict[str, Any]:
    chunk = chunk_from_docnode(node, kb_id=kb_id, doc_id=doc_id, group=group, doc=doc)
    return {
        'available': True,
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


def group_capacities(docs: list[Mapping[str, Any]], group: str) -> dict[str, int]:
    capacities: dict[str, int] = {}
    for doc in docs:
        doc_id = str(doc.get('doc_id') or '')
        if not doc_id:
            continue
        group_counts = doc.get('group_counts') if isinstance(doc.get('group_counts'), Mapping) else {}
        try:
            count = int(group_counts.get(group) or 0)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            capacities[doc_id] = count
    return capacities


def allocate_doc_quotas(total_quota: int, capacities: dict[str, int], doc_order: list[str]) -> dict[str, int]:
    total_capacity = sum(max(count, 0) for count in capacities.values())
    quota = min(max(total_quota, 0), total_capacity)
    if quota <= 0:
        return {}

    ordered = [doc_id for doc_id in doc_order if capacities.get(doc_id, 0) > 0]
    quotas: dict[str, int] = {}
    remainders: dict[str, float] = {}
    for doc_id in ordered:
        raw = quota * capacities[doc_id] / total_capacity
        assigned = min(int(raw), capacities[doc_id])
        quotas[doc_id] = assigned
        remainders[doc_id] = raw - assigned

    for doc_id in ordered:
        if sum(quotas.values()) >= quota:
            break
        if quotas[doc_id] == 0:
            quotas[doc_id] = 1

    while sum(quotas.values()) < quota:
        candidates = [doc_id for doc_id in ordered if quotas[doc_id] < capacities[doc_id]]
        if not candidates:
            break
        doc_id = max(candidates, key=lambda item: (remainders[item], capacities[item] - quotas[item],
                                                   -ordered.index(item)))
        quotas[doc_id] += 1

    return quotas


def fallback_used_by_plan(plan: list[DocGroupQuota], groups: list[str]) -> bool:
    if len(groups) < 2 or not plan:
        return False
    first_group = groups[0]
    return any(item.group != first_group for item in plan)


def build_warnings(chunk_count: int, target: int, fallback_used: bool) -> list[str]:
    warnings = []
    if chunk_count < target:
        warnings.append(f'chunk build produced {chunk_count} chunks, below target {target}; continuing')
    if fallback_used:
        warnings.append('fallback group sampling was used')
    return warnings


def chunk_stats(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    available_chunks = [chunk for chunk in chunks if chunk.get('available')]
    group_counts = Counter(str(chunk.get('group') or '') for chunk in available_chunks)
    doc_groups: dict[str, Counter] = {}
    filenames: dict[str, str] = {}
    for chunk in available_chunks:
        doc_id = str(chunk.get('doc_id') or '')
        doc_groups.setdefault(doc_id, Counter())[str(chunk.get('group') or '')] += 1
        filenames.setdefault(doc_id, str(chunk.get('filename') or ''))
    return {
        'chunk_count': len(available_chunks),
        'slot_count': len(chunks),
        'empty_count': len(chunks) - len(available_chunks),
        'doc_count': len(doc_groups),
        'group_counts': dict(group_counts),
        'doc_group_stats': [
            {'doc_id': doc_id, 'filename': filenames.get(doc_id, ''), 'total': sum(groups.values()), 'groups': dict(groups)}
            for doc_id, groups in sorted(doc_groups.items())
        ],
    }


def normalize_selection_stats(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        scanned_count = int(value.get('scanned_count'))
        accepted_count = int(value.get('accepted_count'))
    except (TypeError, ValueError) as exc:
        raise ValueError('chunk selection_stats counts must be integers') from exc
    filtered = value.get('filtered_count_by_type')
    if scanned_count < 0 or accepted_count < 0 or not isinstance(filtered, Mapping):
        raise ValueError('chunk selection_stats is invalid')
    filtered_counts: dict[str, int] = {}
    for key, count in filtered.items():
        try:
            normalized_count = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError('chunk selection_stats filtered counts must be integers') from exc
        if normalized_count < 0:
            raise ValueError('chunk selection_stats filtered counts must be non-negative')
        filtered_counts[normalized_type(key)] = normalized_count
    return {
        'scanned_count': scanned_count,
        'accepted_count': accepted_count,
        'filtered_count_by_type': filtered_counts,
    }


def json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def strings(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [text for item in items for text in (str(item or '').strip(),) if text]


def normalized_types(value: Any, *, default: tuple[str, ...]) -> list[str]:
    if value is None:
        return list(default)
    items = value if isinstance(value, list) else [value]
    if not items or not all(isinstance(item, str) for item in items):
        raise ValueError('allowed_types must be a non-empty list of strings')
    normalized = [normalized_type(item) for item in items]
    if any(not item for item in normalized):
        raise ValueError('allowed_types must not contain empty values')
    return list(dict.fromkeys(normalized))


def normalized_node_type(node: Any) -> str:
    metadata = getattr(node, 'metadata', {}) or {}
    if not isinstance(metadata, Mapping):
        return 'unknown'
    return normalized_type(metadata.get('type') or metadata.get('node_type'))


def normalized_type(value: Any) -> str:
    normalized = str(value or '').strip().lower()
    return 'unknown' if normalized in {'', 'unknown'} else normalized


def _docs(selected: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    docs = selected.get('docs')
    if not isinstance(docs, list) or not docs:
        raise ValueError('selected_docs.docs must be a non-empty list')
    return [doc for doc in docs if isinstance(doc, Mapping)]


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
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a positive integer') from exc
    if parsed < 1:
        raise ValueError(f'{name} must be a positive integer')
    return parsed


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
