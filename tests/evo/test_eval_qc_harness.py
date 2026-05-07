from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from evo.domain import EDGE_IDS, EdgeResult, EvalFeature, JudgeRecord, TraceMeta, TraceRecord
from evo.harness import eval_qc as eval_qc_harness
from evo.harness.clustering import cluster_badcases
from evo.runtime.config import load_config
from evo.runtime.session import create_session, session_scope


def _judge(
    *,
    trace_id: str = 't1',
    score: float = 0.5,
    gt_answer: str = 'gt',
    gt_text: list[str] | None = None,
    key: list[str] | None = None,
) -> JudgeRecord:
    return JudgeRecord(
        trace_id=trace_id,
        answer_correctness=score,
        key=key if key is not None else ['k1'],
        hit_key=['k1'],
        reason=['r'],
        context_recall=0.0,
        doc_recall=0.0,
        retrieved_file=[],
        gt_file=[],
        retrieved_text=[],
        gt_text=gt_text if gt_text is not None else ['ctx'],
        generated_answer='ans',
        gt_answer=gt_answer,
        faithfulness=0.0,
        human_verified=True,
    )


def _feature(dataset_id: str, *, passed: bool) -> EvalFeature:
    return EvalFeature(
        dataset_id=dataset_id,
        a_report_bad=not passed,
        qc_passed=passed,
        c_edge_results={
            edge_id: EdgeResult(score=1.0, reason='ok', threshold=0.6, passed=True)
            for edge_id in EDGE_IDS
        },
    )


def test_stage_a_pass_short_circuits() -> None:
    cfg = load_config()
    cfg = replace(cfg, eval_qc=replace(cfg.eval_qc, a_ac_threshold=0.6))
    session = create_session(cfg)
    with session_scope(session):
        session.set_parsed_corpus(
            judges={'case_1': _judge(score=0.95)},
            traces={'t1': TraceRecord(query='q1')},
            trace_meta=TraceMeta(),
        )
        with patch('evo.harness.eval_qc.run_eval_qc') as mocked:
            count = eval_qc_harness.run_eval_qc_step(session)

    feature = session.eval_features['case_1']
    assert count == 1
    assert feature.qc_passed is True
    assert feature.a_report_bad is False
    assert feature.c_edge_results == {}
    mocked.assert_not_called()


def test_stage_b_empty_field_short_circuits() -> None:
    session = create_session(load_config())
    with session_scope(session):
        session.set_parsed_corpus(
            judges={
                'case_1': _judge(
                    score=0.1,
                    gt_answer=' ',
                    gt_text=[],
                    key=[],
                ),
            },
            traces={'t1': TraceRecord(query=' ')},
            trace_meta=TraceMeta(),
        )
        with patch('evo.harness.eval_qc.run_eval_qc') as mocked:
            eval_qc_harness.run_eval_qc_step(session)

    feature = session.eval_features['case_1']
    assert set(feature.b_reject_tags) == {
        'query_empty',
        'gt_answer_empty',
        'gt_text_empty',
        'key_points_empty',
    }
    assert feature.qc_passed is False
    mocked.assert_not_called()


def test_stage_c_missing_edge_filled_as_failed() -> None:
    session = create_session(load_config())
    with session_scope(session):
        session.set_parsed_corpus(
            judges={'case_1': _judge(score=0.2)},
            traces={'t1': TraceRecord(query='q1')},
            trace_meta=TraceMeta(),
        )
        with patch(
            'evo.harness.eval_qc.run_eval_qc',
            return_value={
                'summary_reason': 'partial',
                'edges': [{'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'}],
            },
        ):
            eval_qc_harness.run_eval_qc_step(session)

    feature = session.eval_features['case_1']
    assert set(feature.c_edge_results) == set(EDGE_IDS)
    assert feature.c_edge_results['query_to_gt_answer'].passed is True
    assert feature.c_edge_results['query_to_gt_text'].passed is False
    assert feature.c_edge_results['query_to_gt_text'].reason == 'edge missing in model output'
    assert feature.qc_passed is False


