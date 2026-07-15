from types import SimpleNamespace

import pytest

from evo.operations.dataset.qaplan import qaplan_spec


def _reference(index):
    return {
        'chunk_id': f'chunk-{index}',
        'doc_id': f'doc-{index}',
        'text': f'full text {index}',
    }


def _item(index, *, question_type='precision', difficulty='easy', topic='service level'):
    reference_count = {'easy': 1, 'medium': 2, 'hard': 3}[difficulty]
    prefix = 'entity' if question_type == 'precision' else 'embedding'
    return {
        'plan_item_id': f'qaplan_item_{index:06d}',
        'lane': f'{prefix}_{question_type}_{difficulty}',
        'question_type': question_type,
        'difficulty': difficulty,
        'cluster_id': f'{prefix}_000001',
        'cluster_type': prefix,
        'topic': topic,
        'references': [_reference(number) for number in range(index, index + reference_count)],
        'selection_round': 1,
    }


def _qaplan(items):
    return {
        'source': {'kb_id': 'kb-1'},
        'items': items,
        'stats': {
            'target_case_count': len(items),
            'planned_case_count': len(items),
        },
        'params': {},
    }


def _context(case_id, case_count):
    return SimpleNamespace(
        case_ids=tuple(f'case_{index:04d}' for index in range(1, case_count + 1)),
        output_key_by_name={'qaplan_spec': SimpleNamespace(partition=case_id)},
    )


def _spec(case_id, items):
    return qaplan_spec(_context(case_id, len(items)), {'qaplan_plan': _qaplan(items)})['qaplan_spec']


def test_qaplan_spec_uses_current_case_partition_index_without_reordering_items():
    items = [
        _item(1, question_type='precision', difficulty='easy', topic='first topic'),
        _item(2, question_type='reasoning', difficulty='medium', topic='second topic'),
    ]

    preparation = _spec('case_0002', items)

    assert preparation['id'] == 'case_0002'
    assert preparation['source'] == {'kb_id': 'kb-1'}
    assert preparation['topic'] == 'second topic'
    assert preparation['qaplan']['plan_item_id'] == 'qaplan_item_000002'
    assert preparation['qaplan']['lane'] == 'embedding_reasoning_medium'


def test_qaplan_spec_renders_precision_behavior_without_disclosing_qa_type_or_context_values():
    preparation = _spec('case_0001', [_item(1, question_type='precision', difficulty='medium', topic='服务等级')])

    assert preparation['instruction'] == (
        '- 必须围绕给定 topic 组织问题，并在 question 中显式出现该 topic 名称。\n'
        '- question 只围绕一个对象和一个连贯的问题目标。多个 references 可以共同补足唯一答案，但不能被拼接成多个并列子问。\n'
        '- answer 的结论必须完全由 references 中可直接找到的事实组成。允许抽取、并列、去重和格式化整合直接事实。\n'
        '- 不得要求或使用计算、比较、资格判断、时间先后判断、因果推断或其他新关系建立；'
    )
    assert '服务等级' not in preparation['instruction']
    assert '2 份参考资料' not in preparation['instruction']
    assert 'precision' not in preparation['instruction']


def test_qaplan_spec_renders_reasoning_behavior_without_disclosing_qa_type_or_context_values():
    preparation = _spec('case_0001', [_item(1, question_type='reasoning', difficulty='hard', topic='资源调度')])

    assert preparation['instruction'] == (
        '- 围绕给定 topic 选择问题；topic 是选题引导，references 是唯一事实依据。\n'
        '- question 必须指向一个唯一、可判定的最终结论，而不能是多个并列问题。\n'
        '- 最终结论不能只是任一 reference 中一句话的直接复述；必须将 references 中明确给出的事实、条件或关系进行闭合的归纳或推导后得出。\n'
        '- 可以基于一个 reference 内的多个明确事实推导，也可以综合多个 references 推导；无论哪种情况，都必须实质性使用全部提供的 references。\n'
        '- 不得依赖外部常识、主观判断、开放式总结或资料未建立的关系。'
    )
    assert '资源调度' not in preparation['instruction']
    assert '3 份参考资料' not in preparation['instruction']
    assert 'reasoning' not in preparation['instruction']


def test_qaplan_spec_difficulty_does_not_change_precision_instruction():
    easy = _spec('case_0001', [_item(1, question_type='precision', difficulty='easy', topic='same topic')])
    hard = _spec('case_0001', [_item(1, question_type='precision', difficulty='hard', topic='same topic')])

    assert easy['instruction'] == hard['instruction']


def test_qaplan_spec_preserves_full_references_and_trace_metadata():
    item = _item(1, question_type='reasoning', difficulty='medium')
    preparation = _spec('case_0001', [item])

    assert preparation['references'] == item['references']
    assert preparation['qaplan'] == {
        'plan_item_id': item['plan_item_id'],
        'lane': item['lane'],
        'cluster_id': item['cluster_id'],
        'cluster_type': item['cluster_type'],
        'selection_round': item['selection_round'],
    }


@pytest.mark.parametrize(
    ('qaplan', 'case_id', 'case_count', 'match'),
    [
        (
            {
                'items': [_item(1)],
                'stats': {'target_case_count': 2, 'planned_case_count': 1},
                'params': {},
            },
            'case_0001',
            1,
            'target_case_count.*planned_case_count.*items',
        ),
        (_qaplan([_item(1)]), 'case_0001', 2, 'items.*runtime.*partition'),
    ],
)
def test_qaplan_spec_rejects_plan_and_runtime_count_mismatches(qaplan, case_id, case_count, match):
    with pytest.raises(ValueError, match=match):
        qaplan_spec(_context(case_id, case_count), {'qaplan_plan': qaplan})


@pytest.mark.parametrize(
    ('item', 'match'),
    [
        ({**_item(1), 'references': []}, 'references.*easy'),
        ({**_item(1, difficulty='medium'), 'references': [_reference(1)]}, 'references.*medium'),
        ({**_item(1), 'topic': ''}, 'topic'),
        ({**_item(1), 'question_type': 'unsupported'}, 'question_type'),
    ],
)
def test_qaplan_spec_rejects_invalid_instruction_or_reference_inputs(item, match):
    with pytest.raises(ValueError, match=match):
        qaplan_spec(_context('case_0001', 1), {'qaplan_plan': _qaplan([item])})
