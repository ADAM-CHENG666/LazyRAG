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

from evo.operations.dataset.entities import chunk_entities_extract, chunk_entities_extract_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run a real test for dataset.chunk_entities_extract.')
    parser.add_argument('--text', default='Elon Musk leads Tesla and SpaceX.',
                        help='Chunk text to extract entities from.')
    parser.add_argument('--entities-json', default='{"entities":["Elon Musk","Tesla","SpaceX"]}',
                        help='JSON returned by the local LLM stub.')
    parser.add_argument('--max-entities', type=int, default=10,
                        help='max_entities_per_chunk passed into both operations.')
    parser.add_argument('--placeholder', action='store_true',
                        help='Use an unavailable placeholder chunk and verify LLM is skipped.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    llm_calls = 0

    def complete(prompt: str) -> str:
        nonlocal llm_calls
        llm_calls += 1
        return args.entities_json

    chunk = {
        'available': not args.placeholder,
        'chunk_id': 'chunk-real-1' if not args.placeholder else 'unavailable:case_0001',
        'doc_id': 'doc-real-1' if not args.placeholder else '__unavailable__',
        'group': 'block',
        'text': args.text,
    }
    extract_inputs = {
        'chunk': chunk,
        'chunk_entities_extract_params': {'max_entities_per_chunk': args.max_entities},
    }

    print('== Extract Inputs ==')
    print(json.dumps(extract_inputs, ensure_ascii=False, indent=2))

    extracted = chunk_entities_extract(None, extract_inputs, llm_complete=complete)

    print('\n== Extract Output ==')
    print(json.dumps(extracted, ensure_ascii=False, indent=2))

    manifest_inputs = {
        'built_chunks': {
            'chunks': [{
                'available': chunk['available'],
                'chunk_id': chunk['chunk_id'],
                'doc_id': chunk['doc_id'],
                'group': chunk['group'],
                'partition': 'case_0001',
            }],
        },
        'chunk_entities': (extracted['chunk_entity'],),
        'chunk_entities_extract_manifest_params': {'max_entities_per_chunk': args.max_entities},
    }
    manifest = chunk_entities_extract_manifest(None, manifest_inputs)

    print('\n== Manifest Output ==')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

    print('\n== Summary ==')
    print(json.dumps({
        'llm_calls': llm_calls,
        'available': extracted['chunk_entity']['available'],
        'entities': extracted['chunk_entity']['entities'],
        'stats': manifest['chunk_entities_manifest']['stats'],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
