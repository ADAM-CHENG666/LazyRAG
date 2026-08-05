from types import SimpleNamespace

import pytest

from evo.operations.dataset.qaplan import qaplan_manifest, qaplan_spec


def _ctx():
    return SimpleNamespace(case_ids=('case_0001',), output_key_by_name={'qaplan_spec': SimpleNamespace(partition='case_0001')})


def _inputs():
    return {'import_cases_manifest': {'stats': {'case_allocation': {
        'target_case_count': 1, 'import_case_count': 0, 'auto_case_count': 1,
        'assignments': {'case_0001': {'mode': 'generated'}},
    }}, 'details': []}, 'qaplan_plan': {'items': [{
        'case_id': 'case_0001', 'plan_item_id': 'qaplan_item_000001', 'lane': 'precision_easy',
        'question_type': 'precision', 'difficulty': 'easy', 'topic_id': 'topic-1',
    }]}, 'topic_discovery_manifest': {'topics': [{
        'topic_id': 'topic-1', 'name': 'Warranty', 'question_type': 'precision',
        'chunk_ids': ['chunk-1'], 'chunk_count': 1,
    }]}, 'chunk': ({'available': True, 'kb_id': 'kb-1', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1', 'text': 'Warranty covers defects.'},)}


def test_qaplan_spec_derives_complete_current_topic_snapshot():
    """Spec resolves the flat Topic ID into immutable display and generation material."""
    spec = qaplan_spec(_ctx(), _inputs())['qaplan_spec']
    assert spec['topic'] == {'topic_id': 'topic-1', 'name': 'Warranty'}
    assert spec['references'] == [{'kb_id': 'kb-1', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1', 'text': 'Warranty covers defects.'}]
    assert spec['qaplan'] == {'plan_item_id': 'qaplan_item_000001', 'lane': 'precision_easy'}


def test_qaplan_spec_rejects_missing_or_incompatible_current_topic():
    """A stale Topic ID, wrong type, or unavailable Chunk cannot produce a generated Case spec."""
    inputs = _inputs(); inputs['topic_discovery_manifest']['topics'][0]['question_type'] = 'reasoning'
    with pytest.raises(ValueError, match='topic_id'):
        qaplan_spec(_ctx(), inputs)


def test_qaplan_manifest_exposes_only_fixed_product_lane_summary():
    """Overview has fixed product lanes and no Cluster or ratio fields."""
    plan = {'stats': {'target_case_count': 1, 'import_case_count': 0, 'auto_case_count': 1, 'planned_case_count': 1,
                      'lane_summaries': [
                          {'lane': lane, 'allocated_case_count': int(lane == 'precision_easy'), 'eligible_topic_count': 1}
                          for lane in ('precision_easy','precision_medium','precision_hard','reasoning_easy','reasoning_medium','reasoning_hard')
                      ]}}
    result = qaplan_manifest(None, {**_inputs(), 'qaplan_plan': plan, 'qaplan_specs': ({'id': 'case_0001', 'mode': 'generated'},)})['qaplan_manifest']
    assert [item['lane'] for item in result['lane_summaries']] == ['precision_easy','precision_medium','precision_hard','reasoning_easy','reasoning_medium','reasoning_hard']
