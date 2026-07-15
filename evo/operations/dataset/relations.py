from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any

from ...artifacts import ArtifactDraft, ArtifactRef
from ...runtime import OperationContext, OperationOutput
from .utils import expected_ref

DEFAULT_ENTITY_SIMILARITY_THRESHOLD = 0.9
DEFAULT_EDGE_SCORE_THRESHOLD = 0.01
DEFAULT_NOISY_ENTITY_TOP_PERCENT = 0.05
DEFAULT_MIN_CLUSTER_CHUNK_COUNT = 1
DEFAULT_EMBEDDING_MIN_SAMPLES = 2
DEFAULT_UMAP_N_NEIGHBORS = 15
DEFAULT_EMBEDDING_MIN_CLUSTER_SIZE = 2


@dataclass(frozen=True)
class BuildEntityRelationGraphParams:
    entity_similarity_threshold: float = DEFAULT_ENTITY_SIMILARITY_THRESHOLD
    edge_score_threshold: float = DEFAULT_EDGE_SCORE_THRESHOLD
    noisy_entity_top_percent: float = DEFAULT_NOISY_ENTITY_TOP_PERCENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'BuildEntityRelationGraphParams':
        return cls(
            entity_similarity_threshold=_bounded_float(
                data.get('entity_similarity_threshold'),
                DEFAULT_ENTITY_SIMILARITY_THRESHOLD,
                'entity_similarity_threshold',
            ),
            edge_score_threshold=_bounded_float(
                data.get('edge_score_threshold'),
                DEFAULT_EDGE_SCORE_THRESHOLD,
                'edge_score_threshold',
            ),
            noisy_entity_top_percent=_bounded_float(
                data.get('noisy_entity_top_percent'),
                DEFAULT_NOISY_ENTITY_TOP_PERCENT,
                'noisy_entity_top_percent',
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'entity_similarity_threshold': self.entity_similarity_threshold,
            'edge_score_threshold': self.edge_score_threshold,
            'noisy_entity_top_percent': self.noisy_entity_top_percent,
        }


@dataclass(frozen=True)
class DiscoverEntityTopicClustersParams:
    min_cluster_chunk_count: int = DEFAULT_MIN_CLUSTER_CHUNK_COUNT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'DiscoverEntityTopicClustersParams':
        return cls(min_cluster_chunk_count=_positive_int(
            data.get('min_cluster_chunk_count'), DEFAULT_MIN_CLUSTER_CHUNK_COUNT, 'min_cluster_chunk_count',
        ))

    def to_dict(self) -> dict[str, Any]:
        return {'min_cluster_chunk_count': self.min_cluster_chunk_count}


@dataclass(frozen=True)
class DiscoverEmbeddingTopicClustersParams:
    min_samples: int = DEFAULT_EMBEDDING_MIN_SAMPLES
    umap_n_neighbors: int = DEFAULT_UMAP_N_NEIGHBORS
    min_cluster_size: int = DEFAULT_EMBEDDING_MIN_CLUSTER_SIZE

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'DiscoverEmbeddingTopicClustersParams':
        return cls(
            min_samples=_positive_int(data.get('min_samples'), DEFAULT_EMBEDDING_MIN_SAMPLES, 'min_samples'),
            umap_n_neighbors=_positive_int(
                data.get('umap_n_neighbors'), DEFAULT_UMAP_N_NEIGHBORS, 'umap_n_neighbors',
            ),
            min_cluster_size=_positive_int(
                data.get('min_cluster_size'), DEFAULT_EMBEDDING_MIN_CLUSTER_SIZE, 'min_cluster_size',
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'min_samples': self.min_samples,
            'umap_n_neighbors': self.umap_n_neighbors,
            'min_cluster_size': self.min_cluster_size,
        }


@dataclass(frozen=True)
class _GraphInputs:
    built_ref: ArtifactRef
    chunk_entities_ref: ArtifactRef
    chunk_refs: list[str]
    entity_refs: list[ArtifactRef]


@dataclass(frozen=True)
class _AvailableChunk:
    chunk_ref: str
    entities: list[str]


@dataclass(frozen=True)
class _CollectedChunks:
    available: list[_AvailableChunk]
    skipped: list[dict[str, str]]


@dataclass(frozen=True)
class _TopicInputs:
    graph_ref: ArtifactRef
    chunk_entities_ref: ArtifactRef
    chunk_refs: list[str]
    edges: list[dict[str, Any]]
    entities_by_chunk_ref: dict[str, list[str]]
    skipped_chunk_count: int


@dataclass(frozen=True)
class _TopicCluster:
    topic: list[str]
    chunk_refs: list[str]
    source: str


@dataclass(frozen=True)
class _TopicClusters:
    clusters: list[_TopicCluster]
    second_merge_count: int
    isolated_cluster_count: int


@dataclass(frozen=True)
class _EmbeddingChunk:
    chunk_ref: str
    vector: list[float]


@dataclass(frozen=True)
class _EmbeddingPool:
    built_ref: ArtifactRef
    source_chunk_count: int
    chunks: list[_EmbeddingChunk]
    skipped: list[dict[str, str]]


@dataclass(frozen=True)
class _EmbeddingClusters:
    clusters: list[_TopicCluster]
    singleton_cluster_count: int


class BuildEntityRelationGraphOperation:
    def execute(self, ctx: OperationContext) -> OperationOutput:
        params = BuildEntityRelationGraphParams.from_dict(ctx.params)
        graph_inputs = self._graph_inputs(ctx)
        ctx.report_progress(
            phase='build_entity_relation_graph', status='running', message='building entity relation graph',
        )
        collected = self._collect_available_chunks(ctx, graph_inputs)
        if not collected.available:
            raise ValueError('EntityRelationGraph requires at least one valid chunk')
        noisy_entities = self._noisy_entities(collected.available, params)
        edges = self._build_edges(ctx, collected.available, noisy_entities, params)
        payload = self._payload(graph_inputs, collected, edges, noisy_entities, params)
        ctx.report_progress(
            phase='build_entity_relation_graph', status='success', message='built entity relation graph',
            detail={
                'graph_chunk_count': payload['stats']['graph_chunk_count'],
                'skipped_chunk_count': payload['stats']['skipped_chunk_count'],
                'edge_count': payload['stats']['edge_count'],
            },
        )
        return OperationOutput([ArtifactDraft(
            'entity_relation_graph', 'EntityRelationGraph', payload, ctx.operation_run_id,
            input_refs=[graph_inputs.built_ref, graph_inputs.chunk_entities_ref],
        )])

    def _graph_inputs(self, ctx: OperationContext) -> _GraphInputs:
        built_ref, built = self._single_artifact(ctx, 'BuiltChunks')
        chunk_entities_ref, chunk_entities = self._single_artifact(ctx, 'ChunkEntities')
        chunk_refs = _artifact_ref_strings(built.get('chunk_refs'), 'BuiltChunks.chunk_refs')
        if not chunk_refs:
            raise ValueError('BuiltChunks.chunk_refs is required')
        if str(chunk_entities.get('source_ref') or '') != str(built_ref):
            raise ValueError('ChunkEntities.source_ref must match BuiltChunks ref')
        entity_refs = [
            ArtifactRef.parse(value)
            for value in _artifact_ref_strings(chunk_entities.get('chunk_entity_refs'), 'ChunkEntities.chunk_entity_refs')
        ]
        return _GraphInputs(built_ref, chunk_entities_ref, chunk_refs, entity_refs)

    def _single_artifact(self, ctx: OperationContext, schema_name: str) -> tuple[ArtifactRef, dict[str, Any]]:
        refs = [ref for ref in ctx.input_refs if ctx.artifact_graph.schema_name(ref) == schema_name]
        if not refs:
            raise ValueError(f'BuildEntityRelationGraphOperation requires {schema_name} input_ref')
        if len(refs) > 1:
            raise ValueError(f'BuildEntityRelationGraphOperation requires exactly one {schema_name} input_ref')
        return refs[0], ctx.artifact_graph.get(refs[0])

    def _collect_available_chunks(self, ctx: OperationContext, graph_inputs: _GraphInputs) -> _CollectedChunks:
        available: list[_AvailableChunk] = []
        skipped: list[dict[str, str]] = []
        for index, chunk_ref in enumerate(graph_inputs.chunk_refs):
            ctx.check_interrupt()
            if index >= len(graph_inputs.entity_refs):
                skipped.append(_skipped(chunk_ref, 'missing_chunk_entity', 'no ChunkEntity found for chunk ref'))
                continue
            entity_ref = graph_inputs.entity_refs[index]
            entity = ctx.artifact_graph.get(entity_ref)
            entity_chunk_ref = str(entity.get('chunk_ref') or '')
            if entity_chunk_ref != chunk_ref:
                skipped.append(_skipped(
                    chunk_ref, 'chunk_entity_mismatch',
                    f'expected {chunk_ref}, got {entity_chunk_ref or "<empty>"}',
                ))
                continue
            try:
                entities = _validate_entities(entity.get('entities'))
            except ValueError as exc:
                skipped.append(_skipped(chunk_ref, 'invalid_entities', str(exc)))
                continue
            available.append(_AvailableChunk(chunk_ref, entities))
        return _CollectedChunks(available, skipped)

    def _noisy_entities(
        self,
        chunks: list[_AvailableChunk],
        params: BuildEntityRelationGraphParams,
    ) -> set[str]:
        counts = Counter(_entity_key(entity) for chunk in chunks for entity in chunk.entities)
        if not counts:
            return set()
        noisy_count = int(len(counts) * params.noisy_entity_top_percent)
        return {entity for entity, _ in counts.most_common(noisy_count)}

    def _build_edges(
        self,
        ctx: OperationContext,
        chunks: list[_AvailableChunk],
        noisy_entities: set[str],
        params: BuildEntityRelationGraphParams,
    ) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for left_index, left in enumerate(chunks):
            ctx.check_interrupt()
            for right in chunks[left_index + 1:]:
                edge = _overlap_edge(left, right, noisy_entities, params)
                if edge is not None:
                    edges.append(edge)
        return edges

    def _payload(
        self,
        graph_inputs: _GraphInputs,
        collected: _CollectedChunks,
        edges: list[dict[str, Any]],
        noisy_entities: set[str],
        params: BuildEntityRelationGraphParams,
    ) -> dict[str, Any]:
        chunk_refs = [chunk.chunk_ref for chunk in collected.available]
        return {
            'source_refs': {
                'built_chunks': str(graph_inputs.built_ref),
                'chunk_entities': str(graph_inputs.chunk_entities_ref),
            },
            'chunk_refs': chunk_refs,
            'edges': edges,
            'skipped_chunks': list(collected.skipped),
            'stats': {
                'source_chunk_count': len(graph_inputs.chunk_refs),
                'graph_chunk_count': len(chunk_refs),
                'skipped_chunk_count': len(collected.skipped),
                'edge_count': len(edges),
                'noisy_entity_count': len(noisy_entities),
                'isolated_chunk_count': _isolated_chunk_count(chunk_refs, edges),
            },
            'params': params.to_dict(),
        }


class DiscoverEmbeddingTopicClustersOperation:
    def __init__(self, clusterer: Any | None = None):
        self.clusterer = clusterer

    def execute(self, ctx: OperationContext) -> OperationOutput:
        params = DiscoverEmbeddingTopicClustersParams.from_dict(ctx.params)
        built_ref, built = self._built_chunks(ctx)
        ctx.report_progress(
            phase='discover_embedding_topic_clusters', status='running',
            message='discovering embedding topic clusters',
        )
        pool = self._collect_embedding_chunks(ctx, built_ref, built)
        if not pool.chunks:
            raise ValueError('DiscoverEmbeddingTopicClustersOperation requires at least one valid embedding chunk')
        matrix = self._normalize_embeddings(pool)
        reduced = self._reduce_embeddings(ctx, matrix, params)
        labels = self._cluster_embeddings(ctx, reduced, params)
        clusters = self._build_clusters(ctx, pool, labels)
        artifacts = self._artifact_drafts(ctx, pool, clusters, params)
        ctx.report_progress(
            phase='discover_embedding_topic_clusters', status='success',
            message='discovered embedding topic clusters',
            detail={
                'cluster_count': len(clusters.clusters),
                'embedding_chunk_count': len(pool.chunks),
                'skipped_chunk_count': len(pool.skipped),
                'singleton_cluster_count': clusters.singleton_cluster_count,
            },
        )
        return OperationOutput(artifacts)

    def _built_chunks(self, ctx: OperationContext) -> tuple[ArtifactRef, dict[str, Any]]:
        refs = [ref for ref in ctx.input_refs if ctx.artifact_graph.schema_name(ref) == 'BuiltChunks']
        if not refs:
            raise ValueError('DiscoverEmbeddingTopicClustersOperation requires BuiltChunks input_ref')
        if len(refs) > 1:
            raise ValueError('DiscoverEmbeddingTopicClustersOperation requires exactly one BuiltChunks input_ref')
        built = ctx.artifact_graph.get(refs[0])
        if not built.get('chunk_refs'):
            raise ValueError('BuiltChunks.chunk_refs is required')
        return refs[0], built

    def _collect_embedding_chunks(
        self,
        ctx: OperationContext,
        built_ref: ArtifactRef,
        built: dict[str, Any],
    ) -> _EmbeddingPool:
        chunks: list[_EmbeddingChunk] = []
        skipped: list[dict[str, str]] = []
        expected_dimension: int | None = None
        chunk_refs = _artifact_ref_strings(built.get('chunk_refs'), 'BuiltChunks.chunk_refs')
        for chunk_ref in chunk_refs:
            ctx.check_interrupt()
            ref = ArtifactRef.parse(chunk_ref)
            try:
                if ctx.artifact_graph.schema_name(ref) != 'DatasetChunk':
                    skipped.append(_skipped(chunk_ref, 'missing_dataset_chunk', 'artifact is not DatasetChunk'))
                    continue
                chunk = ctx.artifact_graph.get(ref)
            except (FileNotFoundError, KeyError):
                skipped.append(_skipped(chunk_ref, 'missing_dataset_chunk', 'DatasetChunk artifact not found'))
                continue
            try:
                vector = _select_embedding_vector(chunk.get('embedding'))
            except ValueError as exc:
                skipped.append(_skipped(chunk_ref, _embedding_skip_reason(str(exc)), str(exc)))
                continue
            if expected_dimension is None:
                expected_dimension = len(vector)
            elif len(vector) != expected_dimension:
                skipped.append(_skipped(
                    chunk_ref, 'dimension_mismatch',
                    f'embedding dimension {len(vector)} does not match expected {expected_dimension}',
                ))
                continue
            chunks.append(_EmbeddingChunk(chunk_ref, vector))
        return _EmbeddingPool(built_ref, len(chunk_refs), chunks, skipped)

    def _normalize_embeddings(self, pool: _EmbeddingPool) -> list[list[float]]:
        return [_normalize_vector(chunk.vector) for chunk in pool.chunks]

    def _reduce_embeddings(
        self,
        ctx: OperationContext,
        matrix: list[list[float]],
        params: DiscoverEmbeddingTopicClustersParams,
    ) -> list[list[float]]:
        ctx.check_interrupt()
        return _umap_reduce(matrix, params)

    def _cluster_embeddings(
        self,
        ctx: OperationContext,
        matrix: list[list[float]],
        params: DiscoverEmbeddingTopicClustersParams,
    ) -> list[int]:
        ctx.check_interrupt()
        if self.clusterer is not None:
            labels = self.clusterer(matrix, params)
        else:
            labels = _hdbscan_cluster(matrix, params)
        if not isinstance(labels, list) or len(labels) != len(matrix):
            raise ValueError('embedding clusterer must return one label per chunk')
        output: list[int] = []
        for label in labels:
            if isinstance(label, bool) or not isinstance(label, Integral):
                raise ValueError('embedding cluster labels must be integers')
            output.append(int(label))
        return output

    def _build_clusters(
        self,
        ctx: OperationContext,
        pool: _EmbeddingPool,
        labels: list[int],
    ) -> _EmbeddingClusters:
        by_label: dict[int, list[str]] = {}
        singleton_cluster_count = 0
        for chunk, label in zip(pool.chunks, labels):
            ctx.check_interrupt()
            if label == -1:
                singleton_cluster_count += 1
                by_label[_noise_label(singleton_cluster_count)] = [chunk.chunk_ref]
                continue
            by_label.setdefault(label, []).append(chunk.chunk_ref)
        order = {chunk.chunk_ref: index for index, chunk in enumerate(pool.chunks)}
        clusters = [
            _TopicCluster([], sorted(chunk_refs, key=lambda ref: order[ref]), 'embedding')
            for _, chunk_refs in sorted(by_label.items(), key=lambda item: min(order[ref] for ref in item[1]))
        ]
        return _EmbeddingClusters(clusters, singleton_cluster_count)

    def _artifact_drafts(
        self,
        ctx: OperationContext,
        pool: _EmbeddingPool,
        clusters: _EmbeddingClusters,
        params: DiscoverEmbeddingTopicClustersParams,
    ) -> list[ArtifactDraft]:
        cluster_drafts = [
            ArtifactDraft(
                f'embedding_topic_cluster_{index:06d}', 'TopicCluster',
                {
                    'source_ref': str(pool.built_ref),
                    'cluster_type': 'embedding',
                    'topic': list(cluster.topic),
                    'chunk_refs': list(cluster.chunk_refs),
                },
                ctx.operation_run_id,
                input_refs=[pool.built_ref],
            )
            for index, cluster in enumerate(clusters.clusters, 1)
        ]
        manifest_payload = self._manifest_payload(ctx, pool, clusters, cluster_drafts, params)
        return [*cluster_drafts, ArtifactDraft(
            'embedding_topic_clusters', 'EmbeddingTopicClusters', manifest_payload, ctx.operation_run_id,
            input_refs=[pool.built_ref],
        )]

    def _manifest_payload(
        self,
        ctx: OperationContext,
        pool: _EmbeddingPool,
        clusters: _EmbeddingClusters,
        cluster_drafts: list[ArtifactDraft],
        params: DiscoverEmbeddingTopicClustersParams,
    ) -> dict[str, Any]:
        assigned = {chunk_ref for cluster in clusters.clusters for chunk_ref in cluster.chunk_refs}
        return {
            'source_ref': str(pool.built_ref),
            'cluster_refs': [expected_ref(ctx, draft) for draft in cluster_drafts],
            'skipped_chunks': list(pool.skipped),
            'stats': {
                'source_chunk_count': pool.source_chunk_count,
                'embedding_chunk_count': len(pool.chunks),
                'skipped_chunk_count': len(pool.skipped),
                'cluster_count': len(clusters.clusters),
                'assigned_chunk_count': len(assigned),
                'singleton_cluster_count': clusters.singleton_cluster_count,
            },
            'params': params.to_dict(),
        }


class DiscoverEntityTopicClustersOperation:
    def execute(self, ctx: OperationContext) -> OperationOutput:
        params = DiscoverEntityTopicClustersParams.from_dict(ctx.params)
        graph_ref, graph = self._entity_relation_graph(ctx)
        chunk_entities_ref, chunk_entities = self._chunk_entities_manifest(ctx)
        ctx.report_progress(
            phase='discover_entity_topic_clusters', status='running',
            message='discovering entity topic clusters',
        )
        inputs = self._load_and_validate_inputs(ctx, graph_ref, graph, chunk_entities_ref, chunk_entities)
        topic_buckets = self._build_topic_buckets(ctx, inputs)
        clusters = self._build_clusters(ctx, inputs, topic_buckets, params)
        artifacts = self._artifact_drafts(ctx, inputs, clusters, params)
        ctx.report_progress(
            phase='discover_entity_topic_clusters', status='success',
            message='discovered entity topic clusters',
            detail={
                'cluster_count': len(clusters.clusters),
                'isolated_cluster_count': clusters.isolated_cluster_count,
            },
        )
        return OperationOutput(artifacts)

    def _entity_relation_graph(self, ctx: OperationContext) -> tuple[ArtifactRef, dict[str, Any]]:
        return self._single_artifact(ctx, 'EntityRelationGraph')

    def _chunk_entities_manifest(self, ctx: OperationContext) -> tuple[ArtifactRef, dict[str, Any]]:
        return self._single_artifact(ctx, 'ChunkEntities')

    def _single_artifact(self, ctx: OperationContext, schema_name: str) -> tuple[ArtifactRef, dict[str, Any]]:
        refs = [ref for ref in ctx.input_refs if ctx.artifact_graph.schema_name(ref) == schema_name]
        if not refs:
            raise ValueError(f'DiscoverEntityTopicClustersOperation requires {schema_name} input_ref')
        if len(refs) > 1:
            raise ValueError(f'DiscoverEntityTopicClustersOperation requires exactly one {schema_name} input_ref')
        return refs[0], ctx.artifact_graph.get(refs[0])

    def _load_and_validate_inputs(
        self,
        ctx: OperationContext,
        graph_ref: ArtifactRef,
        graph: dict[str, Any],
        chunk_entities_ref: ArtifactRef,
        chunk_entities: dict[str, Any],
    ) -> _TopicInputs:
        source_refs = graph.get('source_refs') if isinstance(graph.get('source_refs'), dict) else {}
        if str(source_refs.get('chunk_entities') or '') != str(chunk_entities_ref):
            raise ValueError('EntityRelationGraph.source_refs.chunk_entities must match ChunkEntities ref')
        chunk_refs = _artifact_ref_strings(graph.get('chunk_refs'), 'EntityRelationGraph.chunk_refs')
        if not chunk_refs:
            raise ValueError('EntityRelationGraph.chunk_refs is required')
        edges = self._validate_edges(graph.get('edges'), set(chunk_refs))
        entities_by_chunk_ref = self._entities_by_chunk_ref(ctx, chunk_entities)
        missing = [chunk_ref for chunk_ref in chunk_refs if chunk_ref not in entities_by_chunk_ref]
        if missing:
            raise ValueError(f'missing ChunkEntity for EntityRelationGraph chunk refs: {missing}')
        skipped_chunks = graph.get('skipped_chunks') if isinstance(graph.get('skipped_chunks'), list) else []
        return _TopicInputs(
            graph_ref, chunk_entities_ref, chunk_refs, edges, entities_by_chunk_ref, len(skipped_chunks),
        )

    def _validate_edges(self, value: Any, chunk_refs: set[str]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError('EntityRelationGraph.edges must be list')
        edges: list[dict[str, Any]] = []
        for edge in value:
            if not isinstance(edge, dict):
                raise ValueError('EntityRelationGraph.edges must contain only objects')
            source_ref = str(edge.get('source_chunk_ref') or '')
            target_ref = str(edge.get('target_chunk_ref') or '')
            if source_ref not in chunk_refs or target_ref not in chunk_refs:
                raise ValueError('edge endpoint must belong to EntityRelationGraph.chunk_refs')
            overlapped_items = edge.get('overlapped_items')
            if not isinstance(overlapped_items, list):
                raise ValueError('edge.overlapped_items must be list')
            for item in overlapped_items:
                _topic_from_overlapped_item(item)
            edges.append(edge)
        return edges

    def _entities_by_chunk_ref(self, ctx: OperationContext, chunk_entities: dict[str, Any]) -> dict[str, list[str]]:
        entity_refs = [
            ArtifactRef.parse(value)
            for value in _artifact_ref_strings(chunk_entities.get('chunk_entity_refs'), 'ChunkEntities.chunk_entity_refs')
        ]
        by_chunk_ref: dict[str, list[str]] = {}
        for entity_ref in entity_refs:
            ctx.check_interrupt()
            entity = ctx.artifact_graph.get(entity_ref)
            chunk_ref = str(entity.get('chunk_ref') or '')
            if not chunk_ref:
                raise ValueError('ChunkEntity.chunk_ref is required')
            if chunk_ref in by_chunk_ref:
                raise ValueError(f'duplicate ChunkEntity.chunk_ref: {chunk_ref}')
            by_chunk_ref[chunk_ref] = _validate_entities(entity.get('entities'))
        return by_chunk_ref

    def _build_topic_buckets(
        self,
        ctx: OperationContext,
        inputs: _TopicInputs,
    ) -> tuple[dict[str, set[str]], int]:
        buckets: dict[str, set[str]] = {}
        second_merge_count = 0
        for edge in inputs.edges:
            ctx.check_interrupt()
            source_ref = str(edge.get('source_chunk_ref') or '')
            target_ref = str(edge.get('target_chunk_ref') or '')
            for item in edge.get('overlapped_items') or []:
                topic = _topic_from_overlapped_item(item)
                if topic in buckets:
                    second_merge_count += 1
                buckets.setdefault(topic, set()).update({source_ref, target_ref})
        return buckets, second_merge_count

    def _build_clusters(
        self,
        ctx: OperationContext,
        inputs: _TopicInputs,
        topic_buckets: tuple[dict[str, set[str]], int],
        params: DiscoverEntityTopicClustersParams,
    ) -> _TopicClusters:
        buckets, second_merge_count = topic_buckets
        order = {chunk_ref: index for index, chunk_ref in enumerate(inputs.chunk_refs)}
        edge_clusters = self._edge_topic_clusters(ctx, buckets, order, params)
        covered = {chunk_ref for cluster in edge_clusters for chunk_ref in cluster.chunk_refs}
        singleton_clusters = self._singleton_clusters(ctx, inputs, covered)
        return _TopicClusters(
            clusters=[*edge_clusters, *singleton_clusters],
            second_merge_count=second_merge_count,
            isolated_cluster_count=len(singleton_clusters),
        )

    def _edge_topic_clusters(
        self,
        ctx: OperationContext,
        buckets: dict[str, set[str]],
        order: dict[str, int],
        params: DiscoverEntityTopicClustersParams,
    ) -> list[_TopicCluster]:
        clusters: list[_TopicCluster] = []
        sorted_topics = sorted(buckets, key=lambda topic: (min(order[ref] for ref in buckets[topic]), topic))
        for topic in sorted_topics:
            ctx.check_interrupt()
            chunk_refs = sorted(buckets[topic], key=lambda ref: order[ref])
            if len(chunk_refs) < params.min_cluster_chunk_count:
                continue
            clusters.append(_TopicCluster([topic], chunk_refs, 'edge_topic'))
        return clusters

    def _singleton_clusters(
        self,
        ctx: OperationContext,
        inputs: _TopicInputs,
        covered: set[str],
    ) -> list[_TopicCluster]:
        clusters: list[_TopicCluster] = []
        for chunk_ref in inputs.chunk_refs:
            ctx.check_interrupt()
            if chunk_ref in covered:
                continue
            clusters.append(_TopicCluster(list(inputs.entities_by_chunk_ref[chunk_ref]), [chunk_ref], 'isolated_chunk'))
        return clusters

    def _artifact_drafts(
        self,
        ctx: OperationContext,
        inputs: _TopicInputs,
        clusters: _TopicClusters,
        params: DiscoverEntityTopicClustersParams,
    ) -> list[ArtifactDraft]:
        cluster_drafts = [
            ArtifactDraft(
                f'entity_topic_cluster_{index:06d}', 'TopicCluster',
                {
                    'source_ref': str(inputs.graph_ref),
                    'cluster_type': 'entity',
                    'topic': list(cluster.topic),
                    'chunk_refs': list(cluster.chunk_refs),
                },
                ctx.operation_run_id,
                input_refs=[inputs.graph_ref, inputs.chunk_entities_ref],
            )
            for index, cluster in enumerate(clusters.clusters, 1)
        ]
        manifest_payload = self._manifest_payload(ctx, inputs, clusters, cluster_drafts, params)
        return [*cluster_drafts, ArtifactDraft(
            'entity_topic_clusters', 'EntityTopicClusters', manifest_payload, ctx.operation_run_id,
            input_refs=[inputs.graph_ref, inputs.chunk_entities_ref],
        )]

    def _manifest_payload(
        self,
        ctx: OperationContext,
        inputs: _TopicInputs,
        clusters: _TopicClusters,
        cluster_drafts: list[ArtifactDraft],
        params: DiscoverEntityTopicClustersParams,
    ) -> dict[str, Any]:
        return {
            'source_refs': {
                'entity_relation_graph': str(inputs.graph_ref),
                'chunk_entities': str(inputs.chunk_entities_ref),
            },
            'cluster_refs': [expected_ref(ctx, draft) for draft in cluster_drafts],
            'topics': _flat_topics(clusters.clusters),
            'stats': {
                'graph_chunk_count': len(inputs.chunk_refs),
                'source_edge_count': len(inputs.edges),
                'cluster_count': len(clusters.clusters),
                'cluster_size_counts': _cluster_size_counts(clusters.clusters),
                'second_merge_count': clusters.second_merge_count,
                'isolated_cluster_count': clusters.isolated_cluster_count,
                'skipped_chunk_count': inputs.skipped_chunk_count,
            },
            'params': params.to_dict(),
        }


def _bounded_float(value: Any, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a number between 0 and 1') from exc
    if output < 0 or output > 1:
        raise ValueError(f'{name} must be between 0 and 1')
    return output


def _positive_int(value: Any, default: int, name: str) -> int:
    if value is None:
        return default
    try:
        output = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be a positive integer') from exc
    if output < 1:
        raise ValueError(f'{name} must be a positive integer')
    return output


def _select_embedding_vector(value: Any) -> list[float]:
    if not isinstance(value, dict) or not value:
        raise ValueError('DatasetChunk.embedding is empty')
    if len(value) > 1:
        raise ValueError('DatasetChunk.embedding has multiple keys')
    raw_vector = next(iter(value.values()))
    if not isinstance(raw_vector, list):
        raise ValueError('embedding vector must be list[number]')
    if not raw_vector:
        raise ValueError('embedding vector must be non-empty')
    vector: list[float] = []
    for item in raw_vector:
        if isinstance(item, bool) or not isinstance(item, Real):
            raise ValueError('embedding vector must be list[number]')
        vector.append(float(item))
    if _vector_norm(vector) == 0:
        raise ValueError('embedding vector norm must be positive')
    return vector


def _embedding_skip_reason(message: str) -> str:
    if 'empty' in message:
        return 'missing_embedding'
    if 'multiple keys' in message:
        return 'ambiguous_embedding_key'
    return 'invalid_embedding'


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = _vector_norm(vector)
    if norm == 0:
        raise ValueError('embedding vector norm must be positive')
    return [item / norm for item in vector]


def _vector_norm(vector: list[float]) -> float:
    return math.sqrt(sum(item * item for item in vector))


def _umap_reduce(
    matrix: list[list[float]],
    params: DiscoverEmbeddingTopicClustersParams,
) -> list[list[float]]:
    if len(matrix) <= 2:
        return matrix
    try:
        import umap  # type: ignore
    except ImportError:
        return matrix
    n_neighbors = min(params.umap_n_neighbors, len(matrix) - 1)
    if n_neighbors < 2:
        return matrix
    reducer = umap.UMAP(n_neighbors=n_neighbors, random_state=42)
    reduced = reducer.fit_transform(matrix)
    return [[float(item) for item in row] for row in reduced.tolist()]


def _hdbscan_cluster(
    matrix: list[list[float]],
    params: DiscoverEmbeddingTopicClustersParams,
) -> list[int]:
    try:
        import hdbscan  # type: ignore
        clusterer = hdbscan.HDBSCAN(min_cluster_size=params.min_cluster_size, min_samples=params.min_samples)
    except ImportError:
        try:
            from sklearn.cluster import HDBSCAN
        except ImportError:
            return _fallback_embedding_cluster(matrix, params)
        clusterer = HDBSCAN(min_cluster_size=params.min_cluster_size, min_samples=params.min_samples)
    return [int(label) for label in clusterer.fit_predict(matrix)]


def _fallback_embedding_cluster(
    matrix: list[list[float]],
    params: DiscoverEmbeddingTopicClustersParams,
) -> list[int]:
    visited: set[int] = set()
    labels = [-1] * len(matrix)
    next_label = 0
    for index in range(len(matrix)):
        if index in visited:
            continue
        component = _embedding_component(index, matrix, visited)
        if len(component) < params.min_cluster_size:
            continue
        for item in component:
            labels[item] = next_label
        next_label += 1
    return labels


def _embedding_component(start: int, matrix: list[list[float]], visited: set[int]) -> list[int]:
    threshold = 0.95
    component: list[int] = []
    frontier = [start]
    visited.add(start)
    while frontier:
        current = frontier.pop()
        component.append(current)
        for candidate in range(len(matrix)):
            if candidate in visited:
                continue
            if _cosine(matrix[current], matrix[candidate]) < threshold:
                continue
            visited.add(candidate)
            frontier.append(candidate)
    return sorted(component)


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(left_item * right_item for left_item, right_item in zip(left, right))


def _noise_label(index: int) -> int:
    return -1_000_000 - index


def _artifact_ref_strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f'{name} must be list[string]')
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f'{name} must contain artifact refs')
        ArtifactRef.parse(item)
        output.append(item)
    return output


def _validate_entities(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError('entities must be list[string]')
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError('entities must contain only non-empty strings')
        output.append(item)
    return output


def _overlap_edge(
    left: _AvailableChunk,
    right: _AvailableChunk,
    noisy_entities: set[str],
    params: BuildEntityRelationGraphParams,
) -> dict[str, Any] | None:
    comparisons = 0
    overlapped_items: list[dict[str, Any]] = []
    for source_entity in left.entities:
        source_key = _entity_key(source_entity)
        if source_key in noisy_entities:
            continue
        for target_entity in right.entities:
            target_key = _entity_key(target_entity)
            if target_key in noisy_entities:
                continue
            comparisons += 1
            similarity = _jaro_winkler(source_key, target_key)
            if similarity >= params.entity_similarity_threshold:
                overlapped_items.append({
                    'source_entity': source_entity,
                    'target_entity': target_entity,
                    'similarity': similarity,
                })
    if comparisons == 0:
        return None
    score = len(overlapped_items) / comparisons
    if score < params.edge_score_threshold:
        return None
    return {
        'source_chunk_ref': left.chunk_ref,
        'target_chunk_ref': right.chunk_ref,
        'type': 'entities_overlap',
        'score': score,
        'overlapped_items': overlapped_items,
    }


def _entity_key(value: str) -> str:
    return value.strip().lower()


def _jaro_winkler(left: str, right: str, prefix_scale: float = 0.1) -> float:
    if left == right:
        return 1.0
    jaro = _jaro_similarity(left, right)
    prefix = 0
    for left_char, right_char in zip(left[:4], right[:4]):
        if left_char != right_char:
            break
        prefix += 1
    return jaro + prefix * prefix_scale * (1 - jaro)


def _jaro_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    match_distance = max(len(left), len(right)) // 2 - 1
    left_matches = [False] * len(left)
    right_matches = [False] * len(right)
    matches = 0
    for left_index, left_char in enumerate(left):
        start = max(0, left_index - match_distance)
        end = min(left_index + match_distance + 1, len(right))
        for right_index in range(start, end):
            if right_matches[right_index] or left_char != right[right_index]:
                continue
            left_matches[left_index] = True
            right_matches[right_index] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    transpositions = 0
    right_index = 0
    for left_index, left_char in enumerate(left):
        if not left_matches[left_index]:
            continue
        while not right_matches[right_index]:
            right_index += 1
        if left_char != right[right_index]:
            transpositions += 1
        right_index += 1
    return (
        matches / len(left)
        + matches / len(right)
        + (matches - transpositions / 2) / matches
    ) / 3


def _skipped(chunk_ref: str, reason: str, detail: str) -> dict[str, str]:
    return {'chunk_ref': chunk_ref, 'reason': reason, 'detail': detail}


def _isolated_chunk_count(chunk_refs: list[str], edges: list[dict[str, Any]]) -> int:
    connected = {
        ref
        for edge in edges
        for ref in (str(edge.get('source_chunk_ref') or ''), str(edge.get('target_chunk_ref') or ''))
        if ref
    }
    return sum(1 for ref in chunk_refs if ref not in connected)


def _topic_from_overlapped_item(item: Any) -> str:
    if not isinstance(item, dict):
        raise ValueError('edge.overlapped_items must contain only objects')
    source_entity = item.get('source_entity')
    target_entity = item.get('target_entity')
    if not isinstance(source_entity, str) or not source_entity.strip():
        raise ValueError('edge.overlapped_items.source_entity must be non-empty string')
    if not isinstance(target_entity, str) or not target_entity.strip():
        raise ValueError('edge.overlapped_items.target_entity must be non-empty string')
    source = source_entity.strip()
    target = target_entity.strip()
    return target if len(target) > len(source) else source


def _flat_topics(clusters: list[_TopicCluster]) -> list[str]:
    topics: list[str] = []
    seen: set[str] = set()
    for cluster in clusters:
        for topic in cluster.topic:
            if topic in seen:
                continue
            seen.add(topic)
            topics.append(topic)
    return topics


def _cluster_size_counts(clusters: list[_TopicCluster]) -> dict[str, int]:
    return dict(Counter(str(len(cluster.chunk_refs)) for cluster in clusters))
