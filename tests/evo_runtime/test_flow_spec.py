from __future__ import annotations

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.evo.flow import DatasetFlowSpec, EvoFlowSpec
from evo.artifact_runtime.kernel import ArtifactKey


def test_dataset_stages_expand_chunk_and_case_outputs_from_one_layout():
    spec = EvoFlowSpec(
        ('case_0001', 'case_0002'),
        DatasetFlowSpec(('chunk_0001', 'chunk_0002', 'chunk_0003'), ('case_0001', 'case_0002')),
    )

    # Chunk-building artifacts follow chunk partitions; QA and case artifacts follow case partitions.
    assert spec.step_roots == {
        'dataset.build_chunks': ArtifactKey.of(C.DATASET_BUILD_CHUNKS_MANIFEST),
        'dataset.topic_discovery': ArtifactKey.of(C.DATASET_TOPIC_DISCOVERY_MANIFEST),
        'dataset.qaplan': ArtifactKey.of(C.DATASET_QAPLAN_MANIFEST),
        'dataset.generate': ArtifactKey.of(C.DATASET_GENERATE_MANIFEST),
        'dataset.generate_enhance': ArtifactKey.of(C.DATASET_GENERATE_ENHANCE_MANIFEST),
        'eval': ArtifactKey.of(C.EVAL_SUMMARY),
        'analysis': ArtifactKey.of(C.ANALYSIS_SUMMARY),
        'repair': ArtifactKey.of(C.REPAIR_VERIFIED_PATCH),
        'abtest': ArtifactKey.of(C.ABTEST_COMPARISON),
    }
    assert [key.partition for key in spec.step_output_keys('dataset.build_chunks') if key.artifact_id == C.DATASET_CHUNK] == [
        'chunk_0001', 'chunk_0002', 'chunk_0003',
    ]
    assert [key.partition for key in spec.step_output_keys('dataset.qaplan') if key.artifact_id == C.DATASET_QAPLAN_SPEC] == [
        'case_0001', 'case_0002',
    ]
    assert [key.partition for key in spec.step_output_keys('dataset.generate_enhance') if key.artifact_id == C.DATASET_CASE_ENHANCE] == [
        'case_0001', 'case_0002',
    ]
