#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace


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

from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef
from evo.operations.dataset.chunks_build import build_chunks, build_chunks_manifest, target_chunk_count
from evo.operations.dataset.select_docs import select_docs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run a real test for dataset.build_chunks.')
    parser.add_argument('--kb-id', required=True, help='Knowledge base id to inspect.')
    parser.add_argument('--max-docs', type=int, default=3, help='How many docs select_docs should keep.')
    parser.add_argument('--target-case-count', type=int, default=100,
                        help='target_case_count passed into source_config.')
    parser.add_argument('--groups', nargs='*', default=['block'], help='Chunk groups used by build_chunks.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    select_inputs = {
        'source_config': {
            'kb_id': args.kb_id,
            'max_docs': args.max_docs,
            'target_case_count': args.target_case_count,
        }
    }
    build_params = {'groups': args.groups}

    print('== Select Inputs ==')
    print(json.dumps(select_inputs, ensure_ascii=False, indent=2))
    print('\n== Build Params ==')
    print(json.dumps(build_params, ensure_ascii=False, indent=2))

    selected_result = select_docs(ctx=None, inputs=select_inputs)
    selected_docs = selected_result['selected_docs']
    target = target_chunk_count(selected_docs)
    partitions = tuple(f'chunk_{index:04d}' for index in range(1, target + 1))

    chunks = tuple(
        build_chunks(
            SimpleNamespace(output_key_by_name={'chunk': ArtifactKey('dataset.chunk', partition)}),
            {'selected_docs': selected_docs, 'build_chunks_params': build_params},
        )['chunk']
        for partition in partitions
    )

    manifest = build_chunks_manifest(
        SimpleNamespace(input_ref_by_key={
            ArtifactKey.of('dataset.selected_docs'): ArtifactRef(ArtifactKey.of('dataset.selected_docs'), 1),
            **{
                ArtifactKey('dataset.chunk', partition): ArtifactRef(ArtifactKey('dataset.chunk', partition), index)
                for index, partition in enumerate(partitions, start=1)
            },
        }),
        {'selected_docs': selected_docs, 'chunk': chunks, 'build_chunks_params': build_params},
    )['built_chunks']

    print('\n== Selected Docs ==')
    print(json.dumps(selected_docs, ensure_ascii=False, indent=2))
    print('\n== Chunk Slots ==')
    print(json.dumps(chunks, ensure_ascii=False, indent=2))
    print('\n== Built Manifest ==')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print('\n== Summary ==')
    print(json.dumps({
        'target_chunk_count': target,
        'available_chunk_count': manifest['stats']['chunk_count'],
        'empty_slot_count': manifest['stats']['empty_count'],
        'partitions': list(partitions),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
