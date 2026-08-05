import pytest

from evo.operations.dataset.topic_discovery import topic_discovery_manifest


def _cluster(cluster_id, cluster_type, topics, chunk_ids, **overrides):
    value = {
        'cluster_id': cluster_id,
        'cluster_type': cluster_type,
        'topics': topics,
        'chunk_ids': chunk_ids,
        'chunk_count': len(chunk_ids),
    }
    value.update(overrides)
    return value


def _inputs(*, entity_clusters=None, embedding_clusters=None):
    return {
        'entity_clusters': {'clusters': entity_clusters if entity_clusters is not None else [
            _cluster('entity_cluster_1', 'entity', ['Tesla'], ['chunk-1']),
        ]},
        'embedding_clusters': {'clusters': embedding_clusters if embedding_clusters is not None else [
            _cluster('embedding_cluster_1', 'embedding', ['mobility'], ['chunk-2', 'chunk-3']),
        ]},
    }


def test_manifest_flattens_clusters_to_topics_in_source_order():
    # 发布 Artifact 按 entity Cluster/Topic、embedding Cluster/Topic 的稳定顺序展平。
    manifest = topic_discovery_manifest(None, _inputs(
        entity_clusters=[
            _cluster('entity_cluster_1', 'entity', ['Tesla', 'EV'], ['chunk-1']),
            _cluster('entity_cluster_2', 'entity', ['SpaceX'], ['chunk-2']),
        ],
        embedding_clusters=[
            _cluster('embedding_cluster_1', 'embedding', ['mobility', 'supply chain'], ['chunk-3', 'chunk-4']),
        ],
    ))['topic_discovery_manifest']

    assert [(topic['name'], topic['question_type']) for topic in manifest['topics']] == [
        ('Tesla', 'precision'), ('EV', 'precision'), ('SpaceX', 'precision'),
        ('mobility', 'reasoning'), ('supply chain', 'reasoning'),
    ]
    assert [topic['chunk_ids'] for topic in manifest['topics']] == [
        ['chunk-1'], ['chunk-1'], ['chunk-2'], ['chunk-3', 'chunk-4'], ['chunk-3', 'chunk-4'],
    ]
    assert [topic['chunk_count'] for topic in manifest['topics']] == [1, 1, 1, 2, 2]
    assert manifest['stats'] == {
        'total_topic_count': 5,
        'question_types': {'precision': {'count': 3}, 'reasoning': {'count': 2}},
    }


def test_manifest_uses_unique_opaque_ids_and_allows_duplicate_names():
    # Topic 身份由 topic_id 定义；不同来源的同名 Topic 必须保留且 ID 唯一。
    topics = topic_discovery_manifest(None, _inputs(
        entity_clusters=[_cluster('entity_cluster_1', 'entity', ['Tesla'], ['chunk-1'])],
        embedding_clusters=[_cluster('embedding_cluster_1', 'embedding', ['Tesla'], ['chunk-2'])],
    ))['topic_discovery_manifest']['topics']

    assert [topic['name'] for topic in topics] == ['Tesla', 'Tesla']
    assert len({topic['topic_id'] for topic in topics}) == 2
    assert all(topic['topic_id'].strip() for topic in topics)


def test_manifest_does_not_expose_internal_cluster_fields():
    # 前端只依赖 Topic 契约，算法 Cluster ID、评分和元数据均不得透传。
    manifest = topic_discovery_manifest(None, _inputs(
        entity_clusters=[_cluster(
            'entity_cluster_1', 'entity', ['Tesla'], ['chunk-1'], scores={'score': 0.9}, metadata={'debug': True},
        )],
    ))['topic_discovery_manifest']

    assert set(manifest) == {'topics', 'stats'}
    assert set(manifest['topics'][0]) == {'topic_id', 'name', 'question_type', 'chunk_ids', 'chunk_count'}


def test_manifest_returns_a_valid_empty_topic_collection():
    # 没有可发现主题时，仍须产生可查询的稳定空 Artifact，而非失败或缺失字段。
    manifest = topic_discovery_manifest(
        None, _inputs(entity_clusters=[], embedding_clusters=[]),
    )['topic_discovery_manifest']

    assert manifest == {
        'topics': [],
        'stats': {
            'total_topic_count': 0,
            'question_types': {'precision': {'count': 0}, 'reasoning': {'count': 0}},
        },
    }


@pytest.mark.parametrize(
    ('entity_clusters', 'match'),
    [
        # 上游分支类型不可混用，避免错误题型进入发布 Artifact。
        ([_cluster('entity_cluster_1', 'embedding', ['Tesla'], ['chunk-1'])], 'cluster_type must be entity'),
        # 空 Topic、空 Chunk ID 和不一致计数都会导致详情引用与容量统计不可信。
        ([_cluster('entity_cluster_1', 'entity', [], ['chunk-1'])], 'topics must be non-empty'),
        ([_cluster('entity_cluster_1', 'entity', ['Tesla'], [''])], 'chunk_ids must contain only non-empty strings'),
        ([_cluster('entity_cluster_1', 'entity', ['Tesla'], ['chunk-1'], chunk_count=2)], 'chunk_count must match chunk_ids length'),
    ],
)
def test_manifest_rejects_invalid_source_cluster_contract(entity_clusters, match):
    with pytest.raises(ValueError, match=match):
        topic_discovery_manifest(None, _inputs(entity_clusters=entity_clusters))
