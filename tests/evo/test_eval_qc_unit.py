from __future__ import annotations

from jsonschema import Draft202012Validator

from evo.agents.eval_qc import _enrich_computed_scores
from evo.domain import EDGE_IDS, JudgeRecord
from evo.harness.schemas import SCHEMAS
from evo.runtime.config import EvalQCConfig
from evo.tools.eval_qc import (
    apply_hard_filter,
    apply_hard_rules,
    compute_score_from_claims,
    normalize_edge_output,
)


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


def test_compute_score_from_claims_mapping() -> None:
    assert compute_score_from_claims(None) == 0.0
    assert compute_score_from_claims([]) == 0.0
    assert compute_score_from_claims([
        {'text': 'a', 'judgment': 'supported'},
        {'text': 'b', 'judgment': 'supported'},
    ]) == 0.95
    assert compute_score_from_claims([
        {'text': 'a', 'judgment': 'unsupported'},
    ]) == 0.25
    assert compute_score_from_claims([
        {'text': 'a', 'judgment': 'supported'},
        {'text': 'b', 'judgment': 'unsupported'},
    ]) == 0.25
    assert compute_score_from_claims([
        {'text': 'a', 'judgment': 'supported'},
        {'text': 'b', 'judgment': 'partial'},
    ]) == 0.60
    assert compute_score_from_claims([
        {'text': 'a', 'judgment': 'partial'},
        {'text': 'b', 'judgment': 'unsupported'},
    ]) == 0.25


def test_normalize_edge_output_claims_override_score_for_gt_text_edge() -> None:
    edge_id, score, reason = normalize_edge_output({
        'id': 'gt_text_to_gt_answer',
        'score': 0.99,
        'claims': [
            {'text': 'a', 'judgment': 'supported'},
            {'text': 'b', 'judgment': 'unsupported'},
        ],
        'reason': 'computed',
    })
    assert (edge_id, score, reason) == ('gt_text_to_gt_answer', 0.25, 'computed')

    legacy_id, legacy_score, _ = normalize_edge_output({
        'id': 'gt_text_to_gt_answer',
        'score': 0.87,
        'reason': 'legacy',
    })
    assert (legacy_id, legacy_score) == ('gt_text_to_gt_answer', 0.87)


def test_enrich_computed_scores_writes_score_back_for_claims_edge() -> None:
    parsed = {
        'edges': [
            {
                'id': 'gt_text_to_gt_answer',
                'claims': [
                    {'text': 'a', 'judgment': 'supported'},
                    {'text': 'b', 'judgment': 'unsupported'},
                ],
                'reason': 'one unsupported',
            },
        ],
        'summary_reason': 'summary',
    }

    enriched = _enrich_computed_scores(parsed)

    assert enriched is parsed
    assert parsed['edges'][0]['score'] == 0.25
    assert 'claims' in parsed['edges'][0]


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
                'claims': [{'text': 'claim', 'judgment': 'supported'}],
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
                'claims': [{'text': 'claim', 'judgment': 'supported'}],
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
                'claims': [{'text': 'claim', 'judgment': 'supported'}],
                'reason': 'ok',
            },
        ],
        'summary_reason': 'missing id',
    }

    assert list(validator.iter_errors(invalid_id))
    assert list(validator.iter_errors(duplicate_id))
    assert list(validator.iter_errors(missing_id))
