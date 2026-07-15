from __future__ import annotations

import json
from decimal import Decimal, ROUND_FLOOR
from math import isfinite
from collections.abc import Callable
from typing import Any, Mapping

from .llm_json import call_json


LANES = (
    ('entity_precision_easy', 'entity', 'precision', 'easy'),
    ('entity_precision_medium', 'entity', 'precision', 'medium'),
    ('entity_precision_hard', 'entity', 'precision', 'hard'),
    ('embedding_reasoning_easy', 'embedding', 'reasoning', 'easy'),
    ('embedding_reasoning_medium', 'embedding', 'reasoning', 'medium'),
    ('embedding_reasoning_hard', 'embedding', 'reasoning', 'hard'),
)
LANE_NAMES = tuple(lane[0] for lane in LANES)
REFERENCE_COUNTS = {'easy': 1, 'medium': 2, 'hard': 3}
SHARED_GENERATION_PROMPT = '''你将基于给定的 topic 和 reference materials，生成一条可用于 RAG 评测的 QA。

只能依据提供的 reference materials 生成内容；topic 仅用于选题引导，不能作为额外事实来源。
question 和 answer 都必须实质性使用全部提供的 references，不得只基于其中的子集生成内容。
question 必须指代明确、范围不超过 references，且应存在唯一、可判定的答案；不得生成开放式、主观性或并列拼接的多个问题。
question、answer、key_points 与 grading_guidance 使用 references 的主要语言，并保留必要的专有名词和缩写。
answer 必须是简短、完整、直接给出准确结论的陈述句；不要展示推理过程、附加背景信息或使用 Markdown。

只返回一个 JSON object，不要包含 Markdown 或其他字段：
{
  "question": "...",
  "answer": "...",
  "key_points": [{"statement": "...", "evidence_reference_ids": ["ref_1"]}],
  "grading_guidance": "...",
  "forbidden_claims": ["..."]
}

生成 1 至 5 个 key_points。每个 statement 必须是 answer 中的一个独立、完整、可单独判断的事实，并能被其 evidence_reference_ids 指向的 references 支持。
一个 statement 只能表达一个事实；将 answer 中必须命中的条件、对象、数值、时间、地点、因果关系拆成不同 statement。
不要加入背景、修饰语、解释性废话，或 answer 中没有的信息。
evidence_reference_ids 只能使用提供的短别名。每个 key point 至少关联一个 reference，所有 key points 的 evidence_reference_ids 并集必须覆盖全部提供的 references。

grading_guidance 只生成一条本 case 特有的指导性判分说明，说明答案怎样才算覆盖完整、条件正确或推理成立；不要机械复述 key_points。
forbidden_claims 生成 0 至 3 条。只列 references 明确否定、且本 case 容易出现的具体错误结论；不要写通用禁止语。'''
PRECISION_INSTRUCTION = '''- 必须围绕给定 topic 组织问题，并在 question 中显式出现该 topic 名称。
- question 只围绕一个对象和一个连贯的问题目标。多个 references 可以共同补足唯一答案，但不能被拼接成多个并列子问。
- answer 的结论必须完全由 references 中可直接找到的事实组成。允许抽取、并列、去重和格式化整合直接事实。
- 不得要求或使用计算、比较、资格判断、时间先后判断、因果推断或其他新关系建立；'''
REASONING_INSTRUCTION = '''- 围绕给定 topic 选择问题；topic 是选题引导，references 是唯一事实依据。
- question 必须指向一个唯一、可判定的最终结论，而不能是多个并列问题。
- 最终结论不能只是任一 reference 中一句话的直接复述；必须将 references 中明确给出的事实、条件或关系进行闭合的归纳或推导后得出。
- 可以基于一个 reference 内的多个明确事实推导，也可以综合多个 references 推导；无论哪种情况，都必须实质性使用全部提供的 references。
- 不得依赖外部常识、主观判断、开放式总结或资料未建立的关系。'''


