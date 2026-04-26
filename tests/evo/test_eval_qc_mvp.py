"""MVP / smoke tests for eval_qc wiring with session and downstream steps.

Kept small and fast: pass-set when QC is off, strict eval_features, clustering exclusion,
and schema registration. For A/B/C stages, config env, and mocked agent calls see
``test_eval_qc_coverage.py``.
"""
from __future__ import annotations

from dataclasses import replace

from evo.domain import EdgeResult, EvalFeature, JudgeRecord, TraceMeta, TraceRecord
from evo.harness.clustering import cluster_badcases
from evo.harness.schemas import SCHEMAS
from evo.runtime.config import load_config
from evo.runtime.session import create_session, session_scope


def _judge(*, trace_id: str, score: float) -> JudgeRecord:
    return JudgeRecord(
        trace_id=trace_id,
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


def _feature(dataset_id: str, passed: bool) -> EvalFeature:
    return EvalFeature(
        dataset_id=dataset_id,
        a_report_bad=not passed,
        qc_passed=passed,
        c_edge_results={
            'query_to_gt_answer': EdgeResult(score=1.0, reason='ok', threshold=0.6, passed=True),
        },
    )


def test_get_passed_dataset_ids_returns_all_when_eval_qc_disabled() -> None:
    cfg = load_config()
    cfg = replace(cfg, eval_qc=replace(cfg.eval_qc, enabled=False))
    session = create_session(cfg)
    judges = {
        'case_1': _judge(trace_id='t1', score=0.2),
        'case_2': _judge(trace_id='t2', score=0.8),
    }
    traces = {
        't1': TraceRecord(query='q1'),
        't2': TraceRecord(query='q2'),
    }
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        assert session.get_passed_dataset_ids() == {'case_1', 'case_2'}


def test_get_passed_dataset_ids_raises_when_eval_qc_enabled_but_empty() -> None:
    session = create_session(load_config())
    judges = {'case_1': _judge(trace_id='t1', score=0.2)}
    traces = {'t1': TraceRecord(query='q1')}
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        try:
            session.get_passed_dataset_ids()
            raise AssertionError(
                'get_passed_dataset_ids should fail when eval_qc is enabled but empty',
            )
        except RuntimeError as exc:
            assert 'eval_features is empty' in str(exc)


def test_set_eval_features_rejects_unknown_dataset_ids() -> None:
    session = create_session(load_config())
    judges = {'case_1': _judge(trace_id='t1', score=0.2)}
    traces = {'t1': TraceRecord(query='q1')}
    with session_scope(session):
        session.set_parsed_corpus(judges=judges, traces=traces, trace_meta=TraceMeta())
        try:
            session.set_eval_features({'case_9': _feature('case_9', passed=True)})
            raise AssertionError('set_eval_features should fail on unknown dataset ids')
        except ValueError as exc:
            assert 'unknown dataset_id' in str(exc)


def test_cluster_badcases_excludes_qc_failed_cases() -> None:
    session = create_session(load_config())
    judges = {
        'case_1': _judge(trace_id='t1', score=0.1),
        'case_2': _judge(trace_id='t2', score=0.2),
    }
    traces = {
        't1': TraceRecord(query='q1'),
        't2': TraceRecord(query='q2'),
    }
    features = {
        'case_1': {'retriever': {'m1': 1.0, 'm2': 0.0}},
        'case_2': {'retriever': {'m1': 0.1, 'm2': 1.0}},
    }
    with session_scope(session):
        session.set_parsed_corpus(
            judges=judges,
            traces=traces,
            trace_meta=TraceMeta(pipeline=['retriever']),
        )
        session.set_step_features(features, {'retriever': {'stats': {}}})
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


def test_eval_qc_schema_registered() -> None:
    assert 'eval_qc' in SCHEMAS
    schema = SCHEMAS['eval_qc']
    assert 'edges' in schema['properties']
    assert 'summary_reason' in schema['required']
