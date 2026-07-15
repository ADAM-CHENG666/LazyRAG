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

from evo.operations.dataset.select_docs import select_docs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run a real test for dataset.select_docs.')
    parser.add_argument('--kb-id', required=True, help='Knowledge base id to inspect.')
    parser.add_argument('--max-docs', type=int, default=3, help='How many docs select_docs should keep.')
    parser.add_argument('--target-case-count', type=int, default=100,
                        help='target_case_count passed into source_config.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = {
        'source_config': {
            'kb_id': args.kb_id,
            'max_docs': args.max_docs,
            'target_case_count': args.target_case_count,
        }
    }

    print('== Inputs ==')
    print(json.dumps(inputs, ensure_ascii=False, indent=2))

    result = select_docs(ctx=None, inputs=inputs)

    print('\n== Output ==')
    print(json.dumps(result, ensure_ascii=False, indent=2))

    payload = result['selected_docs']
    print('\n== Summary ==')
    print(json.dumps({
        'matched': payload['stats']['matched'],
        'selected': payload['stats']['selected'],
        'selected_doc_ids': [doc['doc_id'] for doc in payload['docs']],
        'selected_filenames': [doc['filename'] for doc in payload['docs']],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