def qaplan_plan(ctx: Any, inputs: Mapping[str, object]) -> dict[str, object]:
    case_ids = _case_ids(ctx, 'qaplan_plan')
    source_config = _mapping(inputs.get('source_config'), 'source_config')
    kb_id = _text(source_config.get('kb_id'), 'kb_id')
    target_case_count = _positive_int(source_config.get('target_case_count'), 'target_case_count')
    if target_case_count != len(case_ids):
        raise ValueError('target_case_count must match runtime case partition count')

    ratios = _lane_ratios(inputs.get('qaplan_plan_params'))
    quotas = _allocate_quotas(target_case_count, ratios)
    chunks = _chunk_map(inputs.get('chunk'))
    clusters = _clusters(inputs.get('topic_discovery_manifest'), chunks)

    items: list[dict[str, object]] = []
    lane_summaries: list[dict[str, object]] = []
    for lane, cluster_type, question_type, difficulty in LANES:
        quota = quotas[lane]
        if quota == 0:
            lane_summaries.append({
                'lane': lane,
                'allocated_case_count': 0,
                'candidate_cluster_count': 0,
                'topic_capacity': 0,
                'selected_cluster_count': 0,
            })
            continue

        reference_count = REFERENCE_COUNTS[difficulty]
        candidates = [
            cluster
            for cluster in clusters
            if cluster['cluster_type'] == cluster_type and cluster['chunk_count'] >= reference_count
        ]
        capacity = sum(len(cluster['topics']) for cluster in candidates)
        if capacity < quota:
            raise ValueError(f'{lane} quota {quota} exceeds topic capacity {capacity}')

        selected = _select_topics(candidates, quota)
        selected_clusters = {cluster['cluster_id'] for cluster, _, _ in selected}
        lane_summaries.append({
            'lane': lane,
            'allocated_case_count': quota,
            'candidate_cluster_count': len(candidates),
            'topic_capacity': capacity,
            'selected_cluster_count': len(selected_clusters),
        })

        for cluster, topic, selection_round in selected:
            references = _references(cluster, reference_count, chunks)
            items.append({
                'plan_item_id': f'qaplan_item_{len(items) + 1:06d}',
                'lane': lane,
                'question_type': question_type,
                'difficulty': difficulty,
                'cluster_id': cluster['cluster_id'],
                'cluster_type': cluster_type,
                'topic': topic,
                'references': references,
                'selection_round': selection_round,
            })

    payload = {
        'source': {'kb_id': kb_id},
        'items': items,
        'stats': {
            'target_case_count': target_case_count,
            'planned_case_count': len(items),
            'lane_summaries': lane_summaries,
        },
        'params': {
            'lane_ratios': ratios,
            'resolved_lane_quotas': quotas,
            'lane_order': list(LANE_NAMES),
        },
    }
    return {'qaplan_plan': payload}


