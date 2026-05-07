from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from evo.agents.eval_qc import _enrich_computed_scores, _patch_gt_text_edge, run_eval_qc
from evo.domain import EDGE_IDS, JudgeRecord
from evo.harness.schemas import SCHEMAS
from evo.runtime.config import EvalQCConfig, load_config
from evo.runtime.session import create_session, session_scope
from evo.tools.eval_qc import (
    apply_hard_filter,
    apply_hard_rules,
    normalize_edge_output,
)


class _ScriptedLLM:
    def __init__(self, *responses: dict) -> None:
        self.responses = [json.dumps(response, ensure_ascii=False) for response in responses]
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.responses:
            return ''
        return self.responses.pop(0)


def _judge(*, score: float = 0.5) -> JudgeRecord:
    return JudgeRecord(
        trace_id='t1',
        answer_correctness=score,
        key=['k1'],
        hit_key=['k1'],
        reason=['r'],
        context_recall=0.0,
        doc_recall=0.0,
        retrieved_file=[],
        gt_file=[],
        retrieved_text=[],
        gt_text=['ctx'],
        generated_answer='ans',
        gt_answer='gt',
        faithfulness=0.0,
        human_verified=True,
    )


def test_apply_hard_filter_boundary() -> None:
    cfg = EvalQCConfig(a_ac_threshold=0.6)

    low_bad, low_tags, low_severity = apply_hard_filter(
        _judge(score=0.59),
        score_field='answer_correctness',
        config=cfg,
    )
    equal_bad, equal_tags, equal_severity = apply_hard_filter(
        _judge(score=0.6),
        score_field='answer_correctness',
        config=cfg,
    )
    high_bad, high_tags, high_severity = apply_hard_filter(
        _judge(score=0.61),
        score_field='answer_correctness',
        config=cfg,
    )

    assert low_bad is True
    assert low_tags == ['ac_low']
    assert low_severity == 'low'
    assert (equal_bad, equal_tags, equal_severity) == (False, [], None)
    assert (high_bad, high_tags, high_severity) == (False, [], None)


def test_apply_hard_filter_missing_or_invalid_score() -> None:
    cfg = EvalQCConfig(a_ac_threshold=0.6)

    missing_bad, missing_tags, missing_severity = apply_hard_filter(
        _judge(score=0.8),
        score_field='not_exists',
        config=cfg,
    )
    bool_score = _judge(score=0.8)
    bool_score.answer_correctness = True  # type: ignore[assignment]
    bool_bad, bool_tags, bool_severity = apply_hard_filter(
        bool_score,
        score_field='answer_correctness',
        config=cfg,
    )

    assert (missing_bad, missing_tags, missing_severity) == (True, ['ac_missing'], 'high')
    assert (bool_bad, bool_tags, bool_severity) == (True, ['ac_missing'], 'high')


def test_apply_hard_rules_empty_inputs() -> None:
    tags = apply_hard_rules(
        query='',
        gt_answer=' ',
        gt_text=[],
        key_points=[],
    )

    assert set(tags) == {
        'query_empty',
        'gt_answer_empty',
        'gt_text_empty',
        'key_points_empty',
    }


def test_normalize_edge_output_invalid_and_clamp() -> None:
    assert normalize_edge_output('bad_payload') == ('', 0.0, 'invalid edge payload')

    high_id, high_score, high_reason = normalize_edge_output(
        {'id': 'query_to_gt_answer', 'score': 2.7, 'reason': 'too high'},
    )
    low_id, low_score, _ = normalize_edge_output(
        {'id': 'query_to_gt_text', 'score': -1, 'reason': 'too low'},
    )
    invalid_id, invalid_score, _ = normalize_edge_output(
        {'id': 'query_to_key_points', 'score': 'nope', 'reason': 'invalid'},
    )

    assert (high_id, high_score, high_reason) == ('query_to_gt_answer', 1.0, 'too high')
    assert (low_id, low_score) == ('query_to_gt_text', 0.0)
    assert (invalid_id, invalid_score) == ('query_to_key_points', 0.0)


def test_normalize_edge_output_uses_direct_score_for_gt_text_edge() -> None:
    edge_id, score, reason = normalize_edge_output({
        'id': 'gt_text_to_gt_answer',
        'score': 0.17,
        'claims': [
            {'text': 'a', 'score': 0.95},
            {'text': 'b', 'score': 0.17},
        ],
        'reason': 'computed',
    })
    assert (edge_id, score, reason) == ('gt_text_to_gt_answer', 0.17, 'computed')

    missing_id, missing_score, _ = normalize_edge_output({
        'id': 'gt_text_to_gt_answer',
        'claims': [{'text': 'a', 'score': 0.95}],
        'reason': 'missing direct score',
    })
    assert (missing_id, missing_score) == ('gt_text_to_gt_answer', 0.0)


