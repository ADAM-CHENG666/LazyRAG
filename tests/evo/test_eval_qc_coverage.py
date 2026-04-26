"""Deeper eval_qc coverage: config (env), session helpers, A/B/C flow, and pure tools.

LLM / structured-output paths are tested via ``unittest.mock`` (no real API). Complements
``test_eval_qc_mvp.py`` (integration smoke with session + clustering + schema only).
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from evo.agents import eval_qc as eval_qc_agent
from evo.domain import EvalFeature, JudgeRecord, TraceMeta, TraceRecord
from evo.harness import eval_qc as eval_qc_harness
from evo.harness.schemas import SCHEMAS
from evo.runtime.config import EvalQCConfig, load_config
from evo.runtime.session import create_session, session_scope
from evo.tools import eval_qc as eval_qc_tools


def _judge(*, trace_id: str, score: float, gt_answer: str = 'gt', gt_text: list[str] | None = None,
           key: list[str] | None = None, extra: dict | None = None) -> JudgeRecord:
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
        extra=extra or {},
    )


def _edges(score: float = 0.9) -> list[dict]:
    return [
        {'id': 'query_to_gt_answer', 'score': score, 'reason': 'ok'},
        {'id': 'query_to_gt_text', 'score': score, 'reason': 'ok'},
        {'id': 'query_to_key_points', 'score': score, 'reason': 'ok'},
        {'id': 'gt_text_to_gt_answer', 'score': score, 'reason': 'ok'},
        {'id': 'gt_answer_to_key_points', 'score': score, 'reason': 'ok'},
    ]


def test_eval_qc_config_loads_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('EVO_EVAL_QC_ENABLED', 'false')
    monkeypatch.setenv('EVO_EVAL_QC_A_SCORE_FIELD', 'faithfulness')
    monkeypatch.setenv('EVO_EVAL_QC_A_AC_THRESHOLD', '0.55')
    monkeypatch.setenv('EVO_EVAL_QC_C_EDGE_DEFAULT_THRESHOLD', '0.75')

    cfg = load_config(data_dir=tmp_path / 'data', base_dir=tmp_path / 'out')

    assert cfg.eval_qc.enabled is False
    assert cfg.eval_qc.a_score_field == 'faithfulness'
    assert cfg.eval_qc.a_ac_threshold == 0.55
    assert cfg.eval_qc.c_edge_default_threshold == 0.75


def test_session_iter_passed_judge_filters_failed_cases() -> None:
    session = create_session(load_config())
    judges = {
        'case_1': _judge(trace_id='t1', score=0.2),
        'case_2': _judge(trace_id='t2', score=0.2),
    }
    traces = {'t1': TraceRecord(query='q1'), 't2': TraceRecord(query='q2')}
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        session.set_eval_features({
            'case_1': replace(EvalFeature('case_1', a_report_bad=True), qc_passed=True),
            'case_2': replace(EvalFeature('case_2', a_report_bad=True), qc_passed=False),
        })
        passed = [dataset_id for dataset_id, _ in session.iter_passed_judge()]
    assert passed == ['case_1']


def test_stage_a_happy_path_skips_stage_b_and_c() -> None:
    cfg = load_config()
    cfg = replace(cfg, eval_qc=replace(cfg.eval_qc, a_ac_threshold=0.6))
    session = create_session(cfg)
    judges = {'case_1': _judge(trace_id='t1', score=0.95)}
    traces = {'t1': TraceRecord(query='q1')}
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        with patch('evo.harness.eval_qc.run_eval_qc') as mocked:
            count = eval_qc_harness.run_eval_qc_step(session)
    feature = session.eval_features['case_1']
    assert count == 1
    assert feature.a_report_bad is False
    assert feature.qc_passed is True
    assert feature.b_reject_tags == []
    assert feature.c_edge_results == {}
    mocked.assert_not_called()


def test_stage_b_reject_short_circuits_stage_c() -> None:
    session = create_session(load_config())
    judges = {
        'case_1': _judge(trace_id='t1', score=0.1, gt_answer=' ', gt_text=[], key=[]),
    }
    traces = {'t1': TraceRecord(query='q1')}
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        with patch('evo.harness.eval_qc.run_eval_qc') as mocked:
            eval_qc_harness.run_eval_qc_step(session)
    feature = session.eval_features['case_1']
    assert feature.a_report_bad is True
    assert set(feature.b_reject_tags) >= {'gt_answer_empty', 'gt_text_empty', 'key_points_empty'}
    assert feature.qc_passed is False
    mocked.assert_not_called()


def test_stage_c_fills_missing_edges_as_failed_regression() -> None:
    session = create_session(load_config())
    judges = {'case_1': _judge(trace_id='t1', score=0.2)}
    traces = {'t1': TraceRecord(query='q1')}
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        with patch(
            'evo.harness.eval_qc.run_eval_qc',
            return_value={'summary_reason': 'partial', 'edges': [{'id': 'query_to_gt_answer', 'score': 1.0, 'reason': 'ok'}]},
        ):
            eval_qc_harness.run_eval_qc_step(session)
    feature = session.eval_features['case_1']
    assert len(feature.c_edge_results) == len(eval_qc_harness.EDGE_IDS)
    assert feature.c_edge_results['query_to_gt_answer'].passed is True
    assert feature.c_edge_results['query_to_gt_text'].passed is False
    assert feature.c_edge_results['query_to_gt_text'].reason == 'edge missing in model output'
    assert feature.qc_passed is False


def test_eval_qc_disabled_writes_empty_features() -> None:
    cfg = load_config()
    cfg = replace(cfg, eval_qc=replace(cfg.eval_qc, enabled=False))
    session = create_session(cfg)
    judges = {'case_1': _judge(trace_id='t1', score=0.2)}
    traces = {'t1': TraceRecord(query='q1')}
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        count = eval_qc_harness.run_eval_qc_step(session)
    assert count == 0
    assert session.eval_features == {}


def test_resolve_query_falls_back_to_judge_extra_boundary() -> None:
    session = create_session(load_config())
    judge = _judge(trace_id='missing_trace', score=0.1, extra={'query': 'from_extra'})
    with session_scope(session):
        session.set_parsed_corpus(judges={'case_1': judge}, traces={}, trace_meta=TraceMeta())
        with patch('evo.harness.eval_qc.run_eval_qc', return_value={'summary_reason': 'ok', 'edges': _edges(0.8)}) as mocked:
            feature = eval_qc_harness._run_case(session, 'case_1', judge, score_field='answer_correctness')
    assert 'query_empty' not in feature.b_reject_tags
    sent_payload = mocked.call_args[0][1]
    assert sent_payload['query'] == 'from_extra'


def test_tools_apply_hard_filter_boundary_and_missing() -> None:
    cfg = EvalQCConfig(a_ac_threshold=0.6)
    low = _judge(trace_id='t1', score=0.59)
    bad, tags, severity = eval_qc_tools.apply_hard_filter(low, score_field='answer_correctness', config=cfg)
    assert bad is True and tags == ['ac_low'] and severity == 'low'

    good = _judge(trace_id='t2', score=0.60)
    bad2, tags2, severity2 = eval_qc_tools.apply_hard_filter(good, score_field='answer_correctness', config=cfg)
    assert bad2 is False and tags2 == [] and severity2 is None

    missing = _judge(trace_id='t3', score=0.8)
    bad3, tags3, severity3 = eval_qc_tools.apply_hard_filter(missing, score_field='not_exists', config=cfg)
    assert bad3 is True and tags3 == ['ac_missing'] and severity3 == 'high'


def test_tools_normalize_edge_output_error_fallback_and_clamp() -> None:
    assert eval_qc_tools.normalize_edge_output('bad_payload') == ('', 0.0, 'invalid edge payload')
    edge_id, score, reason = eval_qc_tools.normalize_edge_output({'id': 'x', 'score': 2.7, 'reason': 'r'})
    assert edge_id == 'x' and score == 1.0 and reason == 'r'
    edge_id2, score2, _ = eval_qc_tools.normalize_edge_output({'id': 'x', 'score': -1})
    assert edge_id2 == 'x' and score2 == 0.0


def test_contract_schema_edge_ids() -> None:
    assert 'eval_qc' in SCHEMAS
    schema = SCHEMAS['eval_qc']
    assert set(schema['required']) >= {'edges', 'summary_reason'}

    assert set(eval_qc_harness.EDGE_IDS) == {
        'query_to_gt_answer',
        'query_to_gt_text',
        'query_to_key_points',
        'gt_text_to_gt_answer',
        'gt_answer_to_key_points',
    }


def test_agent_eval_qc_can_be_tested_without_real_api() -> None:
    session = create_session(load_config())
    payload = {'query': 'q', 'gt_answer': 'a', 'gt_text': ['ctx'], 'key_points': ['k']}
    fake = {'edges': _edges(0.8), 'summary_reason': 'ok'}
    with patch('evo.agents.eval_qc.invoke_structured', return_value=fake) as invoke_mock:
        out = eval_qc_agent.run_eval_qc(session, payload)

    assert out == fake
    invoke_mock.assert_called_once()
