from types import MappingProxyType


RUN_CONFIG = 'run.config'
CORPUS_SOURCE_CONFIG = 'corpus.source_config'
EVAL_TARGET_CONFIG = 'eval.target_config'
EVAL_POLICY = 'eval.policy'
REPAIR_POLICY = 'repair.policy'
ABTEST_CANDIDATE_CONFIG = 'abtest.candidate_config'

CORPUS_REPORT = 'corpus.report'
CORPUS_SNAPSHOT = 'corpus.snapshot'
EVAL_CASE_REQUESTS = 'eval.case_requests'
EVAL_CASE_REQUEST = 'eval.case_request'
EVAL_CASE_PREPARATION = 'eval.case_preparations'
EVAL_CASE_PREPARATION_ITEM = 'eval.case_preparation'
EVAL_CASE_CANDIDATES = 'eval.case_candidates'
EVAL_CASE_CANDIDATE_ITEM = 'eval.case_candidate'
EVAL_CASE = 'eval.cases'
EVAL_CASE_ITEM = 'eval.case'
EVAL_DATASET = 'eval.dataset'
EVAL_RAG_ANSWER = 'eval.rag_answers'
EVAL_RAG_ANSWER_ITEM = 'eval.rag_answer'
EVAL_JUDGE_RESULT = 'eval.judge_results'
EVAL_JUDGE_RESULT_ITEM = 'eval.judge_result'
EVAL_SUMMARY = 'eval.summary'
ANALYSIS_TRACE_SUMMARY = 'analysis.trace_summaries'
ANALYSIS_TRACE_SUMMARY_ITEM = 'analysis.trace_summary'
ANALYSIS_CASE_CLASSIFICATION = 'analysis.case_classifications'
ANALYSIS_CASE_CLASSIFICATION_ITEM = 'analysis.case_classification'
ANALYSIS_TRACE_CLUSTERS = 'analysis.trace_clusters'
ANALYSIS_SUMMARY = 'analysis.summary'
REPAIR_PLAN = 'repair.plan'
REPAIR_CANDIDATE_WORKSPACE = 'repair.candidate_workspace'
REPAIR_LOOP_RESULT = 'repair.loop_result'
REPAIR_VERIFIED_PATCH = 'repair.verified_patch'
ABTEST_CANDIDATE_SERVICE = 'abtest.candidate_service'
ABTEST_CANDIDATE_RAG_ANSWER = 'abtest.candidate_rag_answers'
ABTEST_CANDIDATE_RAG_ANSWER_ITEM = 'abtest.candidate_rag_answer'
ABTEST_CANDIDATE_JUDGE_RESULT = 'abtest.candidate_judge_results'
ABTEST_CANDIDATE_JUDGE_RESULT_ITEM = 'abtest.candidate_judge_result'
ABTEST_CANDIDATE_EVAL_SUMMARY = 'abtest.candidate_eval_summary'
ABTEST_COMPARISON = 'abtest.comparison'

STEPS = ('dataset', 'eval', 'analysis', 'repair', 'abtest')

SEEDS = (
    RUN_CONFIG,
    CORPUS_SOURCE_CONFIG,
    EVAL_TARGET_CONFIG,
    EVAL_POLICY,
    REPAIR_POLICY,
    ABTEST_CANDIDATE_CONFIG,
)

ROOTS = MappingProxyType({
    'dataset': EVAL_DATASET,
    'eval': EVAL_SUMMARY,
    'analysis': ANALYSIS_SUMMARY,
    'repair': REPAIR_VERIFIED_PATCH,
    'abtest': ABTEST_COMPARISON,
})

COLLECTION_ITEMS = MappingProxyType({
    EVAL_CASE_REQUESTS: EVAL_CASE_REQUEST,
    EVAL_CASE_PREPARATION: EVAL_CASE_PREPARATION_ITEM,
    EVAL_CASE_CANDIDATES: EVAL_CASE_CANDIDATE_ITEM,
    EVAL_CASE: EVAL_CASE_ITEM,
    EVAL_RAG_ANSWER: EVAL_RAG_ANSWER_ITEM,
    EVAL_JUDGE_RESULT: EVAL_JUDGE_RESULT_ITEM,
    ANALYSIS_TRACE_SUMMARY: ANALYSIS_TRACE_SUMMARY_ITEM,
    ANALYSIS_CASE_CLASSIFICATION: ANALYSIS_CASE_CLASSIFICATION_ITEM,
    ABTEST_CANDIDATE_RAG_ANSWER: ABTEST_CANDIDATE_RAG_ANSWER_ITEM,
    ABTEST_CANDIDATE_JUDGE_RESULT: ABTEST_CANDIDATE_JUDGE_RESULT_ITEM,
})


__all__ = [name for name in globals() if name.isupper()]
