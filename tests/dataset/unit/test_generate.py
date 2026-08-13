import json
from types import SimpleNamespace

import pytest

from evo.operations.dataset.generate import generate, generate_manifest


def _ctx(): return SimpleNamespace(output_key_by_name={'case': SimpleNamespace(partition='case_0001')})
def _spec(**overrides):
    value={'id':'case_0001','mode':'generated','question_type':'precision','difficulty':'easy',
           'instruction':'Use references only.','topic':{'topic_id':'topic-1','name':'Warranty'},
           'qaplan':{'plan_item_id':'qaplan_item_000001','lane':'precision_easy'},
           'references':[{'kb_id':'kb-1','doc_id':'doc-1','chunk_id':'chunk-1','text':'Battery defects are covered.'}]}
    value.update(overrides); return value
def _response(**overrides):
    value={'question':'What is covered?','answer':'Battery defects are covered.','grading_guidance':'Check the covered defect.'}
    value.update(overrides); return json.dumps(value)
def _generate(spec=None, response=None):
    return generate(_ctx(), {'qaplan_spec':spec or _spec(),'run_config':{'llm_config':{}}}, llm_complete=lambda _: response or _response())['case']


def test_generate_uses_derived_topic_name_and_complete_references_in_prompt():
    """Only the read-only qaplan spec supplies topic and current material to generation."""
    prompts=[]
    generate(_ctx(), {'qaplan_spec':_spec(),'run_config':{'llm_config':{}}}, llm_complete=lambda prompt: prompts.append(prompt) or _response())
    assert 'Topic: Warranty' in prompts[0] and 'Battery defects are covered.' in prompts[0]


def test_generate_adds_complete_references_without_removing_legacy_outputs():
    """The new four-field reference list is additive to all existing downstream Case fields."""
    case=_generate()
    assert case['references'] == _spec()['references']
    assert case['reference_context'] == [{'chunk_id':'chunk-1','text':'Battery defects are covered.'}]
    assert case['reference_chunk_ids'] == ['chunk-1']
    assert case['reference_doc_ids'] == ['doc-1']
    assert case['source_preparation'] == {'kb_ids':['kb-1']}


def test_generate_imported_case_still_bypasses_llm_without_shape_conversion():
    """Imported Case remains an exact pass-through to preserve the external Case API."""
    imported={'id':'case_0001','question_type':'precision','difficulty':'easy','question':'Q','answer':'A','grading_guidance':'G','reference_chunk_ids':['chunk-1']}
    assert _generate({'id':'case_0001','mode':'imported','imported_case':imported}, AssertionError('must not call')) == imported


@pytest.mark.parametrize('spec', [_spec(topic='old string'), _spec(references=[{'kb_id':'','doc_id':'doc-1','chunk_id':'chunk-1','text':'x'}])])
def test_generate_rejects_legacy_topic_or_incomplete_reference(spec):
    """Generated Specs require structured Topic and complete four-field references."""
    with pytest.raises(ValueError): _generate(spec)


def test_generate_manifest_rejects_reference_contract_disagreement():
    """Optional complete references cannot disagree with established reference_chunk_ids."""
    case={'id':'case_0001','question_type':'precision','difficulty':'easy','reference_chunk_ids':['chunk-1'],
          'references':[{'kb_id':'kb-1','doc_id':'doc-1','chunk_id':'different','text':'x'}]}
    with pytest.raises(ValueError, match='references'):
        generate_manifest(None, {'cases':(case,), 'import_cases_manifest':{'stats':{'case_allocation':{'import_case_count':0,'auto_case_count':1}}}})
