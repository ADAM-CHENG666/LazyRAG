from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from evo import artifacts as A
from evo.artifact_runtime import (
    AggregateValue,
    Operation,
    OperationContext,
    OperationResult,
    PartitionSet,
    all_items,
    each,
    keyed,
    one,
    operation,
    partitioned,
    record_event,
    record_process,
    scalar,
)

from .abtest.candidate import async_candidate_rag_answer, candidate_service, finalize_candidate
from .abtest.comparison import compare_abtest
from .analysis.classify import classify_case
from .analysis.cluster import cluster_traces
from .analysis.summary import build_analysis_summary
from .analysis.trace_summary import build_trace_summary
from .dataset.assemble import assemble_dataset
from .dataset.generation import build_case_requests, generate_case, prepare_case
from .dataset.kb_loader import build_corpus_snapshot, load_corpus
from .eval.answer import async_answer_case
from .eval.judge import judge_case
from .public_contracts import build_eval_summary_root, require_mapping as _mapping
from .repair.loop import build_verified_patch, prepare_candidate_workspace, run_repair_loop
from .repair.plan import build_repair_plan


@operation(
    op_id='dataset.load_corpus',
    inputs={'source_config': one(A.CORPUS_SOURCE_CONFIG)},
    outputs={'report': scalar(A.CORPUS_REPORT)},
)
async def load_corpus_operation(ctx: OperationContext, source_config: object) -> OperationResult:
    report = load_corpus(_mapping(source_config, 'source_config'))
    return await _recorded_result(ctx, 'dataset.corpus_loaded', {'report': report}, status=report.get('status'))


@operation(
    op_id='dataset.build_corpus_snapshot',
    inputs={
        'report': one(A.CORPUS_REPORT),
        'source_config': one(A.CORPUS_SOURCE_CONFIG),
    },
    outputs={'snapshot': scalar(A.CORPUS_SNAPSHOT)},
)
async def build_corpus_snapshot_operation(ctx: OperationContext, report: object, source_config: object
                                          ) -> OperationResult:
    snapshot = build_corpus_snapshot(_mapping(report, 'report'), _mapping(source_config, 'source_config'))
    return await _recorded_result(
        ctx, 'dataset.corpus_snapshot_built', {'snapshot': snapshot},
        document_count=len(snapshot.get('documents') or ()),
    )


@operation(
    op_id='dataset.case_requests',
    inputs={
        'config': one(A.RUN_CONFIG),
        'snapshot': one(A.CORPUS_SNAPSHOT),
    },
    outputs={
        'partitions': scalar(A.EVAL_CASE_REQUESTS),
        'requests': partitioned(A.EVAL_CASE_REQUEST, over=A.EVAL_CASE_REQUESTS),
    },
)
async def case_requests_operation(ctx: OperationContext, config: object, snapshot: object) -> OperationResult:
    requests = build_case_requests(
        _mapping(config, 'config'),
        _mapping(snapshot, 'snapshot'),
    )
    total = len(requests)
    return await _recorded_result(
        ctx, 'dataset.case_requests_built',
        {'partitions': PartitionSet(tuple(requests)), 'requests': requests},
        current=total, total=total, case_count=total,
    )


@operation(
    op_id='dataset.prepare_case',
    inputs={
        'request': each(A.EVAL_CASE_REQUEST, over=A.EVAL_CASE_REQUESTS),
        'config': one(A.RUN_CONFIG),
        'snapshot': one(A.CORPUS_SNAPSHOT),
    },
    outputs={'preparation': partitioned(A.EVAL_CASE_PREPARATION)},
    max_concurrency=4,
)
async def prepare_case_operation(ctx: OperationContext, request: object, config: object, snapshot: object
                                 ) -> OperationResult:
    preparation = prepare_case(
        _mapping(config, 'config'), _mapping(snapshot, 'snapshot'),
        ctx.partition_key, _mapping(request, 'request'),
    )
    return await _recorded_result(
        ctx, 'dataset.case_prepared', {'preparation': preparation}, case_id=ctx.partition_key,
    )


@operation(
    op_id='dataset.generate_case',
    inputs={
        'preparation': each(A.EVAL_CASE_PREPARATION, over=A.EVAL_CASE_REQUESTS),
        'config': one(A.RUN_CONFIG),
        'snapshot': one(A.CORPUS_SNAPSHOT),
    },
    outputs={'case': partitioned(A.EVAL_CASE)},
    max_concurrency=4,
)
async def generate_case_operation(ctx: OperationContext, preparation: object, config: object, snapshot: object
                                  ) -> OperationResult:
    await ctx.record('dataset.case_generation_started', status='started', case_id=ctx.partition_key)
    case = generate_case(
        _mapping(config, 'config'), _mapping(snapshot, 'snapshot'),
        _mapping(preparation, 'preparation'),
    )
    return await _recorded_result(
        ctx, 'dataset.case_generated', {'case': case}, case_id=ctx.partition_key,
    )