def qaplan_spec(ctx: Any, inputs: Mapping[str, object]) -> dict[str, object]:
    case_ids = _case_ids(ctx, 'qaplan_spec')
    output_key = getattr(ctx, 'output_key_by_name', {}).get('qaplan_spec')
    case_id = _text(getattr(output_key, 'partition', None), 'qaplan_spec output partition')
    if case_id not in case_ids:
        raise ValueError('preparation output partition must belong to runtime case partitions')

    qaplan = _mapping(inputs.get('qaplan_plan'), 'qaplan_plan')
    items = qaplan.get('items')
    if not isinstance(items, list):
        raise ValueError('qaplan.items must be a list')
    stats = _mapping(qaplan.get('stats'), 'qaplan.stats')
    target_case_count = _positive_int(stats.get('target_case_count'), 'qaplan.stats.target_case_count')
    planned_case_count = _positive_int(stats.get('planned_case_count'), 'qaplan.stats.planned_case_count')
    if target_case_count != planned_case_count or planned_case_count != len(items):
        raise ValueError('target_case_count, planned_case_count, and qaplan.items must match')
    if len(items) != len(case_ids):
        raise ValueError('qaplan.items count must match runtime case partition count')
    source = _mapping(qaplan.get('source'), 'qaplan_plan.source')

    item = _mapping(items[case_ids.index(case_id)], 'qaplan.items[]')
    question_type = _choice(item.get('question_type'), ('precision', 'reasoning'), 'question_type')
    difficulty = _choice(item.get('difficulty'), ('easy', 'medium', 'hard'), 'difficulty')
    topic = _text(item.get('topic'), 'topic')
    references = _build_references(item.get('references'), difficulty)
    instruction = _instruction(question_type, topic, len(references))

    preparation = {
        'id': case_id,
        'question_type': question_type,
        'difficulty': difficulty,
        'instruction': instruction,
        'topic': topic,
        'source': {'kb_id': _text(source.get('kb_id'), 'qaplan_plan.source.kb_id')},
        'qaplan': {
            'plan_item_id': _text(item.get('plan_item_id'), 'plan_item_id'),
            'lane': _text(item.get('lane'), 'lane'),
            'cluster_id': _text(item.get('cluster_id'), 'cluster_id'),
            'cluster_type': _choice(item.get('cluster_type'), ('entity', 'embedding'), 'cluster_type'),
            'selection_round': _positive_int(item.get('selection_round'), 'selection_round'),
        },
        'references': references,
    }
    return {'qaplan_spec': preparation}


def qaplan_generate(
    ctx: Any,
    inputs: Mapping[str, object],
    llm_complete: Callable[[str], str] | None = None,
) -> dict[str, object]:
    output_key = getattr(ctx, 'output_key_by_name', {}).get('case')
    case_id = _text(getattr(output_key, 'partition', None), 'case output partition')
    preparation = _mapping(inputs.get('qaplan_spec'), 'qaplan_spec')
    if _text(preparation.get('id'), 'qaplan_spec.id') != case_id:
        raise ValueError('qaplan_spec.id must match case output partition')

    question_type = _choice(preparation.get('question_type'), ('precision', 'reasoning'), 'question_type')
    difficulty = _choice(preparation.get('difficulty'), ('easy', 'medium', 'hard'), 'difficulty')
    instruction = _text(preparation.get('instruction'), 'instruction')
    topic = _text(preparation.get('topic'), 'topic')
    references = _build_references(preparation.get('references'), difficulty)
    source = _mapping(preparation.get('source'), 'qaplan_spec.source')
    _mapping(preparation.get('qaplan'), 'qaplan_spec.qaplan')

    run_config = _mapping(inputs.get('run_config'), 'run_config')
    llm_config = _mapping(run_config.get('llm_config'), 'run_config.llm_config')
    complete = llm_complete or _llm_complete(llm_config)
    reference_aliases = {f'ref_{index}': item['chunk_id'] for index, item in enumerate(references, 1)}
    generated = call_json(
        complete,
        _generation_prompt(instruction, topic, references),
        lambda value: _generated_fields(value, reference_aliases),
        repair_instruction=lambda error: _generation_repair_instruction(error, reference_aliases),
    )

    return {'case': {
        'id': case_id,
        'question_type': question_type,
        'difficulty': difficulty,
        'question': generated['question'],
        'answer': generated['answer'],
        'key_points': [
            {
                'statement': item['statement'],
                'evidence_chunk_ids': [reference_aliases[reference_id] for reference_id in item['evidence_reference_ids']],
            }
            for item in generated['key_points']
        ],
        'grading_guidance': generated['grading_guidance'],
        'forbidden_claims': generated['forbidden_claims'],
        'reference_context': {item['chunk_id']: item['text'] for item in references},
        'reference_chunk_ids': [item['chunk_id'] for item in references],
        'reference_doc_ids': list(dict.fromkeys(item['doc_id'] for item in references)),
        'source_preparation': {'kb_id': _text(source.get('kb_id'), 'qaplan_spec.source.kb_id')},
    }}