def test_stage_c_claims_edge_uses_computed_score() -> None:
    session = create_session(load_config())
    with session_scope(session):
        session.set_parsed_corpus(
            judges={'case_1': _judge(score=0.2)},
            traces={'t1': TraceRecord(query='q1')},
            trace_meta=TraceMeta(),
        )
        with patch(
            'evo.harness.eval_qc.run_eval_qc',
            return_value={
                'summary_reason': 'claims computed',
                'edges': [
                    {'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'},
                    {'id': 'query_to_gt_text', 'score': 1.0, 'reason': 'ok'},
                    {'id': 'query_to_key_points', 'score': 1.0, 'reason': 'ok'},
                    {
                        'id': 'gt_text_to_gt_answer',
                        'claims': [
                            {'text': 'supported', 'score': 0.95},
                            {'text': 'unsupported', 'score': 0.0},
                        ],
                        'score': 0.0,
                        'reason': 'one unsupported claim',
                    },
                    {'id': 'gt_answer_to_key_points', 'score': 1.0, 'reason': 'ok'},
                ],
            },
        ):
            eval_qc_harness.run_eval_qc_step(session)

    feature = session.eval_features['case_1']
    result = feature.c_edge_results['gt_text_to_gt_answer']
    assert result.score == 0.0
    assert result.passed is False
    assert feature.qc_passed is False


def test_eval_qc_disabled_writes_empty_features() -> None:
    cfg = load_config()
    cfg = replace(cfg, eval_qc=replace(cfg.eval_qc, enabled=False))
    session = create_session(cfg)
    with session_scope(session):
        session.set_parsed_corpus(
            judges={'case_1': _judge(score=0.2)},
            traces={'t1': TraceRecord(query='q1')},
            trace_meta=TraceMeta(),
        )
        count = eval_qc_harness.run_eval_qc_step(session)

    assert count == 0
    assert session.eval_features == {}


def test_session_get_passed_ids_contract() -> None:
    cfg = load_config()
    disabled_session = create_session(replace(cfg, eval_qc=replace(cfg.eval_qc, enabled=False)))
    with session_scope(disabled_session):
        disabled_session.set_parsed_corpus(
            judges={
                'case_1': _judge(trace_id='t1', score=0.2),
                'case_2': _judge(trace_id='t2', score=0.8),
            },
            traces={'t1': TraceRecord(query='q1'), 't2': TraceRecord(query='q2')},
            trace_meta=TraceMeta(),
        )
        assert disabled_session.get_passed_dataset_ids() == {'case_1', 'case_2'}

    enabled_session = create_session(replace(cfg, eval_qc=replace(cfg.eval_qc, enabled=True)))
    with session_scope(enabled_session):
        enabled_session.set_parsed_corpus(
            judges={'case_1': _judge(score=0.2)},
            traces={'t1': TraceRecord(query='q1')},
            trace_meta=TraceMeta(),
        )
        with pytest.raises(RuntimeError, match='eval_features is empty'):
            enabled_session.get_passed_dataset_ids()


def test_session_set_eval_features_rejects_unknown_id() -> None:
    session = create_session(load_config())
    with session_scope(session):
        session.set_parsed_corpus(
            judges={'case_1': _judge(score=0.2)},
            traces={'t1': TraceRecord(query='q1')},
            trace_meta=TraceMeta(),
        )
        with pytest.raises(ValueError, match='unknown dataset_id'):
            session.set_eval_features({'case_9': _feature('case_9', passed=True)})


def test_downstream_clustering_excludes_failed_cases() -> None:
    session = create_session(load_config())
    with session_scope(session):
        session.set_parsed_corpus(
            judges={
                'case_1': _judge(trace_id='t1', score=0.1),
                'case_2': _judge(trace_id='t2', score=0.2),
            },
            traces={'t1': TraceRecord(query='q1'), 't2': TraceRecord(query='q2')},
            trace_meta=TraceMeta(pipeline=['retriever']),
        )
        session.set_step_features(
            {
                'case_1': {'retriever': {'m1': 1.0, 'm2': 0.0}},
                'case_2': {'retriever': {'m1': 0.1, 'm2': 1.0}},
            },
            {'retriever': {'stats': {}}},
        )
        session.set_eval_features({
            'case_1': _feature('case_1', passed=True),
            'case_2': _feature('case_2', passed=False),
        })
        result = cluster_badcases(limit=10).unwrap()

    member_ids = set()
    for item in result.cluster_summaries:
        member_ids.update(item.exemplar_case_ids)
    assert 'case_1' in member_ids
    assert 'case_2' not in member_ids
