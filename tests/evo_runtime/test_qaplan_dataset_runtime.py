from types import SimpleNamespace

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.evo.adapter import build_evo_artifact_adapter
from evo.artifact_runtime.evo.flow import DatasetFlowSpec
from evo.artifact_runtime.evo.flow_ops import dataset_evo_ops
from evo.artifact_runtime.kernel import ArtifactKey, SQLiteArtifactStore
from evo.operations.dataset.chunks_build import build_chunk_candidates
from evo.operations.dataset.generate import generate
from evo.operations.dataset.qaplan_pipeline import dataset_materializers
from evo.operations.dataset.select_docs import select_docs
from evo.operations.dataset.topic_discovery import topic_discovery_embedding_cluster, topic_discovery_embedding_label


class FakeKnowledgeBaseClient:
    def list_documents(self, kb_id):
        return [{'doc_id': 'doc-1', 'filename': 'guide.md', 'file_type': 'md', 'status': 'success',
                 'group_counts': {'block': 3}}]

    def iter_chunks(self, kb_id, doc_ids, groups, page_size):
        yield [
            SimpleNamespace(uid='chunk-1', text='Tesla has a battery warranty.', group='block',
                            embedding={'default': [1.0, 0.0]}, metadata={'type': 'text'},
                            global_metadata={'filename': 'guide.md'}),
            SimpleNamespace(uid='chunk-2', text='Tesla warranty covers battery defects.', group='block',
                            embedding={'default': [0.9, 0.1]}, metadata={'type': 'text'},
                            global_metadata={'filename': 'guide.md'}),
            SimpleNamespace(uid='chunk-3', text='Battery coverage has stated conditions.', group='block',
                            embedding={'default': [0.8, 0.2]}, metadata={'type': 'text'},
                            global_metadata={'filename': 'guide.md'}),
        ]


def test_topic_discovery_waits_for_chunk_entities_extract_manifest(tmp_path):
    spec = DatasetFlowSpec.from_case_count(2)
    store = SQLiteArtifactStore(tmp_path / 'store')
    materializers = dataset_materializers(spec.case_ids)
    client = FakeKnowledgeBaseClient()
    materializers.update({
        'dataset.select_docs': lambda ctx, inputs: select_docs(ctx, inputs, kb_client=client),
        # Candidate construction owns KB access; build_chunks only materializes a selected slot.
        'dataset.build_chunk_candidates': lambda ctx, inputs: build_chunk_candidates(ctx, inputs, kb_client=client),
        'dataset.chunk_entities_extract': lambda ctx, inputs: {
            'chunk_entity': {
                'available': inputs['chunk']['available'],
                'chunk_id': inputs['chunk']['chunk_id'],
                'doc_id': inputs['chunk']['doc_id'],
                'group': inputs['chunk']['group'],
                'entities': ['Tesla'],
            }
        },
        'dataset.topic_discovery_embedding_cluster': lambda ctx, inputs: topic_discovery_embedding_cluster(
            ctx, inputs, reducer=lambda matrix, params: matrix, clusterer=lambda matrix, params: [0] * len(matrix)
        ),
        'dataset.topic_discovery_embedding_label': lambda ctx, inputs: topic_discovery_embedding_label(
            ctx, inputs, llm_complete=lambda prompt: '{"topics":["battery warranty"]}'
        ),
        'dataset.generate': lambda ctx, inputs: generate(
            ctx, inputs, llm_complete=lambda prompt: '{"question":"What does the warranty cover?",'
            '"answer":"It covers battery defects under stated conditions.",'
            '"grading_guidance":"Assess coverage under the stated conditions."}'
        ),
        'dataset.generate_enhance': lambda ctx, inputs: {
            'case_enhance': {'key_points': [], 'forbidden_claims': []}
        },
    })
    adapter = build_evo_artifact_adapter(store, dataset_evo_ops(spec), materializers)
    _seed(adapter, 'manifest-barrier')

    tick = adapter.tick('manifest-barrier')

    assert tick.status == 'ok'
    op_ids = [item.op_id for item in tick.ops]
    manifest_index = op_ids.index('dataset.chunk_entities_extract_manifest')
    assert manifest_index < op_ids.index('dataset.topic_discovery_entity_build_graph')
    assert manifest_index < op_ids.index('dataset.topic_discovery_embedding_cluster')


