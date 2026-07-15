from __future__ import annotations

from ..kernel import (
    ArtifactInput,
    ArtifactOutput,
    FixedOp,
    StaticPartitions,
    all_to_unpartitioned,
    unpartitioned_to_all,
)

from . import catalog as C
from .flow import DatasetFlowSpec


def default_evo_ops(cases: tuple[str, ...]) -> tuple[type[FixedOp], ...]:
    partitions = StaticPartitions(cases)

    class SelectDocs(FixedOp):
        op_id = 'dataset.select_docs'
        inputs = {'source_config': ArtifactInput(C.CORPUS_SOURCE_CONFIG)}
        outputs = {'selected_docs': ArtifactOutput(C.DATASET_SELECTED_DOCS)}

    class BuildChunks(FixedOp):
        op_id = 'dataset.build_chunks'
        inputs = {
            'build_chunk_candidates': ArtifactInput(C.DATASET_BUILD_CHUNK_CANDIDATES, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'chunk': ArtifactOutput(C.DATASET_CHUNK, partitions)}

    class BuildChunkCandidates(FixedOp):
        op_id = 'dataset.build_chunk_candidates'
        inputs = {'selected_docs': ArtifactInput(C.DATASET_SELECTED_DOCS),
                  'build_chunks_params': ArtifactInput(C.DATASET_BUILD_CHUNKS_PARAMS)}
        outputs = {'build_chunk_candidates': ArtifactOutput(C.DATASET_BUILD_CHUNK_CANDIDATES)}

    class BuildChunksManifest(FixedOp):
        op_id = 'dataset.build_chunks_manifest'
        inputs = {
            'selected_docs': ArtifactInput(C.DATASET_SELECTED_DOCS),
            'build_chunk_candidates': ArtifactInput(C.DATASET_BUILD_CHUNK_CANDIDATES),
            'chunk': ArtifactInput(
                C.DATASET_CHUNK,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
        }
        outputs = {'build_chunks_manifest': ArtifactOutput(C.DATASET_BUILD_CHUNKS_MANIFEST)}

    class ChunkEntitiesExtract(FixedOp):
        op_id = 'dataset.chunk_entities_extract'
        inputs = {
            'chunk': ArtifactInput(C.DATASET_CHUNK, partition_spec=partitions),
            'chunk_entities_extract_params': ArtifactInput(
                C.DATASET_CHUNK_ENTITIES_EXTRACT_PARAMS,
                partition_mapping=unpartitioned_to_all(),
            ),
        }
        outputs = {'chunk_entity': ArtifactOutput(C.DATASET_CHUNK_ENTITY, partitions)}

    class ChunkEntitiesExtractManifest(FixedOp):
        op_id = 'dataset.chunk_entities_extract_manifest'
        inputs = {
            'build_chunks_manifest': ArtifactInput(C.DATASET_BUILD_CHUNKS_MANIFEST),
            'chunk_entities': ArtifactInput(
                C.DATASET_CHUNK_ENTITY,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'chunk_entities_extract_manifest_params': ArtifactInput(
                C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST_PARAMS
            ),
        }
        outputs = {'chunk_entities_extract_manifest': ArtifactOutput(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST)}

    class TopicDiscoveryEntityBuildGraph(FixedOp):
        op_id = 'dataset.topic_discovery_entity_build_graph'
        inputs = {
            'chunk_entity': ArtifactInput(
                C.DATASET_CHUNK_ENTITY,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'topic_discovery_entity_build_graph_params': ArtifactInput(
                C.DATASET_TOPIC_DISCOVERY_ENTITY_BUILD_GRAPH_PARAMS
            ),
        }
        outputs = {'entity_graph': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_ENTITY_GRAPH)}

    class TopicDiscoveryEntityCluster(FixedOp):
        op_id = 'dataset.topic_discovery_entity_cluster'
        inputs = {
            'entity_graph': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_ENTITY_GRAPH),
            'topic_discovery_entity_cluster_params': ArtifactInput(
                C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTER_PARAMS
            ),
        }
        outputs = {'entity_clusters': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTERS)}

    class TopicDiscoveryEmbeddingCluster(FixedOp):
        op_id = 'dataset.topic_discovery_embedding_cluster'
        inputs = {
            'chunk': ArtifactInput(
                C.DATASET_CHUNK,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'topic_discovery_embedding_cluster_params': ArtifactInput(
                C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_PARAMS
            ),
        }
        outputs = {
            'embedding_cluster_candidates': ArtifactOutput(
                C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_CANDIDATES
            )
        }

    class TopicDiscoveryEmbeddingLabel(FixedOp):
        op_id = 'dataset.topic_discovery_embedding_label'
        inputs = {
            'embedding_cluster_candidates': ArtifactInput(
                C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_CANDIDATES
            ),
            'chunk': ArtifactInput(
                C.DATASET_CHUNK,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'topic_discovery_embedding_label_params': ArtifactInput(
                C.DATASET_TOPIC_DISCOVERY_EMBEDDING_LABEL_PARAMS
            ),
        }
        outputs = {'embedding_clusters': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTERS)}

    class TopicDiscoveryManifest(FixedOp):
        op_id = 'dataset.topic_discovery_manifest'
        inputs = {
            'entity_clusters': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTERS),
            'embedding_clusters': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTERS),
        }
        outputs = {'topic_discovery_manifest': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_MANIFEST)}

    class LoadCorpus(FixedOp):
        op_id = 'dataset.load_corpus'
        inputs = {'source_config': ArtifactInput(C.CORPUS_SOURCE_CONFIG)}
        outputs = {'report': ArtifactOutput(C.CORPUS_REPORT)}

    class BuildCorpusSnapshot(FixedOp):
        op_id = 'dataset.build_corpus_snapshot'
        inputs = {
            'report': ArtifactInput(C.CORPUS_REPORT),
            'source_config': ArtifactInput(C.CORPUS_SOURCE_CONFIG),
        }
        outputs = {'snapshot': ArtifactOutput(C.CORPUS_SNAPSHOT)}

    class GenerateCase(FixedOp):
        op_id = 'dataset.generate_case'
        inputs = {
            'config': ArtifactInput(C.RUN_CONFIG, partition_mapping=unpartitioned_to_all()),
            'snapshot': ArtifactInput(C.CORPUS_SNAPSHOT, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {
            'preparation': ArtifactOutput(C.EVAL_CASE_PREPARATION, partitions),
            'case': ArtifactOutput(C.EVAL_CASE, partitions),
        }

    class AssembleDataset(FixedOp):
        op_id = 'dataset.assemble'
        inputs = {
            'cases': ArtifactInput(
                C.EVAL_CASE,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            )
        }
        outputs = {'dataset': ArtifactOutput(C.ROOTS['dataset'])}

    class EvalAnswer(FixedOp):
        op_id = 'eval.answer'
        inputs = {
            'case': ArtifactInput(C.EVAL_CASE, partition_spec=partitions),
            'target_config': ArtifactInput(C.EVAL_TARGET_CONFIG, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'answer': ArtifactOutput(C.EVAL_RAG_ANSWER, partitions)}

    class EvalJudge(FixedOp):
        op_id = 'eval.judge'
        inputs = {
            'case': ArtifactInput(C.EVAL_CASE, partition_spec=partitions),
            'answer': ArtifactInput(C.EVAL_RAG_ANSWER, partition_spec=partitions),
            'policy': ArtifactInput(C.EVAL_POLICY, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'judge': ArtifactOutput(C.EVAL_JUDGE_RESULT, partitions)}

    class EvalSummary(FixedOp):
        op_id = 'eval.summary'
        inputs = {
            'judges': ArtifactInput(
                C.EVAL_JUDGE_RESULT,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            )
        }
        outputs = {'summary': ArtifactOutput(C.ROOTS['eval'])}

    class TraceSummary(FixedOp):
        op_id = 'analysis.trace_summary'
        inputs = {
            'case': ArtifactInput(C.EVAL_CASE, partition_spec=partitions),
            'answer': ArtifactInput(C.EVAL_RAG_ANSWER, partition_spec=partitions),
        }
        outputs = {'summary': ArtifactOutput(C.ANALYSIS_TRACE_SUMMARY, partitions)}

    class ClassifyCase(FixedOp):
        op_id = 'analysis.classify_case'
        inputs = {
            'case': ArtifactInput(C.EVAL_CASE, partition_spec=partitions),
            'answer': ArtifactInput(C.EVAL_RAG_ANSWER, partition_spec=partitions),
            'judge': ArtifactInput(C.EVAL_JUDGE_RESULT, partition_spec=partitions),
            'trace': ArtifactInput(C.ANALYSIS_TRACE_SUMMARY, partition_spec=partitions),
        }
        outputs = {'classification': ArtifactOutput(C.ANALYSIS_CASE_CLASSIFICATION, partitions)}

    class TraceClusters(FixedOp):
        op_id = 'analysis.trace_clusters'
        inputs = {
            'classifications': ArtifactInput(
                C.ANALYSIS_CASE_CLASSIFICATION,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            )
        }
        outputs = {'clusters': ArtifactOutput(C.ANALYSIS_TRACE_CLUSTERS)}

    class AnalysisSummary(FixedOp):
        op_id = 'analysis.summary'
        inputs = {
            'classifications': ArtifactInput(
                C.ANALYSIS_CASE_CLASSIFICATION,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'clusters': ArtifactInput(C.ANALYSIS_TRACE_CLUSTERS),
        }
        outputs = {'summary': ArtifactOutput(C.ROOTS['analysis'])}

    class BuildRepairPlan(FixedOp):
        op_id = 'repair.plan'
        inputs = {
            'classifications': ArtifactInput(
                C.ANALYSIS_CASE_CLASSIFICATION,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'clusters': ArtifactInput(C.ANALYSIS_TRACE_CLUSTERS),
            'policy': ArtifactInput(C.REPAIR_POLICY),
        }
        outputs = {'plan': ArtifactOutput(C.REPAIR_PLAN)}

    class PrepareWorkspace(FixedOp):
        op_id = 'repair.candidate_workspace'
        inputs = {
            'plan': ArtifactInput(C.REPAIR_PLAN),
            'policy': ArtifactInput(C.REPAIR_POLICY),
        }
        outputs = {'workspace': ArtifactOutput(C.REPAIR_CANDIDATE_WORKSPACE)}

    class RepairLoop(FixedOp):
        op_id = 'repair.loop_result'
        inputs = {
            'plan': ArtifactInput(C.REPAIR_PLAN),
            'workspace': ArtifactInput(C.REPAIR_CANDIDATE_WORKSPACE),
            'cases': ArtifactInput(
                C.EVAL_CASE,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'baseline_judges': ArtifactInput(
                C.EVAL_JUDGE_RESULT,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            ),
            'eval_policy': ArtifactInput(C.EVAL_POLICY),
            'candidate_config': ArtifactInput(C.ABTEST_CANDIDATE_CONFIG),
            'policy': ArtifactInput(C.REPAIR_POLICY),
        }
        outputs = {'result': ArtifactOutput(C.REPAIR_LOOP_RESULT)}

    class VerifyRepair(FixedOp):
        op_id = 'repair.verified_patch'
        inputs = {'loop': ArtifactInput(C.REPAIR_LOOP_RESULT)}
        outputs = {'patch': ArtifactOutput(C.ROOTS['repair'])}

    class CandidateService(FixedOp):
        op_id = 'abtest.candidate_service'
        inputs = {
            'config': ArtifactInput(C.ABTEST_CANDIDATE_CONFIG),
            'patch': ArtifactInput(C.ROOTS['repair']),
            'workspace': ArtifactInput(C.REPAIR_CANDIDATE_WORKSPACE),
        }
        outputs = {'service': ArtifactOutput(C.ABTEST_CANDIDATE_SERVICE)}

    class CandidateRagAnswer(FixedOp):
        op_id = 'abtest.candidate_rag_answer'
        inputs = {
            'case': ArtifactInput(C.EVAL_CASE, partition_spec=partitions),
            'service': ArtifactInput(C.ABTEST_CANDIDATE_SERVICE, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'answer': ArtifactOutput(C.ABTEST_CANDIDATE_RAG_ANSWER, partitions)}

    class CandidateJudge(FixedOp):
        op_id = 'abtest.candidate_judge'
        inputs = {
            'case': ArtifactInput(C.EVAL_CASE, partition_spec=partitions),
            'answer': ArtifactInput(C.ABTEST_CANDIDATE_RAG_ANSWER, partition_spec=partitions),
            'policy': ArtifactInput(C.EVAL_POLICY, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'judge': ArtifactOutput(C.ABTEST_CANDIDATE_JUDGE_RESULT, partitions)}

    class CandidateSummary(FixedOp):
        op_id = 'abtest.candidate_eval_summary'
        inputs = {
            'judges': ArtifactInput(
                C.ABTEST_CANDIDATE_JUDGE_RESULT,
                partition_spec=partitions,
                partition_mapping=all_to_unpartitioned(),
            )
        }
        outputs = {'summary': ArtifactOutput(C.ABTEST_CANDIDATE_EVAL_SUMMARY)}

    class CompareABTest(FixedOp):
        op_id = 'abtest.compare'
        inputs = {
            'baseline': ArtifactInput(C.ROOTS['eval']),
            'candidate': ArtifactInput(C.ABTEST_CANDIDATE_EVAL_SUMMARY),
            'service': ArtifactInput(C.ABTEST_CANDIDATE_SERVICE),
        }
        outputs = {'comparison': ArtifactOutput(C.ROOTS['abtest'])}

    return (
        SelectDocs,
        BuildChunkCandidates,
        BuildChunks,
        BuildChunksManifest,
        ChunkEntitiesExtract,
        ChunkEntitiesExtractManifest,
        TopicDiscoveryEntityBuildGraph,
        TopicDiscoveryEntityCluster,
        TopicDiscoveryEmbeddingCluster,
        TopicDiscoveryEmbeddingLabel,
        TopicDiscoveryManifest,
        LoadCorpus,
        BuildCorpusSnapshot,
        GenerateCase,
        AssembleDataset,
        EvalAnswer,
        EvalJudge,
        EvalSummary,
        TraceSummary,
        ClassifyCase,
        TraceClusters,
        AnalysisSummary,
        BuildRepairPlan,
        PrepareWorkspace,
        RepairLoop,
        VerifyRepair,
        CandidateService,
        CandidateRagAnswer,
        CandidateJudge,
        CandidateSummary,
        CompareABTest,
    )


def dataset_evo_ops(cases: tuple[str, ...]) -> tuple[type[FixedOp], ...]:
    op_ids = (
        'dataset.select_docs',
        'dataset.build_chunks',
        'dataset.build_chunks_manifest',
        'dataset.chunk_entities_extract',
        'dataset.chunk_entities_extract_manifest',
        'dataset.topic_discovery_entity_build_graph',
        'dataset.topic_discovery_entity_cluster',
        'dataset.topic_discovery_embedding_cluster',
        'dataset.topic_discovery_embedding_label',
        'dataset.topic_discovery_manifest',
        'dataset.load_corpus',
        'dataset.build_corpus_snapshot',
        'dataset.generate_case',
        'dataset.assemble',
    )
    ops = {op.op_id: op for op in default_evo_ops(cases)}
    return tuple(ops[op_id] for op_id in op_ids)


def qaplan_dataset_evo_ops(spec: DatasetFlowSpec) -> tuple[type[FixedOp], ...]:
    """The new dataset-only graph with independent chunk and case partitions."""

    chunks = StaticPartitions(spec.chunk_ids)
    cases = StaticPartitions(spec.case_ids)

    class SelectDocs(FixedOp):
        op_id = 'dataset.select_docs'
        inputs = {'source_config': ArtifactInput(C.CORPUS_SOURCE_CONFIG)}
        outputs = {'selected_docs': ArtifactOutput(C.DATASET_SELECTED_DOCS)}

    class BuildChunks(FixedOp):
        op_id = 'dataset.build_chunks'
        inputs = {
            'build_chunk_candidates': ArtifactInput(C.DATASET_BUILD_CHUNK_CANDIDATES, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'chunk': ArtifactOutput(C.DATASET_CHUNK, chunks)}

    class BuildChunkCandidates(FixedOp):
        op_id = 'dataset.build_chunk_candidates'
        inputs = {'selected_docs': ArtifactInput(C.DATASET_SELECTED_DOCS),
                  'build_chunks_params': ArtifactInput(C.DATASET_BUILD_CHUNKS_PARAMS)}
        outputs = {'build_chunk_candidates': ArtifactOutput(C.DATASET_BUILD_CHUNK_CANDIDATES)}

    class BuildChunksManifest(FixedOp):
        op_id = 'dataset.build_chunks_manifest'
        inputs = {
            'selected_docs': ArtifactInput(C.DATASET_SELECTED_DOCS),
            'build_chunk_candidates': ArtifactInput(C.DATASET_BUILD_CHUNK_CANDIDATES),
            'chunk': ArtifactInput(C.DATASET_CHUNK, partition_spec=chunks, partition_mapping=all_to_unpartitioned()),
        }
        outputs = {'build_chunks_manifest': ArtifactOutput(C.DATASET_BUILD_CHUNKS_MANIFEST)}

    class ChunkEntitiesExtract(FixedOp):
        op_id = 'dataset.chunk_entities_extract'
        inputs = {
            'chunk': ArtifactInput(C.DATASET_CHUNK, partition_spec=chunks),
            'build_chunks_manifest': ArtifactInput(C.DATASET_BUILD_CHUNKS_MANIFEST, partition_mapping=unpartitioned_to_all()),
            'chunk_entities_extract_params': ArtifactInput(C.DATASET_CHUNK_ENTITIES_EXTRACT_PARAMS, partition_mapping=unpartitioned_to_all()),
            'run_config': ArtifactInput(C.RUN_CONFIG, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'chunk_entity': ArtifactOutput(C.DATASET_CHUNK_ENTITY, chunks)}

    class ChunkEntitiesManifest(FixedOp):
        op_id = 'dataset.chunk_entities_extract_manifest'
        inputs = {
            'build_chunks_manifest': ArtifactInput(C.DATASET_BUILD_CHUNKS_MANIFEST),
            'chunk_entities': ArtifactInput(C.DATASET_CHUNK_ENTITY, partition_spec=chunks, partition_mapping=all_to_unpartitioned()),
            'chunk_entities_extract_manifest_params': ArtifactInput(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST_PARAMS),
        }
        outputs = {'chunk_entities_extract_manifest': ArtifactOutput(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST)}

    class EntityGraph(FixedOp):
        op_id = 'dataset.topic_discovery_entity_build_graph'
        inputs = {
            'chunk_entity': ArtifactInput(C.DATASET_CHUNK_ENTITY, partition_spec=chunks, partition_mapping=all_to_unpartitioned()),
            'chunk_entities_extract_manifest': ArtifactInput(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST),
            'topic_discovery_entity_build_graph_params': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_ENTITY_BUILD_GRAPH_PARAMS),
        }
        outputs = {'entity_graph': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_ENTITY_GRAPH)}

    class EntityClusters(FixedOp):
        op_id = 'dataset.topic_discovery_entity_cluster'
        inputs = {
            'entity_graph': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_ENTITY_GRAPH),
            'topic_discovery_entity_cluster_params': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTER_PARAMS),
        }
        outputs = {'entity_clusters': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTERS)}

    class EmbeddingCandidates(FixedOp):
        op_id = 'dataset.topic_discovery_embedding_cluster'
        inputs = {
            'chunk': ArtifactInput(C.DATASET_CHUNK, partition_spec=chunks, partition_mapping=all_to_unpartitioned()),
            'chunk_entities_extract_manifest': ArtifactInput(C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST),
            'topic_discovery_embedding_cluster_params': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_PARAMS),
        }
        outputs = {'embedding_cluster_candidates': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_CANDIDATES)}

    class EmbeddingClusters(FixedOp):
        op_id = 'dataset.topic_discovery_embedding_label'
        inputs = {
            'embedding_cluster_candidates': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_CANDIDATES),
            'chunk': ArtifactInput(C.DATASET_CHUNK, partition_spec=chunks, partition_mapping=all_to_unpartitioned()),
            'topic_discovery_embedding_label_params': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_LABEL_PARAMS),
            'run_config': ArtifactInput(C.RUN_CONFIG),
        }
        outputs = {'embedding_clusters': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTERS)}

    class TopicManifest(FixedOp):
        op_id = 'dataset.topic_discovery_manifest'
        inputs = {
            'entity_clusters': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTERS),
            'embedding_clusters': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTERS),
        }
        outputs = {'topic_discovery_manifest': ArtifactOutput(C.DATASET_TOPIC_DISCOVERY_MANIFEST)}

    class QaplanPlan(FixedOp):
        op_id = 'dataset.qaplan_plan'
        inputs = {
            'source_config': ArtifactInput(C.CORPUS_SOURCE_CONFIG),
            'topic_discovery_manifest': ArtifactInput(C.DATASET_TOPIC_DISCOVERY_MANIFEST),
            'chunk': ArtifactInput(C.DATASET_CHUNK, partition_spec=chunks, partition_mapping=all_to_unpartitioned()),
            'qaplan_plan_params': ArtifactInput(C.DATASET_QAPLAN_PLAN_PARAMS),
        }
        outputs = {'qaplan_plan': ArtifactOutput(C.DATASET_QAPLAN_PLAN)}

    class QaplanSpec(FixedOp):
        op_id = 'dataset.qaplan_spec'
        inputs = {'qaplan_plan': ArtifactInput(C.DATASET_QAPLAN_PLAN, partition_mapping=unpartitioned_to_all())}
        outputs = {'qaplan_spec': ArtifactOutput(C.DATASET_QAPLAN_SPEC, cases)}

    class Generate(FixedOp):
        op_id = 'dataset.generate'
        inputs = {
            'qaplan_spec': ArtifactInput(C.DATASET_QAPLAN_SPEC, partition_spec=cases),
            'run_config': ArtifactInput(C.RUN_CONFIG, partition_mapping=unpartitioned_to_all()),
        }
        outputs = {'case': ArtifactOutput(C.DATASET_CASE, cases)}

    class GenerateManifest(FixedOp):
        op_id = 'dataset.generate_manifest'
        inputs = {'cases': ArtifactInput(C.DATASET_CASE, partition_spec=cases, partition_mapping=all_to_unpartitioned())}
        outputs = {'generate_manifest': ArtifactOutput(C.DATASET_GENERATE_MANIFEST)}

    return (
        SelectDocs, BuildChunkCandidates, BuildChunks, BuildChunksManifest, ChunkEntitiesExtract, ChunkEntitiesManifest,
        EntityGraph, EntityClusters, EmbeddingCandidates, EmbeddingClusters, TopicManifest,
        QaplanPlan, QaplanSpec, Generate, GenerateManifest,
    )