def test_enrich_computed_scores_injects_claims_edge_from_top_level_claims() -> None:
    parsed = {
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'},
        ],
        'claims_judgment': [
            {'id': 'c1', 'text': 'a', 'score': 0.95, 'evidence': 'ctx'},
            {'id': 'c2', 'text': 'b', 'score': 0.0, 'evidence': ''},
        ],
        'summary_reason': 'summary',
    }

    enriched = _enrich_computed_scores(parsed)

    assert enriched is parsed
    assert 'claims_judgment' not in parsed
    edge = next(item for item in parsed['edges'] if item['id'] == 'gt_text_to_gt_answer')
    assert edge['score'] == 0.0
    assert edge['reason'] == 'summary'
    assert edge['claims'] == [
        {'id': 'c1', 'text': 'a', 'score': 0.95, 'evidence': 'ctx'},
        {'id': 'c2', 'text': 'b', 'score': 0.0, 'evidence': ''},
    ]


def test_eval_qc_schema_and_edge_ids_contract() -> None:
    assert EDGE_IDS == (
        'query_to_gt_answer',
        'query_to_gt_text',
        'query_to_key_points',
        'gt_text_to_gt_answer',
        'gt_answer_to_key_points',
    )

    schema = SCHEMAS['eval_qc']
    assert set(schema['required']) >= {'edges', 'summary_reason'}

    validator = Draft202012Validator(schema)
    invalid_id = {
        'edges': [
            {'id': 'doc_0', 'score': 0.9, 'reason': 'bad'},
            {'id': 'query_to_gt_text', 'score': 0.9, 'reason': 'ok'},
            {'id': 'query_to_key_points', 'score': 0.9, 'reason': 'ok'},
            {
                'id': 'gt_text_to_gt_answer',
                'claims': [{'text': 'claim', 'score': 0.95}],
                'reason': 'ok',
            },
            {'id': 'gt_answer_to_key_points', 'score': 0.9, 'reason': 'ok'},
        ],
        'summary_reason': 'bad id',
    }
    duplicate_id = {
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 0.9, 'reason': 'ok'},
            {'id': 'query_to_gt_answer', 'score': 0.8, 'reason': 'dup'},
            {'id': 'query_to_key_points', 'score': 0.9, 'reason': 'ok'},
            {
                'id': 'gt_text_to_gt_answer',
                'claims': [{'text': 'claim', 'score': 0.95}],
                'reason': 'ok',
            },
            {'id': 'gt_answer_to_key_points', 'score': 0.9, 'reason': 'ok'},
        ],
        'summary_reason': 'duplicate id',
    }
    missing_id = {
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 0.9, 'reason': 'ok'},
            {'id': 'query_to_gt_text', 'score': 0.9, 'reason': 'ok'},
            {'id': 'query_to_key_points', 'score': 0.9, 'reason': 'ok'},
            {
                'id': 'gt_text_to_gt_answer',
                'claims': [{'text': 'claim', 'score': 0.95}],
                'reason': 'ok',
            },
        ],
        'summary_reason': 'missing id',
    }

    assert list(validator.iter_errors(invalid_id))
    assert list(validator.iter_errors(duplicate_id))
    assert list(validator.iter_errors(missing_id))


def test_eval_qc_two_phase_schemas_contract() -> None:
    split_validator = Draft202012Validator(SCHEMAS['eval_qc_split'])
    with_claims_validator = Draft202012Validator(SCHEMAS['eval_qc_with_claims'])

    assert not list(split_validator.iter_errors({
        'claims': [{'id': 'c1', 'text': 'claim'}],
    }))
    assert list(split_validator.iter_errors({'claims': []}))
    assert list(split_validator.iter_errors({'claims': [{'text': 'claim'}]}))
    assert not list(with_claims_validator.iter_errors({
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_gt_text', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_key_points', 'score': 1.0, 'reason': 'ok'},
            {'id': 'gt_answer_to_key_points', 'score': 1.0, 'reason': 'ok'},
        ],
        'claims_judgment': [
            {'id': 'c1', 'text': 'claim', 'score': 0.95, 'evidence': 'ctx'},
        ],
        'summary_reason': 'summary',
    }))
    assert list(with_claims_validator.iter_errors({
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_gt_text', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_key_points', 'score': 1.0, 'reason': 'ok'},
            {'id': 'gt_text_to_gt_answer', 'score': 1.0, 'reason': 'bad'},
        ],
        'claims_judgment': [
            {'id': 'c1', 'text': 'claim', 'score': 0.95, 'evidence': 'ctx'},
        ],
        'summary_reason': 'summary',
    }))