def qaplan_generate_manifest(ctx: Any, inputs: Mapping[str, object]) -> dict[str, object]:
    values = inputs.get('cases')
    if not isinstance(values, tuple) or not values:
        raise ValueError('cases must be a non-empty partitioned tuple')
    cases = []
    for index, raw in enumerate(values, 1):
        case = _mapping(raw, f'cases[{index}]')
        reference_chunk_ids = _string_list(case.get('reference_chunk_ids'), 'reference_chunk_ids')
        key_points = _key_points(case.get('key_points'), 'evidence_chunk_ids')
        evidence_chunk_ids = {chunk_id for item in key_points for chunk_id in item['evidence_chunk_ids']}
        if not evidence_chunk_ids.issubset(set(reference_chunk_ids)):
            raise ValueError('key_points evidence_chunk_ids must reference case reference_chunk_ids')
        if evidence_chunk_ids != set(reference_chunk_ids):
            raise ValueError('key_points evidence_chunk_ids must cover case reference_chunk_ids')
        cases.append({
            'id': _text(case.get('id'), 'id'),
            'question_type': _choice(case.get('question_type'), ('precision', 'reasoning'), 'question_type'),
            'difficulty': _choice(case.get('difficulty'), ('easy', 'medium', 'hard'), 'difficulty'),
            'key_point_count': len(key_points),
            'reference_count': len(reference_chunk_ids),
        })
    if len({item['id'] for item in cases}) != len(cases):
        raise ValueError('id values must be unique')
    return {'qaplan_generate_manifest': {
        'cases': cases,
        'stats': {
            'case_count': len(cases),
            'question_type_counts': {
                name: sum(1 for item in cases if item['question_type'] == name)
                for name in ('precision', 'reasoning')
            },
            'difficulty_counts': {
                name: sum(1 for item in cases if item['difficulty'] == name)
                for name in ('easy', 'medium', 'hard')
            },
        },
    }}


def _case_ids(ctx: Any, operation: str) -> tuple[str, ...]:
    values = getattr(ctx, 'case_ids', ())
    if not isinstance(values, tuple) or not values:
        raise ValueError(f'{operation} requires runtime case_ids')
    if any(not isinstance(case_id, str) or not case_id.strip() for case_id in values):
        raise ValueError('runtime case_ids must contain non-empty strings')
    if len(set(values)) != len(values):
        raise ValueError('runtime case_ids must be unique')
    return values


def _lane_ratios(value: object) -> dict[str, object]:
    params = _mapping(value if value is not None else {}, 'qaplan_plan_params')
    raw = params.get('lane_ratios', {})
    raw = _mapping(raw, 'lane_ratios')
    ratios: dict[str, object] = {}
    total = Decimal('0')
    for lane in LANE_NAMES:
        current = raw.get(lane, 1)
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError('lane_ratios values must be numbers')
        if isinstance(current, float) and not isfinite(current):
            raise ValueError('lane_ratios values must be finite')
        ratio = Decimal(str(current))
        if ratio < 0:
            raise ValueError('lane_ratios values must be non-negative')
        ratios[lane] = current
        total += ratio
    if total <= 0:
        raise ValueError('lane_ratios must contain a positive value')
    return ratios


def _allocate_quotas(target_case_count: int, ratios: Mapping[str, object]) -> dict[str, int]:
    values = {lane: Decimal(str(ratios[lane])) for lane in LANE_NAMES}
    total = sum(values.values(), Decimal('0'))
    raw = {lane: Decimal(target_case_count) * values[lane] / total for lane in LANE_NAMES}
    quotas = {lane: int(raw[lane].to_integral_value(rounding=ROUND_FLOOR)) for lane in LANE_NAMES}
    remainder = target_case_count - sum(quotas.values())
    ordered = sorted(range(len(LANE_NAMES)), key=lambda index: (-(raw[LANE_NAMES[index]] - quotas[LANE_NAMES[index]]), index))
    for index in ordered[:remainder]:
        quotas[LANE_NAMES[index]] += 1
    return quotas