@operation(
    op_id='dataset.assemble',
    inputs={'cases': all_items(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS)},
    outputs={'dataset': scalar(A.EVAL_DATASET)},
)
async def assemble_dataset_operation(ctx: OperationContext, cases: object) -> OperationResult:
    case_map = _mapping(cases, 'cases')
    failures = _failure_summary(cases)
    if not case_map:
        raise ValueError('dataset has no successful cases')
    dataset = assemble_dataset(
        case_map, run_id=ctx.run_id, failed_cases=failures['failed_cases'],
    )
    return await _recorded_result(
        ctx, 'dataset.assembled', {'dataset': dataset},
        current=len(case_map), total=len(case_map) + failures['failed_case_num'], case_count=len(case_map),
        failed_case_count=failures['failed_case_num'],
    )


@operation(
    op_id='eval.answer',
    inputs={
        'case': each(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS),
        'dataset': one(A.EVAL_DATASET),
        'target_config': one(A.EVAL_TARGET_CONFIG),
        'approval': one(A.APPROVAL_DATASET),
    },
    outputs={'answer': partitioned(A.EVAL_RAG_ANSWER)},
    max_concurrency=4,
)
async def eval_answer_operation(ctx: OperationContext, case: object, dataset: object, target_config: object,
                                approval: object) -> OperationResult:
    await ctx.record('eval.answer_requested', status='started', case_id=ctx.partition_key)
    answer = await async_answer_case(
        _mapping(case, 'case'),
        _mapping(target_config, 'target_config'),
    )
    return await _recorded_result(
        ctx, 'eval.answer_received', {'answer': answer}, case_id=ctx.partition_key,
        answer_status=answer.get('status'), trace_id=answer.get('trace_id'),
    )


@operation(
    op_id='eval.judge',
    inputs={
        'case': each(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS),
        'answer': keyed(A.EVAL_RAG_ANSWER),
        'policy': one(A.EVAL_POLICY),
    },
    outputs={'judge': partitioned(A.EVAL_JUDGE_RESULT)},
    max_concurrency=4,
)
async def eval_judge_operation(ctx: OperationContext, case: object, answer: object, policy: object) -> OperationResult:
    judge = judge_case(
        _mapping(case, 'case'),
        _mapping(answer, 'answer'),
        _mapping(policy, 'policy'),
    )
    return await _recorded_result(
        ctx, 'eval.case_judged', {'judge': judge}, case_id=ctx.partition_key,
        quality_label=judge.get('quality_label'), failure_type=judge.get('failure_type'),
        overall_score=judge.get('overall_score'),
    )


@operation(
    op_id='eval.summary',
    inputs={'judges': all_items(A.EVAL_JUDGE_RESULT, over=A.EVAL_CASE_REQUESTS)},
    outputs={'summary': scalar(A.EVAL_SUMMARY)},
)
async def eval_summary_operation(ctx: OperationContext, judges: object) -> OperationResult:
    values = _partition_values(judges, 'judges')
    failures = _failure_summary(judges)
    summary = build_eval_summary_root(ctx.run_id, values, failures['failed_cases'])
    return await _recorded_result(
        ctx, 'eval.summary_built', {'summary': summary},
        current=len(values), total=len(values) + failures['failed_case_num'], case_count=len(values),
        failed_case_count=failures['failed_case_num'],
    )


@operation(
    op_id='analysis.trace_summary',
    inputs={
        'case': each(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS),
        'answer': keyed(A.EVAL_RAG_ANSWER),
        'eval_summary': one(A.EVAL_SUMMARY),
        'approval': one(A.APPROVAL_EVAL),
    },
    outputs={'summary': partitioned(A.ANALYSIS_TRACE_SUMMARY)},
    max_concurrency=4,
)
async def trace_summary_operation(ctx: OperationContext, case: object, answer: object, eval_summary: object,
                                  approval: object) -> OperationResult:
    _mapping(eval_summary, 'eval_summary')
    summary = build_trace_summary(_mapping(case, 'case'), _mapping(answer, 'answer'))
    return await _recorded_result(
        ctx, 'analysis.trace_summarized', {'summary': summary}, case_id=ctx.partition_key,
        retrieval_step_count=len(summary.get('retrieval_steps') or ()),
        error_stage_count=len(summary.get('error_stages') or ()),
    )