def test_patch_gt_text_edge_replaces_only_claims_edge() -> None:
    parsed = {
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'keep'},
            {
                'id': 'gt_text_to_gt_answer',
                'claims': [{'text': 'old', 'score': 0.95}],
                'reason': 'old',
            },
        ],
        'summary_reason': 'summary',
    }
    replacement = {
        'id': 'gt_text_to_gt_answer',
        'claims': [{'text': 'new', 'score': 0.0, 'evidence': ''}],
        'reason': 'new',
    }

    patched = _patch_gt_text_edge(parsed, replacement)

    assert patched is parsed
    assert parsed['edges'][0] == {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'keep'}
    assert parsed['edges'][1] is replacement


def test_run_eval_qc_uses_two_phase_claims_by_default() -> None:
    session = create_session(load_config())
    split_output = {
        'claims': [
            {'id': 'c1', 'text': 'supported claim'},
            {'id': 'c2', 'text': 'unsupported claim'},
        ],
    }
    eval_output = {
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_gt_text', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_key_points', 'score': 1.0, 'reason': 'ok'},
            {'id': 'gt_answer_to_key_points', 'score': 1.0, 'reason': 'ok'},
        ],
        'claims_judgment': [
            {'id': 'c1', 'text': 'supported claim', 'score': 0.95, 'evidence': 'ctx'},
            {'id': 'c2', 'text': 'unsupported claim', 'score': 0.0, 'evidence': ''},
        ],
        'summary_reason': 'summary',
    }
    llm = _ScriptedLLM(split_output, eval_output)

    with session_scope(session):
        parsed = run_eval_qc(session, _payload(), llm=llm)

    assert len(llm.calls) == 2
    assert 'gt_answer_claims' in llm.calls[1]
    edge = next(item for item in parsed['edges'] if item['id'] == 'gt_text_to_gt_answer')
    assert edge['claims'] == [
        {'id': 'c1', 'text': 'supported claim', 'score': 0.95, 'evidence': 'ctx'},
        {'id': 'c2', 'text': 'unsupported claim', 'score': 0.0, 'evidence': ''},
    ]
    assert edge['reason'] == 'summary'
    assert edge['score'] == 0.0


def test_run_eval_qc_two_phase_rejects_claim_shape_drift() -> None:
    session = create_session(load_config())
    split_output = {
        'claims': [
            {'id': 'c1', 'text': 'supported claim'},
            {'id': 'c2', 'text': 'unsupported claim'},
        ],
    }
    eval_output = {
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_gt_text', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_key_points', 'score': 1.0, 'reason': 'ok'},
            {'id': 'gt_answer_to_key_points', 'score': 1.0, 'reason': 'ok'},
        ],
        'claims_judgment': [
            {'id': 'c1', 'text': 'changed claim', 'score': 0.95, 'evidence': 'ctx'},
        ],
        'summary_reason': 'summary',
    }
    llm = _ScriptedLLM(split_output, eval_output)

    with session_scope(session):
        parsed = run_eval_qc(session, _payload(), llm=llm)

    edge = next(item for item in parsed['edges'] if item['id'] == 'gt_text_to_gt_answer')
    assert edge['claims'] == [
        {
            'id': 'fallback',
            'text': 'eval_qc fallback failed',
            'score': 0.0,
            'evidence': '',
        },
    ]
    assert edge['reason'] == 'eval_qc two-phase failed'
    assert normalize_edge_output(edge)[1] == 0.0


def test_run_eval_qc_falls_back_to_single_phase_when_split_is_empty() -> None:
    session = create_session(load_config())
    split_output = {'claims': []}
    fallback_output = {
        'edges': [
            {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_gt_text', 'score': 1.0, 'reason': 'ok'},
            {'id': 'query_to_key_points', 'score': 1.0, 'reason': 'ok'},
            {
                'id': 'gt_text_to_gt_answer',
                'claims': [
                    {
                        'text': 'fallback unsupported claim',
                        'score': 0.0,
                        'evidence': '',
                    },
                ],
                'reason': '单阶段兜底完成',
            },
            {'id': 'gt_answer_to_key_points', 'score': 1.0, 'reason': 'ok'},
        ],
        'summary_reason': 'summary',
    }
    llm = _ScriptedLLM(split_output, split_output, fallback_output)

    with session_scope(session):
        parsed = run_eval_qc(session, _payload(), llm=llm)

    assert len(llm.calls) == 3
    assert 'gt_answer_claims' not in llm.calls[2]
    edge = next(item for item in parsed['edges'] if item['id'] == 'gt_text_to_gt_answer')
    assert edge['claims'] == [
        {'text': 'fallback unsupported claim', 'score': 0.0, 'evidence': ''},
    ]
    assert edge['reason'] == '单阶段兜底完成'
    assert normalize_edge_output(edge)[1] == 0.0


def _payload() -> dict:
    return {
        'query': 'q',
        'gt_answer': 'supported claim and unsupported claim',
        'gt_text': ['ctx'],
        'key_points': ['k'],
    }