def test_qaplan_dataset_runtime_converts_three_chunks_into_two_cases(tmp_path):
    spec = DatasetFlowSpec.from_case_count(2)
    assert len(spec.chunk_ids) == 3
    store = SQLiteArtifactStore(tmp_path / 'store')
    materializers = dataset_materializers(spec.case_ids)
    client = FakeKnowledgeBaseClient()
    materializers.update({
        'dataset.select_docs': lambda ctx, inputs: select_docs(ctx, inputs, kb_client=client),
        # Candidate construction owns KB access; build_chunks only materializes a selected slot.
        'dataset.build_chunk_candidates': lambda ctx, inputs: build_chunk_candidates(ctx, inputs, kb_client=client),
        'dataset.chunk_entities_extract': lambda ctx, inputs: {
            'chunk_entity': {
                'available': inputs['chunk']['available'], 'chunk_id': inputs['chunk']['chunk_id'],
                'doc_id': inputs['chunk']['doc_id'], 'group': inputs['chunk']['group'], 'entities': ['Tesla'],
            }
        },
        'dataset.topic_discovery_embedding_cluster': lambda ctx, inputs: topic_discovery_embedding_cluster(
            ctx, inputs, reducer=lambda matrix, params: matrix, clusterer=lambda matrix, params: [0] * len(matrix)
        ),
        'dataset.topic_discovery_embedding_label': lambda ctx, inputs: topic_discovery_embedding_label(
            ctx, inputs, llm_complete=lambda prompt: '{"topics":["battery warranty"]}'
        ),
        'dataset.generate': lambda ctx, inputs: generate(
            ctx, inputs, llm_complete=lambda prompt: '{"question":"What does the warranty cover?",'
            '"answer":"It covers battery defects under stated conditions.",'
            '"grading_guidance":"Assess coverage under the stated conditions."}'
        ),
        'dataset.generate_enhance': lambda ctx, inputs: {
            'case_enhance': {'key_points': [], 'forbidden_claims': []}
        },
    })
    adapter = build_evo_artifact_adapter(store, dataset_evo_ops(spec), materializers)
    run_id = 'qaplan-runtime'
    _seed(adapter, run_id)

    for _ in range(30):
        tick = adapter.tick(run_id)
        assert tick.status == 'ok', tick.ops
        if ArtifactKey.of(C.DATASET_GENERATE_ENHANCE_MANIFEST) in adapter.effective_artifacts(run_id):
            break
    else:
        raise AssertionError('qaplan generation manifest was not materialized')

    effective = adapter.effective_artifacts(run_id)
    manifest = adapter.get(run_id, effective[ArtifactKey.of(C.DATASET_GENERATE_MANIFEST)]).value
    assert manifest['stats']['case_count'] == 2
    assert [item['id'] for item in manifest['cases']] == ['case_0001', 'case_0002']
    assert ArtifactKey(C.DATASET_CHUNK, 'chunk_0003') in effective
    assert ArtifactKey(C.DATASET_CASE, 'case_0002') in effective
    assert ArtifactKey(C.DATASET_CASE_ENHANCE, 'case_0002') in effective
    assert ArtifactKey.of(C.DATASET_QAPLAN_MANIFEST) in effective
    assert ArtifactKey.of(C.DATASET_GENERATE_ENHANCE_MANIFEST) in effective


def _seed(adapter, run_id):
    values = {
        C.RUN_CONFIG: {'llm_config': {'evo_llm': {'model': 'test'}}},
        C.CORPUS_SOURCE_CONFIG: {'kb_id': 'kb-1', 'max_docs': 1, 'target_case_count': 2},
        C.DATASET_BUILD_CHUNKS_PARAMS: {'groups': ['block']},
        C.DATASET_CHUNK_ENTITIES_EXTRACT_PARAMS: {'max_entities_per_chunk': 8},
        C.DATASET_CHUNK_ENTITIES_EXTRACT_MANIFEST_PARAMS: {},
        C.DATASET_TOPIC_DISCOVERY_ENTITY_BUILD_GRAPH_PARAMS: {},
        C.DATASET_TOPIC_DISCOVERY_ENTITY_CLUSTER_PARAMS: {},
        C.DATASET_TOPIC_DISCOVERY_EMBEDDING_CLUSTER_PARAMS: {
            'umap_n_neighbors': 2, 'umap_n_components': 1, 'min_cluster_size': 2, 'min_samples': 1,
        },
        C.DATASET_TOPIC_DISCOVERY_EMBEDDING_LABEL_PARAMS: {},
        C.DATASET_QAPLAN_PLAN_PARAMS: {'lane_ratios': {
            'entity_precision_easy': 1, 'entity_precision_medium': 0, 'entity_precision_hard': 0,
            'embedding_reasoning_easy': 1, 'embedding_reasoning_medium': 0, 'embedding_reasoning_hard': 0,
        }},
    }
    for artifact_id, value in values.items():
        adapter.commit_external(run_id, ArtifactKey.of(artifact_id), value, idempotency_key=f'seed:{artifact_id}')