@operation(
    op_id='analysis.classify_case',
    inputs={
        'case': each(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS),
        'answer': keyed(A.EVAL_RAG_ANSWER),
        'judge': keyed(A.EVAL_JUDGE_RESULT),
        'trace': keyed(A.ANALYSIS_TRACE_SUMMARY),
    },
    outputs={'classification': partitioned(A.ANALYSIS_CASE_CLASSIFICATION)},
    max_concurrency=4,
)
async def classify_case_operation(ctx: OperationContext, case: object, answer: object, judge: object, trace: object
                                  ) -> OperationResult:
    classification = classify_case(
        _mapping(case, 'case'), _mapping(answer, 'answer'),
        _mapping(judge, 'judge'), _mapping(trace, 'trace'),
    )
    return await _recorded_result(
        ctx, 'analysis.case_classified', {'classification': classification}, case_id=ctx.partition_key,
        issue_type=classification.get('issue_type'), failure_mode=classification.get('failure_mode'),
    )


@operation(
    op_id='analysis.trace_clusters',
    inputs={
        'classifications': all_items(
            A.ANALYSIS_CASE_CLASSIFICATION,
            over=A.EVAL_CASE_REQUESTS,
        ),
    },
    outputs={'clusters': scalar(A.ANALYSIS_TRACE_CLUSTERS)},
)
async def trace_clusters_operation(ctx: OperationContext, classifications: object) -> OperationResult:
    values = _partition_values(classifications, 'classifications')
    clusters = cluster_traces(values) | _failure_summary(classifications)
    return await _recorded_result(
        ctx, 'analysis.traces_clustered', {'clusters': clusters},
        case_count=len(values), cluster_count=len(clusters.get('clusters') or ()),
    )


@operation(
    op_id='analysis.summary',
    inputs={
        'classifications': all_items(
            A.ANALYSIS_CASE_CLASSIFICATION,
            over=A.EVAL_CASE_REQUESTS,
        ),
        'clusters': one(A.ANALYSIS_TRACE_CLUSTERS),
    },
    outputs={'summary': scalar(A.ANALYSIS_SUMMARY)},
)
async def analysis_summary_operation(ctx: OperationContext, classifications: object, clusters: object
                                     ) -> OperationResult:
    values = _partition_values(classifications, 'classifications')
    summary = build_analysis_summary(ctx.run_id, values, _mapping(clusters, 'clusters')) | _failure_summary(
        classifications,
    )
    return await _recorded_result(
        ctx, 'analysis.summary_built', {'summary': summary},
        case_count=len(values), repair_group_count=len(summary.get('repair_group_queue') or ()),
    )


@operation(
    op_id='repair.plan',
    inputs={
        'analysis': one(A.ANALYSIS_SUMMARY),
        'policy': one(A.REPAIR_POLICY),
        'approval': one(A.APPROVAL_ANALYSIS),
    },
    outputs={'plan': scalar(A.REPAIR_PLAN)},
)
async def repair_plan_operation(ctx: OperationContext, analysis: object, policy: object, approval: object
                                ) -> OperationResult:
    plan = build_repair_plan(_mapping(analysis, 'analysis'), _mapping(policy, 'policy'))
    return await _recorded_result(
        ctx, 'repair.plan_built', {'plan': plan}, status=plan.get('status'),
        validation_case_count=len((plan.get('objective') or {}).get('validation_case_ids') or ()),
    )


@operation(
    op_id='repair.candidate_workspace',
    inputs={
        'plan': one(A.REPAIR_PLAN),
        'policy': one(A.REPAIR_POLICY),
    },
    outputs={'workspace': scalar(A.REPAIR_CANDIDATE_WORKSPACE)},
)
async def candidate_workspace_operation(ctx: OperationContext, plan: object, policy: object) -> OperationResult:
    workspace = prepare_candidate_workspace(_mapping(plan, 'plan'), _mapping(policy, 'policy'))
    return await _recorded_result(
        ctx, 'repair.workspace_prepared', {'workspace': workspace}, status=workspace.get('status'),
        workspace_kind=workspace.get('workspace_kind'),
    )


