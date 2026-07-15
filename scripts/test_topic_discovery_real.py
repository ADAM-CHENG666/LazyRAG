#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    candidates = [
        Path(__file__).resolve().parents[1],
        Path('/app'),
        Path('/Users/huangsicheng/Documents/projects/LazyRAG'),
    ]
    for root in candidates:
        if not (root / 'evo').exists():
            continue
        for path in (root, root / 'algorithm'):
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
        return


_bootstrap_paths()

from evo.operations.dataset.topic_discovery import (
    topic_discovery_embedding_cluster,
    topic_discovery_embedding_label,
    topic_discovery_entity_build_graph,
    topic_discovery_entity_cluster,
    topic_discovery_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run a real smoke test for dataset topic discovery.')
    parser.add_argument('--use-real-embedding-cluster-deps', action='store_true',
                        help='Use installed UMAP/HDBSCAN instead of deterministic local stubs.')
    parser.add_argument('--entity-similarity-threshold', type=float, default=0.9)
    parser.add_argument('--topic-merge-similarity-threshold', type=float, default=0.95)
    parser.add_argument('--max-topics-per-cluster', type=int, default=3)
    parser.add_argument('--label-json', default='{"topics":["mobility"]}',
                        help='JSON returned by the local embedding label LLM stub.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    llm_calls = 0

    def complete(prompt: str) -> str:
        nonlocal llm_calls
        llm_calls += 1
        return args.label_json

    chunk_entities = (
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
         'entities': ['Tesla', 'EV']},
        {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block',
         'entities': ['Tesla', 'Battery']},
        {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block',
         'entities': ['SpaceX', 'Rocket']},
    )
    chunks = (
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
         'text': 'Tesla builds electric vehicles and batteries.', 'embedding': {'default': [1.0, 0.0]}},
        {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'block',
         'text': 'Tesla battery systems support electric mobility.', 'embedding': {'default': [0.99, 0.01]}},
        {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-3', 'group': 'block',
         'text': 'SpaceX launches rockets to orbit.', 'embedding': {'default': [0.0, 1.0]}},
    )

    print('== Inputs ==')
    print(json.dumps({'chunk_entities': chunk_entities, 'chunks': chunks}, ensure_ascii=False, indent=2))

    entity_graph = topic_discovery_entity_build_graph(None, {
        'chunk_entity': chunk_entities,
        'topic_discovery_entity_build_graph_params': {
            'entity_similarity_threshold': args.entity_similarity_threshold,
        },
    })['entity_graph']
    print('\n== Entity Graph ==')
    print(json.dumps(entity_graph, ensure_ascii=False, indent=2))

    entity_clusters = topic_discovery_entity_cluster(None, {
        'entity_graph': entity_graph,
        'topic_discovery_entity_cluster_params': {
            'topic_merge_similarity_threshold': args.topic_merge_similarity_threshold,
        },
    })['entity_clusters']
    print('\n== Entity Clusters ==')
    print(json.dumps(entity_clusters, ensure_ascii=False, indent=2))

    embedding_cluster_kwargs = {}
    if not args.use_real_embedding_cluster_deps:
        embedding_cluster_kwargs = {
            'reducer': lambda matrix, params: matrix,
            'clusterer': lambda matrix, params: [0, 0, -1],
        }
    embedding_candidates = topic_discovery_embedding_cluster(None, {
        'chunk': chunks,
        'topic_discovery_embedding_cluster_params': {
            'umap_n_neighbors': 1,
            'umap_n_components': 1,
            'min_cluster_size': 1,
            'min_samples': 1,
        },
    }, **embedding_cluster_kwargs)['embedding_cluster_candidates']
    print('\n== Embedding Cluster Candidates ==')
    print(json.dumps(embedding_candidates, ensure_ascii=False, indent=2))

    embedding_clusters = topic_discovery_embedding_label(None, {
        'embedding_cluster_candidates': embedding_candidates,
        'chunk': chunks,
        'topic_discovery_embedding_label_params': {
            'max_topics_per_cluster': args.max_topics_per_cluster,
        },
    }, llm_complete=complete)['embedding_clusters']
    print('\n== Embedding Clusters ==')
    print(json.dumps(embedding_clusters, ensure_ascii=False, indent=2))

    manifest = topic_discovery_manifest(None, {
        'entity_clusters': entity_clusters,
        'embedding_clusters': embedding_clusters,
    })['topic_discovery_manifest']
    print('\n== Topic Discovery Manifest ==')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

    print('\n== Summary ==')
    print(json.dumps({
        'llm_calls': llm_calls,
        'entity_cluster_count': entity_clusters['stats']['cluster_count'],
        'embedding_candidate_count': embedding_candidates['stats']['candidate_count'],
        'embedding_cluster_count': embedding_clusters['stats']['cluster_count'],
        'manifest_stats': manifest['stats'],
        'used_real_embedding_cluster_deps': args.use_real_embedding_cluster_deps,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
