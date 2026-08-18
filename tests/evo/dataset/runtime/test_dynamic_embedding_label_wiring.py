"""Runtime contracts for partitioned embedding-candidate labeling."""

import importlib

from evo import artifacts as A


dataset_module = importlib.import_module('evo.operations.dataset.operations')


def test_embedding_labeling_declares_its_own_dynamic_partition_set():
    """Candidate partitions are distinct from source Chunk partitions."""
    assert A.DATASET_EMBEDDING_LABEL_REQUESTS == 'dataset.embedding_label_requests'
    assert A.DATASET_EMBEDDING_LABEL_REQUEST == 'dataset.embedding_label_request'
    assert A.DATASET_EMBEDDING_CLUSTER == 'dataset.embedding_cluster'
    assert A.PARTITION_SET_BY_ARTIFACT[A.DATASET_EMBEDDING_LABEL_REQUEST] == (
        A.DATASET_EMBEDDING_LABEL_REQUESTS
    )
    assert A.PARTITION_SET_BY_ARTIFACT[A.DATASET_EMBEDDING_CLUSTER] == (
        A.DATASET_EMBEDDING_LABEL_REQUESTS
    )


def test_embedding_labeling_operation_graph_fans_out_then_joins_with_existing_runtime_limit():
    """The existing four-way Runtime policy schedules ``each`` candidate invocation."""
    cluster_outputs = dataset_module.cluster_embeddings_operation.spec.outputs
    label_inputs = dataset_module.label_embedding_cluster_operation.spec.inputs
    label_outputs = dataset_module.label_embedding_cluster_operation.spec.outputs
    manifest_inputs = dataset_module.embedding_label_manifest_operation.spec.inputs

    assert cluster_outputs['partitions'].artifact_id == A.DATASET_EMBEDDING_LABEL_REQUESTS
    assert cluster_outputs['requests'].artifact_id == A.DATASET_EMBEDDING_LABEL_REQUEST
    assert label_inputs['request'].artifact_id == A.DATASET_EMBEDDING_LABEL_REQUEST
    assert label_inputs['request'].mode == 'each'
    assert label_inputs['request'].partition_set_id == A.DATASET_EMBEDDING_LABEL_REQUESTS
    assert label_outputs['cluster'].artifact_id == A.DATASET_EMBEDDING_CLUSTER
    assert manifest_inputs['clusters'].artifact_id == A.DATASET_EMBEDDING_CLUSTER
    assert manifest_inputs['clusters'].mode == 'all'
    assert manifest_inputs['clusters'].partition_set_id == A.DATASET_EMBEDDING_LABEL_REQUESTS
    assert dataset_module.label_embedding_cluster_operation.spec.max_concurrency == 4


def test_topic_discovery_waits_for_the_joined_embedding_clusters_contract():
    """No partial candidate result can bypass the final embedding-cluster manifest."""
    operation_ids = {operation.spec.op_id for operation in dataset_module.dataset_operations()}

    assert 'dataset.label_embedding_clusters' not in operation_ids
    assert {
        'dataset.cluster_embeddings',
        'dataset.label_embedding_cluster',
        'dataset.embedding_label_manifest',
        'dataset.topic_manifest',
    } <= operation_ids