@operation(
    op_id='repair.loop_result',
    inputs={
        'plan': one(A.REPAIR_PLAN),
        'workspace': one(A.REPAIR_CANDIDATE_WORKSPACE),
        'cases': all_items(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS),
        'baseline_judges': all_items(
            A.EVAL_JUDGE_RESULT,
            over=A.EVAL_CASE_REQUESTS,
        ),
        'eval_policy': one(A.EVAL_POLICY),
        'candidate_config': one(A.ABTEST_CANDIDATE_CONFIG),
        'policy': one(A.REPAIR_POLICY),
    },
    outputs={'result': scalar(A.REPAIR_LOOP_RESULT)},
    timeout=1800.0,
)
@record_process
async def repair_loop_operation(ctx: OperationContext, plan: object, workspace: object, cases: object,
                                baseline_judges: object, eval_policy: object, candidate_config: object, policy: object
                                ) -> OperationResult:
    return OperationResult({
        'result': await run_repair_loop(
            _mapping(workspace, 'workspace'),
            _partition_values(cases, 'cases'),
            _partition_values(baseline_judges, 'baseline_judges'),
            _mapping(eval_policy, 'eval_policy'),
            _mapping(candidate_config, 'candidate_config'),
            _mapping(policy, 'policy'),
            ctx,
            _mapping(plan, 'plan'),
        ),
    })


@operation(
    op_id='repair.verified_patch',
    inputs={'loop': one(A.REPAIR_LOOP_RESULT)},
    outputs={'patch': scalar(A.REPAIR_VERIFIED_PATCH)},
)
@record_process
async def verified_patch_operation(ctx: OperationContext, loop: object) -> OperationResult:
    patch = build_verified_patch(ctx.run_id, _mapping(loop, 'loop'))
    record_event(
        'repair.patch_verified',
        status='completed',
        terminal=True,
        data={
            'status': patch.get('status'),
            'file_count': len(patch.get('diff') or {}),
        },
    )
    return OperationResult({'patch': patch})


@operation(
    op_id='abtest.candidate_service',
    inputs={
        'config': one(A.ABTEST_CANDIDATE_CONFIG),
        'patch': one(A.REPAIR_VERIFIED_PATCH),
        'workspace': one(A.REPAIR_CANDIDATE_WORKSPACE),
        'approval': one(A.APPROVAL_REPAIR),
    },
    outputs={'service': scalar(A.ABTEST_CANDIDATE_SERVICE)},
)
async def candidate_service_operation(ctx: OperationContext, config: object, patch: object, workspace: object,
                                      approval: object) -> OperationResult:
    await ctx.record('abtest.candidate_service_starting', status='started')
    service = candidate_service(
        _mapping(config, 'config'), _mapping(patch, 'patch'), ctx, _mapping(workspace, 'workspace'),
    )
    return await _recorded_result(
        ctx, 'abtest.candidate_service_ready', {'service': service}, status=service.get('status'),
        service_kind=service.get('service_kind'), algorithm_id=service.get('algorithm_id'),
    )


@operation(
    op_id='abtest.candidate_rag_answer',
    inputs={
        'case': each(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS),
        'service': one(A.ABTEST_CANDIDATE_SERVICE),
    },
    outputs={'answer': partitioned(A.ABTEST_CANDIDATE_RAG_ANSWER)},
    max_concurrency=4,
)
async def candidate_answer_operation(ctx: OperationContext, case: object, service: object) -> OperationResult:
    await ctx.record('abtest.candidate_answer_requested', status='started', case_id=ctx.partition_key)
    answer = await async_candidate_rag_answer(
        _mapping(case, 'case'),
        _mapping(service, 'service'),
    )
    return await _recorded_result(
        ctx, 'abtest.candidate_answer_received', {'answer': answer}, case_id=ctx.partition_key,
        answer_status=answer.get('status'), trace_id=answer.get('trace_id'),
    )


@operation(
    op_id='abtest.candidate_judge',
    inputs={
        'case': each(A.EVAL_CASE, over=A.EVAL_CASE_REQUESTS),
        'answer': keyed(A.ABTEST_CANDIDATE_RAG_ANSWER),
        'policy': one(A.EVAL_POLICY),
    },
    outputs={'judge': partitioned(A.ABTEST_CANDIDATE_JUDGE_RESULT)},
    max_concurrency=4,
)
async def candidate_judge_operation(ctx: OperationContext, case: object, answer: object, policy: object
                                    ) -> OperationResult:
    judge = judge_case(
        _mapping(case, 'case'),
        _mapping(answer, 'answer'),
        _mapping(policy, 'policy'),
    )
    return await _recorded_result(
        ctx, 'abtest.candidate_case_judged', {'judge': judge}, case_id=ctx.partition_key,
        quality_label=judge.get('quality_label'), failure_type=judge.get('failure_type'),
        overall_score=judge.get('overall_score'),
    )