def _chunk_map(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, tuple):
        raise ValueError('chunk input must be a partitioned tuple')
    chunks: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(value):
        item = _mapping(raw, f'chunk[{index}]')
        if not isinstance(item.get('available'), bool):
            raise ValueError('chunk.available must be boolean')
        chunk_id = _text(item.get('chunk_id'), 'chunk_id')
        if not item['available']:
            continue
        if chunk_id in chunks:
            raise ValueError('available chunk_id values must be unique')
        chunks[chunk_id] = {
            'chunk_id': chunk_id,
            'doc_id': _text(item.get('doc_id'), 'doc_id'),
            'text': item.get('text') if isinstance(item.get('text'), str) else '',
        }
    return chunks


def _clusters(value: object, chunks: Mapping[str, Mapping[str, str]]) -> list[dict[str, object]]:
    manifest = _mapping(value, 'topic_discovery_manifest')
    raw_clusters = manifest.get('clusters')
    if not isinstance(raw_clusters, list):
        raise ValueError('topic_discovery_manifest.clusters must be a list')
    output: list[dict[str, object]] = []
    cluster_ids: set[str] = set()
    for index, raw in enumerate(raw_clusters):
        item = _mapping(raw, f'clusters[{index}]')
        cluster_id = _text(item.get('cluster_id'), 'cluster_id')
        if cluster_id in cluster_ids:
            raise ValueError('cluster_id values must be unique')
        cluster_ids.add(cluster_id)
        cluster_type = _choice(item.get('cluster_type'), ('entity', 'embedding'), 'cluster_type')
        topics = _text_list(item.get('topics'), 'topics')
        chunk_ids = _text_list(item.get('chunk_ids'), 'chunk_ids')
        chunk_count = _positive_int(item.get('chunk_count'), 'chunk_count')
        if chunk_count != len(chunk_ids):
            raise ValueError('chunk_count must match chunk_ids length')
        if any(chunk_id not in chunks for chunk_id in chunk_ids):
            raise ValueError('cluster references a missing or unavailable chunk')
        output.append({
            'cluster_id': cluster_id,
            'cluster_type': cluster_type,
            'topics': topics,
            'chunk_ids': chunk_ids,
            'chunk_count': chunk_count,
        })
    return output


def _select_topics(candidates: list[dict[str, object]], quota: int) -> list[tuple[dict[str, object], str, int]]:
    selected: list[tuple[dict[str, object], str, int]] = []
    topic_index = 0
    while len(selected) < quota:
        for cluster in candidates:
            topics = cluster['topics']
            if topic_index < len(topics):
                selected.append((cluster, topics[topic_index], topic_index + 1))
                if len(selected) == quota:
                    return selected
        topic_index += 1
    return selected


def _references(cluster: Mapping[str, object], count: int, chunks: Mapping[str, Mapping[str, str]]) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    for chunk_id in cluster['chunk_ids'][:count]:
        chunk = chunks[chunk_id]
        if not chunk['text'].strip():
            raise ValueError('referenced chunk text must be non-empty')
        references.append(dict(chunk))
    return references