def eval_evo_ops(cases: tuple[str, ...]) -> tuple[type[FixedOp], ...]:
    op_ids = (
        *[op.op_id for op in dataset_evo_ops(cases)],
        'eval.answer',
        'eval.judge',
        'eval.summary',
    )
    ops = {op.op_id: op for op in default_evo_ops(cases)}
    return tuple(ops[op_id] for op_id in op_ids)


def analysis_evo_ops(cases: tuple[str, ...]) -> tuple[type[FixedOp], ...]:
    op_ids = (
        *[op.op_id for op in eval_evo_ops(cases)],
        'analysis.trace_summary',
        'analysis.classify_case',
        'analysis.trace_clusters',
        'analysis.summary',
    )
    ops = {op.op_id: op for op in default_evo_ops(cases)}
    return tuple(ops[op_id] for op_id in op_ids)


def repair_evo_ops(cases: tuple[str, ...]) -> tuple[type[FixedOp], ...]:
    op_ids = (
        *[op.op_id for op in analysis_evo_ops(cases)],
        'repair.plan',
        'repair.candidate_workspace',
        'repair.loop_result',
        'repair.verified_patch',
    )
    ops = {op.op_id: op for op in default_evo_ops(cases)}
    return tuple(ops[op_id] for op_id in op_ids)


def abtest_evo_ops(cases: tuple[str, ...]) -> tuple[type[FixedOp], ...]:
    op_ids = (
        'abtest.candidate_service',
        'abtest.candidate_rag_answer',
        'abtest.candidate_judge',
        'abtest.candidate_eval_summary',
        'abtest.compare',
    )
    ops = {op.op_id: op for op in default_evo_ops(cases)}
    return tuple(ops[op_id] for op_id in op_ids)


__all__ = [
    'abtest_evo_ops',
    'analysis_evo_ops',
    'dataset_evo_ops',
    'default_evo_ops',
    'eval_evo_ops',
    'repair_evo_ops',
]
