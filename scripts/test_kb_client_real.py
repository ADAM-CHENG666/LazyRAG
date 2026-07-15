#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
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

from evo.operations.dataset.kb_client import KnowledgeBaseClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run a real read test for evo KnowledgeBaseClient.')
    parser.add_argument('--kb-id', required=True, help='Knowledge base id to inspect.')
    parser.add_argument('--doc-id', default='', help='Optional document id to read chunks from.')
    parser.add_argument('--group', action='append', dest='groups',
                        help='Chunk group to read. Repeatable. Defaults to block.')
    parser.add_argument('--page-size', type=int, default=5, help='Chunk page size for each batch read.')
    parser.add_argument('--max-docs', type=int, default=5, help='How many docs to print from list_documents.')
    parser.add_argument('--max-batches', type=int, default=2, help='How many chunk batches to print.')
    parser.add_argument('--base-url', default=os.getenv('LAZYMIND_EVO_KB_BASE_URL', ''),
                        help='Doc server base url. Defaults to LAZYMIND_EVO_KB_BASE_URL.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.base_url.strip():
        raise ValueError('base url is required; pass --base-url or set LAZYMIND_EVO_KB_BASE_URL')

    groups = args.groups or ['block']
    client = KnowledgeBaseClient(base_url=args.base_url.strip())

    print('== Environment ==')
    print(json.dumps({
        'kb_id': args.kb_id,
        'doc_id': args.doc_id,
        'groups': groups,
        'page_size': args.page_size,
        'base_url': args.base_url.strip(),
        'algo_id': os.getenv('LAZYMIND_ALGO_ID', ''),
    }, ensure_ascii=False, indent=2))

    print('\n== list_documents ==')
    docs = client.list_documents(args.kb_id)
    print(f'document_count={len(docs)}')
    for index, doc in enumerate(docs[:args.max_docs], 1):
        print(json.dumps({
            'index': index,
            'doc_id': doc['doc_id'],
            'filename': doc['filename'],
            'file_type': doc['file_type'],
            'upload_status': doc['upload_status'],
            'status': doc['status'],
            'group_counts': doc['group_counts'],
        }, ensure_ascii=False))

    target_doc_ids = [args.doc_id] if args.doc_id else ([docs[0]['doc_id']] if docs else [])
    if not target_doc_ids:
        print('\n== iter_chunks ==')
        print('skip: no document available')
        return 0

    print('\n== iter_chunks ==')
    print(f'target_doc_ids={target_doc_ids}')
    batch_count = 0
    for batch in client.iter_chunks(args.kb_id, target_doc_ids, groups, args.page_size):
        batch_count += 1
        preview = []
        for node in batch[:2]:
            preview.append({
                'uid': str(getattr(node, 'uid', '') or getattr(node, '_uid', '')),
                'text_preview': str(getattr(node, 'text', '') or '')[:120],
                'metadata': getattr(node, 'metadata', {}) or {},
                'global_metadata': getattr(node, 'global_metadata', {}) or {},
            })
        print(json.dumps({
            'batch_index': batch_count,
            'batch_size': len(batch),
            'preview': preview,
        }, ensure_ascii=False))
        if batch_count >= args.max_batches:
            break

    if batch_count == 0:
        print('no chunks returned')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