@operation(
    op_id='abtest.candidate_eval_summary',
    inputs={
        'judges': all_items(
            A.ABTEST_CANDIDATE_JUDGE_RESULT,
            over=A.EVAL_CASE_REQUESTS,
        ),
    },
    outputs={'summary': scalar(A.ABTEST_CANDIDATE_EVAL_SUMMARY)},
)
async def candidate_summary_operation(ctx: OperationContext, judges: object) -> OperationResult:
    values = _partition_values(judges, 'judges')
    failures = _failure_summary(judges)
    summary = build_eval_summary_root(ctx.run_id, values, failures['failed_cases'])
    return await _recorded_result(
        ctx, 'abtest.candidate_summary_built', {'summary': summary},
        current=len(values), total=len(values) + failures['failed_case_num'], case_count=len(values),
        failed_case_count=failures['failed_case_num'],
    )


@operation(
    op_id='abtest.compare',
    inputs={
        'baseline': one(A.EVAL_SUMMARY),
        'candidate': one(A.ABTEST_CANDIDATE_EVAL_SUMMARY),
        'service': one(A.ABTEST_CANDIDATE_SERVICE),
    },
    outputs={'comparison': scalar(A.ABTEST_COMPARISON)},
)
async def compare_abtest_operation(ctx: OperationContext, baseline: object, candidate: object, service: object
                                   ) -> OperationResult:
    comparison = compare_abtest(
        ctx.run_id,
        _mapping(baseline, 'baseline'),
        _mapping(candidate, 'candidate'),
        _mapping(service, 'service'),
    )
    finalize_candidate(_mapping(service, 'service'), comparison)
    return await _recorded_result(
        ctx, 'abtest.comparison_completed', {'comparison': comparison},
        verdict=comparison.get('verdict'), status=comparison.get('status'),
    )


_EVO_OPERATIONS: tuple[Operation, ...] = (
    load_corpus_operation,
    build_corpus_snapshot_operation,
    case_requests_operation,
    prepare_case_operation,
    generate_case_operation,
    assemble_dataset_operation,
    eval_answer_operation,
    eval_judge_operation,
    eval_summary_operation,
    trace_summary_operation,
    classify_case_operation,
    trace_clusters_operation,
    analysis_summary_operation,
    repair_plan_operation,
    candidate_workspace_operation,
    repair_loop_operation,
    verified_patch_operation,
    candidate_service_operation,
    candidate_answer_operation,
    candidate_judge_operation,
    candidate_summary_operation,
    compare_abtest_operation,
)


def evo_operations() -> tuple[Operation, ...]:
    return _EVO_OPERATIONS


async def _recorded_result(ctx: OperationContext, event_type: str, values: Mapping[str, object], *,
                           current: int | None = None, total: int | None = None, **data: object) -> OperationResult:
    await ctx.record(event_type, status='completed', data=data, current=current, total=total)
    return OperationResult(values)


def _partition_values(value: object, name: str) -> tuple[Mapping[str, Any], ...]:
    values = tuple(_mapping(value, name).values())
    if not values:
        raise ValueError(f'{name} has no successful cases')
    if not all(isinstance(item, Mapping) for item in values):
        raise ValueError(f'{name} must contain mappings')
    return values


def _failure_summary(value: object) -> dict[str, object]:
    failures = [] if not isinstance(value, AggregateValue) else [
        {
            'case_id': failure.case_id,
            'operation_id': failure.operation_id,
            'attempt_id': failure.attempt_id,
            'error_kind': failure.error.kind,
            'error_message': failure.error.message,
        }
        for failure in value.failures.values()
    ]
    return {
        'failed_case_num': len(failures),
        'failed_cases': failures,
        'completed_with_problems': bool(failures),
    }


__all__ = [
    'analysis_summary_operation', 'assemble_dataset_operation', 'build_corpus_snapshot_operation',
    'candidate_answer_operation', 'candidate_judge_operation', 'candidate_service_operation',
    'candidate_summary_operation', 'candidate_workspace_operation', 'case_requests_operation',
    'classify_case_operation', 'compare_abtest_operation', 'eval_answer_operation',
    'eval_judge_operation', 'eval_summary_operation', 'evo_operations', 'generate_case_operation',
    'load_corpus_operation', 'prepare_case_operation', 'repair_loop_operation',
    'repair_plan_operation', 'trace_clusters_operation', 'trace_summary_operation',
    'verified_patch_operation',
]