def _build_references(value: object, difficulty: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError('references must be a list')
    expected = REFERENCE_COUNTS[difficulty]
    if len(value) != expected:
        raise ValueError(f'references count must match {difficulty}')
    output: list[dict[str, str]] = []
    for index, raw in enumerate(value):
        item = _mapping(raw, f'references[{index}]')
        output.append({
            'chunk_id': _text(item.get('chunk_id'), 'reference chunk_id'),
            'doc_id': _text(item.get('doc_id'), 'reference doc_id'),
            'text': _text(item.get('text'), 'reference text'),
        })
    return output


def _instruction(question_type: str, topic: str, reference_count: int) -> str:
    if question_type == 'precision':
        return PRECISION_INSTRUCTION
    return REASONING_INSTRUCTION


def _generation_prompt(instruction: str, topic: str, references: list[dict[str, str]]) -> str:
    materials = '\n\n'.join(
        f'<reference id="ref_{index}">\n{item["text"]}\n</reference>'
        for index, item in enumerate(references, 1)
    )
    return (
        f'{instruction}\n\n'
        f'{SHARED_GENERATION_PROMPT}\n\n'
        f'Topic: {topic}\n\n'
        f'Reference materials:\n{materials}'
    )


def _generation_repair_instruction(
    error: Exception,
    reference_aliases: Mapping[str, str],
) -> str:
    aliases = ', '.join(sorted(reference_aliases))
    return (
        '上一份 JSON 未通过校验，请重新生成完整 JSON，不要解释或复述失败内容。\n'
        f'校验错误：{error}\n'
        f'硬性要求：key_points 必须有 1 至 5 条；evidence_reference_ids 只能使用 {aliases}，'
        f'且所有 key_points 的引用并集必须覆盖 {aliases}。'
    )


def _generated_fields(raw: object, reference_aliases: Mapping[str, str]) -> dict[str, object]:
    value = raw if isinstance(raw, Mapping) else json.loads(str(raw))
    if not isinstance(value, Mapping):
        raise ValueError('LLM output must be a JSON object')
    key_points = _key_points(value.get('key_points'), 'evidence_reference_ids')
    evidence_reference_ids = {reference_id for item in key_points for reference_id in item['evidence_reference_ids']}
    expected_reference_ids = set(reference_aliases)
    if not evidence_reference_ids.issubset(expected_reference_ids):
        raise ValueError('key_points evidence_reference_ids must reference provided references')
    if evidence_reference_ids != expected_reference_ids:
        raise ValueError('key_points evidence_reference_ids must cover all provided references')
    return {
        'question': _text(value.get('question'), 'generated question'),
        'answer': _text(value.get('answer'), 'generated answer'),
        'key_points': key_points,
        'grading_guidance': _text(value.get('grading_guidance'), 'generated grading_guidance'),
        'forbidden_claims': _string_list(value.get('forbidden_claims'), 'generated forbidden_claims', minimum=0, maximum=3),
    }


def _key_points(value: object, evidence_field: str) -> list[dict[str, list[str] | str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise ValueError('key_points must contain 1 to 5 items')
    points = []
    for raw in value:
        item = _mapping(raw, 'key_points[]')
        points.append({
            'statement': _text(item.get('statement'), 'key_points[].statement'),
            evidence_field: _string_list(item.get(evidence_field), f'key_points[].{evidence_field}'),
        })
    return points


def _llm_complete(llm_config: Mapping[str, object]) -> Callable[[str], str]:
    from evo.llm import LazyLLMClient

    return LazyLLMClient(llm_config=llm_config)


def _model_name(llm_config: Mapping[str, object]) -> str:
    value = llm_config.get('evo_llm')
    return value.get('model', '') if isinstance(value, Mapping) else ''


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f'{name} must be a mapping')
    return value


def _text_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f'{name} must be a non-empty list')
    return [_text(item, name) for item in value]


def _string_list(value: object, name: str, minimum: int = 1, maximum: int | None = None) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum or (maximum is not None and len(value) > maximum):
        raise ValueError(f'{name} must contain {minimum} to {maximum or "more"} non-empty strings')
    return [_text(item, name) for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _choice(value: object, choices: tuple[str, ...], name: str) -> str:
    item = _text(value, name)
    if item not in choices:
        raise ValueError(f'{name} is invalid')
    return item
