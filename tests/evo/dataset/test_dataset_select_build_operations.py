from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef
from evo.artifacts.schema import validate_artifact_payload
from evo.operations.dataset import (
    BuildChunksParams,
    build_chunk_candidates,
    build_chunks,
    build_chunks_manifest,
    select_docs,
)


DEFAULT_ALLOWED_TYPES = ['text', 'paragraph', 'table', 'formula', 'equation', 'unknown']
DEFAULT_BUILD_PARAMS = {
    'groups': ['block'],
    'allowed_types': DEFAULT_ALLOWED_TYPES,
    'excluded_chunks': [],
    'max_scan_docs_per_kb': 10_000,
    'max_scan_chunks': 100_000,
}


class FakeDiscoveryClient:
    def __init__(self, documents_by_kb=None):
        self.documents_by_kb = documents_by_kb or {}
        self.list_calls = []

    def list_documents(self, kb_id):
        self.list_calls.append(kb_id)
        return list(self.documents_by_kb.get(kb_id, []))


class FakeCandidateClient:
    """Storage-level fake: count first, then fetch only quota-selected payloads."""

    def __init__(self, *, counts_by_kb=None, chunks=None, groups_by_kb=None):
        self.counts_by_kb = counts_by_kb or {}
        self.chunks = chunks or {}
        self.groups_by_kb = groups_by_kb or {}
        self.count_calls = []
        self.fetch_calls = []

    def list_groups(self, kb_id):
        return list(self.groups_by_kb.get(kb_id, []))

    def count_valid_chunks(
        self, kb_id, doc_ids, groups, allowed_types, max_scan_chunks, *, excluded_chunk_ids=None,
    ):
        self.count_calls.append({
            'kb_id': kb_id,
            'doc_ids': list(doc_ids),
            'groups': list(groups),
            'allowed_types': list(allowed_types),
            'max_scan_chunks': max_scan_chunks,
            'excluded_chunk_ids': set(excluded_chunk_ids or ()),
        })
        return self.counts_by_kb[kb_id]

    def fetch_valid_chunks(
        self, kb_id, doc_id, group, allowed_types, limit, *, order_by, excluded_chunk_ids=None,
    ):
        self.fetch_calls.append({
            'kb_id': kb_id,
            'doc_id': doc_id,
            'group': group,
            'allowed_types': list(allowed_types),
            'limit': limit,
            'order_by': order_by,
            'excluded_chunk_ids': set(excluded_chunk_ids or ()),
        })
        excluded = set(excluded_chunk_ids or ())
        values = [item for item in self.chunks.get((kb_id, doc_id, group), []) if item.uid not in excluded]
        values.sort(key=lambda item: hashlib.sha256(item.uid.encode()).hexdigest())
        return values[:limit]


def node(uid, *, doc_id='doc-1', text='chunk text', group='block', number=1, chunk_type='text'):
    return SimpleNamespace(
        uid=uid,
        text=text,
        embedding={'default': [1.0]},
        group=group,
        number=number,
        metadata={'type': chunk_type, 'page': 1},
        global_metadata={'filename': f'{doc_id}.pdf'},
    )


def import_manifest(*, target=2, imported=0):
    return {'stats': {'case_allocation': {
        'target_case_count': target,
        'import_case_count': imported,
        'auto_case_count': target - imported,
        'assignments': {},
    }}}


def selected_docs(docs_by_kb):
    docs = []
    for kb_id, kb_docs in docs_by_kb.items():
        docs.extend({'kb_id': kb_id, **doc} for doc in kb_docs)
    return {
        'kb_ids': list(docs_by_kb),
        'docs': docs,
        'excluded_docs': [],
        'stats': {
            'discovered_count': len(docs),
            'selected_count': len(docs),
            'excluded_count': 0,
        },
        'params': {'kb_ids': list(docs_by_kb), 'excluded_docs': []},
    }


def count_result(
    capacities, *, scanned_count=None, filtered=None, invalid=None, manual=None, observed_types=None,
):
    effective_count = sum(sum(docs.values()) for docs in capacities.values())
    return {
        'scanned_count': effective_count if scanned_count is None else scanned_count,
        'effective_count': effective_count,
        'capacities': capacities,
        'filtered_count_by_type': filtered or {},
        'invalid_count_by_reason': invalid or {},
        'manual_exclusions': manual or [],
        'observed_types': observed_types or ['text'],
    }


def candidate_payload(selected, client, *, target=2, params=None):
    return build_chunk_candidates(None, {
        'selected_docs': selected,
        'build_chunks_params': params or {'groups': ['block']},
        'import_cases_manifest': import_manifest(target=target),
    }, client)['build_chunk_candidates']


def chunk_ctx(partition):
    return SimpleNamespace(output_key_by_name={'chunk': ArtifactKey('dataset.chunk', partition)})


def manifest_ctx(partitions):
    refs = {ArtifactKey.of('dataset.selected_docs'): ArtifactRef(ArtifactKey.of('dataset.selected_docs'), 1)}
    refs.update({
        ArtifactKey('dataset.chunk', partition): ArtifactRef(ArtifactKey('dataset.chunk', partition), index)
        for index, partition in enumerate(partitions, start=1)
    })
    return SimpleNamespace(input_ref_by_key=refs)


def test_select_docs_enumerates_all_docs_in_stable_kb_and_document_order():
    client = FakeDiscoveryClient({
        'kb-a': [
            {'doc_id': 'a-1', 'filename': 'a1.pdf', 'upload_status': 'success'},
            {'doc_id': 'a-2', 'filename': 'a2.pdf', 'upload_status': 'success'},
        ],
        'kb-b': [
            {'doc_id': 'b-1', 'filename': 'b1.pdf', 'upload_status': 'success'},
            {'doc_id': 'b-2', 'filename': 'b2.pdf', 'upload_status': 'success'},
            {'doc_id': 'b-3', 'filename': 'b3.pdf', 'upload_status': 'success'},
        ],
    })

    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a', 'kb-b']},
        'import_cases_manifest': import_manifest(target=3),
    }, client)['selected_docs']

    assert [item['doc_id'] for item in output['docs']] == ['a-1', 'a-2', 'b-1', 'b-2', 'b-3']
    assert output['stats'] == {
        'discovered_count': 5,
        'selected_count': 5,
        'excluded_count': 0,
    }
    assert output['excluded_docs'] == []
    assert output['params'] == {'kb_ids': ['kb-a', 'kb-b'], 'excluded_docs': []}
    assert client.list_calls == ['kb-a', 'kb-b']


def test_select_docs_preserves_source_provenance_without_group_counts_or_id_rewriting():
    client = FakeDiscoveryClient({'kb-1': [{
        'doc_id': 'doc-1',
        'display_name': 'fallback-name.pdf',
        'file_type': 'pdf',
        'upload_status': 'ready',
        'group_counts': {'block': 999},
    }]})

    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-1']},
        'import_cases_manifest': import_manifest(),
    }, client)['selected_docs']

    assert output['docs'] == [{
        'kb_id': 'kb-1',
        'doc_id': 'doc-1',
        'filename': 'fallback-name.pdf',
        'file_type': 'pdf',
        'status': 'ready',
    }]
    validate_artifact_payload('SelectedDocs', output)


def test_select_docs_skips_kb_access_when_every_case_is_imported():
    class NoReadClient:
        def list_documents(self, kb_id):
            raise AssertionError('all-imported runs must not enumerate knowledge-base documents')

    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a', 'kb-b']},
        'import_cases_manifest': import_manifest(target=2, imported=2),
    }, NoReadClient())['selected_docs']

    assert output == {
        'kb_ids': ['kb-a', 'kb-b'],
        'docs': [],
        'excluded_docs': [],
        'stats': {'discovered_count': 0, 'selected_count': 0, 'excluded_count': 0},
        'params': {'kb_ids': ['kb-a', 'kb-b'], 'excluded_docs': []},
    }


def test_select_docs_rejects_empty_discovery_when_auto_generation_is_required():
    with pytest.raises(ValueError, match='discovered no documents'):
        select_docs(None, {
            'source_config': {'kb_ids': ['kb-1']},
            'import_cases_manifest': import_manifest(),
        }, FakeDiscoveryClient({'kb-1': []}))


@pytest.mark.parametrize('inputs, match', [
    ({'source_config': None, 'import_cases_manifest': import_manifest()}, 'source_config must be a mapping'),
    ({'source_config': {'kb_ids': []}, 'import_cases_manifest': import_manifest()}, 'kb_ids'),
    ({'source_config': {'kb_ids': [1]}, 'import_cases_manifest': import_manifest()}, 'kb_ids'),
    ({'source_config': {'kb_ids': ['kb-1', 'kb-1']}, 'import_cases_manifest': import_manifest()},
     'kb_ids must be unique'),
    ({'source_config': {'kb_ids': ['kb-1'], 'max_docs': 10}, 'import_cases_manifest': import_manifest()},
     'max_docs is not supported'),
    ({'source_config': {'kb_ids': ['kb-1']}, 'import_cases_manifest': None},
     'import_cases_manifest must be a mapping'),
    ({'source_config': {'kb_ids': ['kb-1']}, 'import_cases_manifest': {}},
     'import_cases_manifest.stats must be a mapping'),
])
def test_select_docs_rejects_invalid_contract(inputs, match):
    with pytest.raises(ValueError, match=match):
        select_docs(None, inputs, FakeDiscoveryClient({'kb-1': [{'doc_id': 'doc-1'}]}))


def test_select_docs_applies_exclusions_and_keeps_unmatched_refs_dormant():
    client = FakeDiscoveryClient({'kb-a': [
        {'doc_id': 'doc-1', 'filename': 'one.pdf', 'upload_status': 'ready'},
        {'doc_id': 'doc-2', 'filename': 'two.pdf', 'upload_status': 'ready'},
        {'doc_id': 'doc-3', 'filename': 'three.pdf', 'upload_status': 'ready'},
    ]})
    excluded = [
        {'kb_id': 'kb-a', 'doc_id': 'doc-2'},
        {'kb_id': 'kb-a', 'doc_id': 'not-in-current-snapshot'},
    ]

    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a']},
        'select_docs_params': {'excluded_docs': excluded},
        'import_cases_manifest': import_manifest(target=2),
    }, client)['selected_docs']

    assert [item['doc_id'] for item in output['docs']] == ['doc-1', 'doc-3']
    assert [item['doc_id'] for item in output['excluded_docs']] == ['doc-2']
    assert output['stats'] == {'discovered_count': 3, 'selected_count': 2, 'excluded_count': 1}
    assert output['params'] == {'kb_ids': ['kb-a'], 'excluded_docs': excluded}


def test_select_docs_all_documents_excluded_returns_a_valid_empty_selection():
    client = FakeDiscoveryClient({'kb-a': [
        {'doc_id': 'doc-1', 'filename': 'one.pdf'},
        {'doc_id': 'doc-2', 'filename': 'two.pdf'},
    ]})

    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a']},
        'select_docs_params': {'excluded_docs': [
            {'kb_id': 'kb-a', 'doc_id': 'doc-1'},
            {'kb_id': 'kb-a', 'doc_id': 'doc-2'},
        ]},
        'import_cases_manifest': import_manifest(target=2),
    }, client)['selected_docs']

    assert output['docs'] == []
    assert len(output['excluded_docs']) == 2
    assert output['stats'] == {'discovered_count': 2, 'selected_count': 0, 'excluded_count': 2}
    validate_artifact_payload('SelectedDocs', output)


@pytest.mark.parametrize('excluded_docs', [
    [{'kb_id': '', 'doc_id': 'doc-1'}],
    [{'kb_id': 'kb-a', 'doc_id': ' '}],
    [
        {'kb_id': 'kb-a', 'doc_id': 'doc-1'},
        {'kb_id': 'kb-a', 'doc_id': 'doc-1'},
    ],
])
def test_select_docs_rejects_invalid_or_duplicate_exclusion_refs(excluded_docs):
    with pytest.raises(ValueError, match='excluded_docs'):
        select_docs(None, {
            'source_config': {'kb_ids': ['kb-a']},
            'select_docs_params': {'excluded_docs': excluded_docs},
            'import_cases_manifest': import_manifest(target=2),
        }, FakeDiscoveryClient({'kb-a': [{'doc_id': 'doc-1'}]}))


def test_build_chunks_params_include_resolved_scan_safety_limits():
    assert BuildChunksParams.from_dict({'groups': ['block']}).to_dict() == DEFAULT_BUILD_PARAMS
    assert BuildChunksParams.from_dict({
        'groups': [' block ', 'line'],
        'allowed_types': [' TABLE ', 'unknown'],
        'max_scan_docs_per_kb': 25,
        'max_scan_chunks': 500,
    }).to_dict() == {
        'groups': ['block', 'line'],
        'allowed_types': ['table', 'unknown'],
        'excluded_chunks': [],
        'max_scan_docs_per_kb': 25,
        'max_scan_chunks': 500,
    }


@pytest.mark.parametrize('field, value', [
    ('max_scan_docs_per_kb', 0),
    ('max_scan_docs_per_kb', True),
    ('max_scan_chunks', 0),
    ('max_scan_chunks', True),
])
def test_build_chunks_params_reject_invalid_scan_safety_limits(field, value):
    with pytest.raises(ValueError, match=field):
        BuildChunksParams.from_dict({'groups': ['block'], field: value})


@pytest.mark.parametrize('params, match', [
    ({'groups': 'block'}, 'groups'),
    ({'groups': ['block', 'block']}, 'groups'),
    ({'groups': ['block'], 'allowed_types': 'text'}, 'allowed_types'),
    ({'groups': ['block'], 'allowed_types': None}, 'allowed_types'),
    ({'groups': ['block'], 'allowed_types': ['text', ' TEXT ']}, 'allowed_types'),
])
def test_build_chunks_params_reject_implicit_list_compatibility(params, match):
    with pytest.raises(ValueError, match=match):
        BuildChunksParams.from_dict(params)


def test_build_chunks_params_resolve_and_validate_excluded_chunk_refs():
    resolved = BuildChunksParams.from_dict({
        'groups': ['block'],
        'excluded_chunks': [{'kb_id': ' kb-a ', 'doc_id': ' doc-1 ', 'chunk_id': ' chunk-1 '}],
    }).to_dict()

    assert resolved['excluded_chunks'] == [
        {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1'},
    ]
    with pytest.raises(ValueError, match='excluded_chunks'):
        BuildChunksParams.from_dict({
            'groups': ['block'],
            'excluded_chunks': [
                {'kb_id': 'kb-a', 'doc_id': 'doc-1', 'chunk_id': 'chunk-1'},
                {'kb_id': 'kb-a', 'doc_id': 'doc-2', 'chunk_id': 'chunk-1'},
            ],
        })


def test_build_chunk_candidates_uses_complete_effective_capacity_before_allocation():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1'}, {'doc_id': 'a-2'}]})
    client = FakeCandidateClient(
        counts_by_kb={'kb-a': count_result(
            {'block': {'a-1': 2, 'a-2': 1}},
            scanned_count=7,
            filtered={'heading': 2},
            invalid={'empty_text': 1, 'missing_embedding': 1},
        )},
        chunks={
            ('kb-a', 'a-1', 'block'): [node('a1-1', doc_id='a-1'), node('a1-2', doc_id='a-1', number=2)],
            ('kb-a', 'a-2', 'block'): [node('a2-1', doc_id='a-2')],
        },
    )

    output = candidate_payload(selected, client, target=2)

    assert output['summary'] == {
        'candidate_limit': 3,
        'scanned_doc_count': 2,
        'scanned_chunk_count': 7,
        'effective_count': 3,
        'selected_count': 3,
        'unselected_effective_count': 0,
        'shortfall_count': 0,
    }
    assert output['invalid_counts'] == {
        'filtered_by_type': 2,
        'empty_text': 1,
        'missing_embedding': 1,
        'invalid_embedding': 0,
    }
    assert [call['limit'] for call in client.fetch_calls] == [2, 1]


def test_build_chunk_candidates_allocates_group_then_kb_then_doc_with_largest_remainder():
    selected = selected_docs({
        'kb-a': [{'doc_id': 'a-1'}, {'doc_id': 'a-2'}],
        'kb-b': [{'doc_id': 'b-1'}],
    })
    client = FakeCandidateClient(
        counts_by_kb={
            'kb-a': count_result({'block': {'a-1': 5, 'a-2': 3}}),
            'kb-b': count_result({'block': {'b-1': 4}}),
        },
        chunks={
            ('kb-a', 'a-1', 'block'): [node(f'a1-{i}', doc_id='a-1', number=i) for i in range(1, 6)],
            ('kb-a', 'a-2', 'block'): [node(f'a2-{i}', doc_id='a-2', number=i) for i in range(1, 4)],
            ('kb-b', 'b-1', 'block'): [node(f'b1-{i}', doc_id='b-1', number=i) for i in range(1, 5)],
        },
    )

    output = candidate_payload(selected, client, target=7)  # candidate_limit = ceil(7 * 1.5) = 11
    assert output['groups'] == [{'group': 'block', 'effective_count': 12, 'selected_count': 11}]
    assert [(doc['kb_id'], doc['doc_id'], doc['selected_count']) for doc in output['documents']] == [
        ('kb-a', 'a-1', 4),
        ('kb-a', 'a-2', 3),
        ('kb-b', 'b-1', 4),
    ]


def test_build_chunk_candidates_uses_global_group_priority_before_fallback():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1'}], 'kb-b': [{'doc_id': 'b-1'}]})
    client = FakeCandidateClient(
        counts_by_kb={
            'kb-a': count_result({'block': {'a-1': 2}, 'line': {'a-1': 10}}),
            'kb-b': count_result({'block': {'b-1': 1}, 'line': {'b-1': 10}}),
        },
        chunks={
            ('kb-a', 'a-1', 'block'): [node('a-block-1'), node('a-block-2', number=2)],
            ('kb-b', 'b-1', 'block'): [node('b-block-1', doc_id='b-1')],
            ('kb-a', 'a-1', 'line'): [node(f'a-line-{i}', group='line', number=i) for i in range(1, 11)],
            ('kb-b', 'b-1', 'line'): [node(f'b-line-{i}', doc_id='b-1', group='line', number=i) for i in range(1, 11)],
        },
    )

    output = candidate_payload(selected, client, target=4, params={'groups': ['block', 'line']})
    assert output['groups'] == [
        {'group': 'block', 'effective_count': 3, 'selected_count': 3},
        {'group': 'line', 'effective_count': 20, 'selected_count': 3},
    ]
    assert [chunk['group'] for chunk in output['chunks']] == ['block', 'block', 'block', 'line', 'line', 'line']


def test_build_chunk_candidates_uses_stable_hash_selection_and_stable_output_order():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1'}]})
    candidates = [node(f'chunk-{i}', number=i) for i in range(1, 11)]
    client = FakeCandidateClient(
        counts_by_kb={'kb-a': count_result({'block': {'a-1': 10}})},
        chunks={('kb-a', 'a-1', 'block'): candidates},
    )

    first = candidate_payload(selected, client, target=2)
    second = candidate_payload(selected, client, target=2)
    expected = sorted(candidates, key=lambda item: hashlib.sha256(item.uid.encode()).hexdigest())[:3]
    expected_ids = [item.uid for item in sorted(expected, key=lambda item: item.number)]

    assert [item['chunk_id'] for item in first['chunks']] == expected_ids
    assert [item['chunk_id'] for item in second['chunks']] == expected_ids
    assert client.fetch_calls[0]['order_by'] == 'stable_chunk_id_hash'


def test_build_chunk_candidates_returns_shortfall_without_failing():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1'}]})
    client = FakeCandidateClient(
        counts_by_kb={'kb-a': count_result({'block': {'a-1': 2}})},
        chunks={('kb-a', 'a-1', 'block'): [node('one'), node('two', number=2)]},
    )

    output = candidate_payload(selected, client, target=4)

    assert len(output['chunks']) == 2
    assert output['summary']['candidate_limit'] == 6
    assert output['summary']['selected_count'] == 2
    assert output['summary']['shortfall_count'] == 4


@pytest.mark.parametrize('params, counts, match', [
    ({'groups': ['block'], 'max_scan_docs_per_kb': 1}, {'kb-a': count_result({'block': {'a-1': 1, 'a-2': 1}})},
     'max_scan_docs_per_kb'),
    ({'groups': ['block'], 'max_scan_chunks': 3}, {'kb-a': count_result({'block': {'a-1': 2}}, scanned_count=4)},
     'max_scan_chunks'),
])
def test_build_chunk_candidates_scan_limits_fail_without_silent_truncation(params, counts, match):
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1'}, {'doc_id': 'a-2'}]})

    with pytest.raises(ValueError, match=match):
        candidate_payload(selected, FakeCandidateClient(counts_by_kb=counts), params=params)


def test_all_imported_cases_skip_candidate_scan_and_return_structured_zero_stats():
    class NoReadClient:
        def count_valid_chunks(self, *args, **kwargs):
            raise AssertionError('all-imported runs must not inspect knowledge-base chunks')

    output = build_chunk_candidates(None, {
        'selected_docs': selected_docs({'kb-a': [{'doc_id': 'a-1'}]}),
        'build_chunks_params': {'groups': ['block']},
        'import_cases_manifest': import_manifest(target=2, imported=2),
    }, NoReadClient())['build_chunk_candidates']

    assert output == {
        'chunks': [],
        'summary': {
            'candidate_limit': 0,
            'scanned_doc_count': 0,
            'scanned_chunk_count': 0,
            'effective_count': 0,
            'selected_count': 0,
            'unselected_effective_count': 0,
            'shortfall_count': 0,
        },
        'invalid_counts': {
            'filtered_by_type': 0,
            'empty_text': 0,
            'missing_embedding': 0,
            'invalid_embedding': 0,
        },
        'manual_exclusions': {'chunk_count': 0, 'chunks': []},
        'filter_options': {'available_groups': ['block'], 'available_types': DEFAULT_ALLOWED_TYPES},
        'groups': [],
        'documents': [],
        'params': DEFAULT_BUILD_PARAMS,
    }


def test_build_chunk_candidates_output_contract_contains_complete_payload_and_resolved_params():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1', 'filename': 'a.pdf'}]})
    client = FakeCandidateClient(
        counts_by_kb={'kb-a': count_result({'block': {'a-1': 1}})},
        chunks={('kb-a', 'a-1', 'block'): [node('chunk-1')]},
    )

    output = candidate_payload(selected, client, target=1)

    assert set(output) == {
        'chunks', 'summary', 'invalid_counts', 'manual_exclusions',
        'filter_options', 'groups', 'documents', 'params',
    }
    assert set(output['chunks'][0]) == {
        'kb_id', 'chunk_id', 'doc_id', 'filename', 'group', 'type',
        'text', 'embedding', 'metadata',
    }
    assert output['params'] == DEFAULT_BUILD_PARAMS


def test_build_chunk_candidates_rejects_duplicate_global_chunk_id():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1'}, {'doc_id': 'a-2'}]})
    client = FakeCandidateClient(
        counts_by_kb={'kb-a': count_result({'block': {'a-1': 1, 'a-2': 1}})},
        chunks={
            ('kb-a', 'a-1', 'block'): [node('same-id', doc_id='a-1')],
            ('kb-a', 'a-2', 'block'): [node('same-id', doc_id='a-2')],
        },
    )

    with pytest.raises(ValueError, match='duplicate chunk_id.*same-id'):
        candidate_payload(selected, client, target=2)


def test_build_chunk_candidates_excludes_manual_refs_before_selection_and_invalid_counts():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1', 'filename': 'a.pdf'}]})
    client = FakeCandidateClient(
        counts_by_kb={'kb-a': count_result(
            {'block': {'a-1': 1}},
            scanned_count=3,
            invalid={'empty_text': 1},
            manual=[{
                'kb_id': 'kb-a',
                'doc_id': 'a-1',
                'chunk_id': 'exclude-me',
                'group': 'block',
                'type': 'text',
            }],
        )},
        chunks={('kb-a', 'a-1', 'block'): [
            node('keep-me', doc_id='a-1'),
            node('exclude-me', doc_id='a-1', number=2),
        ]},
    )

    output = candidate_payload(selected, client, target=1, params={
        'groups': ['block'],
        'excluded_chunks': [{'kb_id': 'kb-a', 'doc_id': 'a-1', 'chunk_id': 'exclude-me'}],
    })

    assert [item['chunk_id'] for item in output['chunks']] == ['keep-me']
    assert output['manual_exclusions']['chunk_count'] == 1
    assert [item['chunk_id'] for item in output['manual_exclusions']['chunks']] == ['exclude-me']
    assert output['invalid_counts'] == {
        'filtered_by_type': 0,
        'empty_text': 1,
        'missing_embedding': 0,
        'invalid_embedding': 0,
    }
    assert output['summary']['scanned_chunk_count'] == (
        output['manual_exclusions']['chunk_count']
        + sum(output['invalid_counts'].values())
        + output['summary']['effective_count']
    )


def test_build_chunk_candidates_zero_effective_capacity_fails_when_auto_generation_is_required():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1'}]})
    client = FakeCandidateClient(counts_by_kb={
        'kb-a': count_result(
            {'block': {'a-1': 0}},
            scanned_count=2,
            invalid={'empty_text': 1, 'missing_embedding': 1},
        ),
    })

    with pytest.raises(ValueError, match='effective|valid chunk|capacity'):
        candidate_payload(selected, client, target=1)


def test_build_chunk_candidates_uses_flat_review_contract_and_reconcilable_summaries():
    selected = selected_docs({'kb-a': [{'doc_id': 'a-1', 'filename': 'a.pdf', 'file_type': 'pdf'}]})
    client = FakeCandidateClient(
        counts_by_kb={'kb-a': count_result({'block': {'a-1': 2}}, scanned_count=4, filtered={'heading': 1},
                                           invalid={'missing_embedding': 1}, observed_types=['text', 'heading'])},
        chunks={('kb-a', 'a-1', 'block'): [
            node('chunk-1', doc_id='a-1'),
            node('chunk-2', doc_id='a-1', number=2),
        ]},
        groups_by_kb={'kb-a': ['block', 'line']},
    )

    output = candidate_payload(selected, client, target=1)

    assert set(output) == {
        'chunks', 'summary', 'invalid_counts', 'manual_exclusions',
        'filter_options', 'groups', 'documents', 'params',
    }
    assert output['summary'] == {
        'candidate_limit': 2,
        'scanned_doc_count': 1,
        'scanned_chunk_count': 4,
        'effective_count': 2,
        'selected_count': 2,
        'unselected_effective_count': 0,
        'shortfall_count': 0,
    }
    assert sum(item['effective_count'] for item in output['groups']) == output['summary']['effective_count']
    assert sum(item['selected_count'] for item in output['documents']) == output['summary']['selected_count']
    assert output['filter_options']['available_groups'] == ['block', 'line']
    assert output['filter_options']['available_types'][:2] == ['text', 'heading']
    assert 'available' not in output['chunks'][0]


def test_build_chunks_maps_candidates_and_uses_placeholders_for_shortfall():
    candidates = {
        'chunks': [{
            'kb_id': 'kb-a',
            'chunk_id': 'chunk-1',
            'doc_id': 'a-1',
            'filename': 'a.pdf',
            'group': 'block',
            'type': 'text',
            'text': 'one',
            'embedding': {'model': 'default', 'vector': [1.0]},
            'metadata': {},
        }],
        'params': DEFAULT_BUILD_PARAMS,
    }

    assert build_chunks(chunk_ctx('chunk_0001'), {'build_chunk_candidates': candidates})['chunk']['available'] is True
    placeholder = build_chunks(chunk_ctx('chunk_0002'), {'build_chunk_candidates': candidates})['chunk']
    assert placeholder['available'] is False
    assert placeholder['chunk_id'] == 'unavailable:chunk_0002'


def test_build_chunks_adds_available_flag_to_real_candidates_without_mutating_source_fields():
    candidate = {
        'kb_id': 'kb-a',
        'chunk_id': 'chunk-1',
        'doc_id': 'a-1',
        'filename': 'a.pdf',
        'group': 'block',
        'type': 'text',
        'text': 'one',
        'embedding': {'model': 'default', 'vector': [1.0]},
        'metadata': {'page': 1},
    }
    candidates = {
        'chunks': [candidate],
        'summary': {},
        'invalid_counts': {},
        'manual_exclusions': {'chunk_count': 0, 'chunks': []},
        'filter_options': {'available_groups': ['block'], 'available_types': ['text']},
        'groups': [],
        'documents': [],
        'params': {**DEFAULT_BUILD_PARAMS, 'excluded_chunks': []},
    }

    output = build_chunks(chunk_ctx('chunk_0001'), {'build_chunk_candidates': candidates})['chunk']

    assert output == {'available': True, **candidate}


def _step1_manifest_contract_inputs():
    selected = {
        'kb_ids': ['kb-a'],
        'docs': [{'kb_id': 'kb-a', 'doc_id': 'a-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'ready'}],
        'excluded_docs': [{'kb_id': 'kb-a', 'doc_id': 'a-2', 'filename': 'hidden.pdf',
                           'file_type': 'pdf', 'status': 'ready'}],
        'stats': {'discovered_count': 2, 'selected_count': 1, 'excluded_count': 1},
        'params': {'kb_ids': ['kb-a'], 'excluded_docs': [{'kb_id': 'kb-a', 'doc_id': 'a-2'}]},
    }
    candidate_chunks = [
        {'kb_id': 'kb-a', 'chunk_id': f'chunk-{index}', 'doc_id': 'a-1', 'filename': 'a.pdf',
         'group': 'block', 'type': 'text', 'text': f'text {index}', 'embedding': {'default': [1.0]},
         'metadata': {'page': index}}
        for index in (1, 2)
    ]
    candidates = {
        'chunks': candidate_chunks,
        'summary': {
            'candidate_limit': 2,
            'scanned_doc_count': 1,
            'scanned_chunk_count': 5,
            'effective_count': 2,
            'selected_count': 2,
            'unselected_effective_count': 0,
            'shortfall_count': 0,
        },
        'invalid_counts': {
            'filtered_by_type': 1,
            'empty_text': 1,
            'missing_embedding': 0,
            'invalid_embedding': 0,
        },
        'manual_exclusions': {
            'chunk_count': 1,
            'chunks': [{'kb_id': 'kb-a', 'doc_id': 'a-1', 'chunk_id': 'hidden-chunk',
                        'filename': 'a.pdf', 'group': 'block', 'type': 'text'}],
        },
        'filter_options': {'available_groups': ['block', 'line'], 'available_types': ['text', 'table']},
        'groups': [{'group': 'block', 'effective_count': 2, 'selected_count': 2}],
        'documents': [{
            'kb_id': 'kb-a', 'doc_id': 'a-1', 'filename': 'a.pdf', 'file_type': 'pdf',
            'effective_count': 2, 'selected_count': 2,
            'groups': [{'group': 'block', 'effective_count': 2, 'selected_count': 2}],
        }],
        'params': {**DEFAULT_BUILD_PARAMS, 'excluded_chunks': [
            {'kb_id': 'kb-a', 'doc_id': 'a-1', 'chunk_id': 'hidden-chunk'},
        ]},
    }
    chunks = tuple({'available': True, **item} for item in candidate_chunks)
    return {
        'selected_docs': selected,
        'import_cases_manifest': {
            'source': {'csv_path': '/private/data/cases.csv'},
            'stats': {'case_allocation': {
                'target_case_count': 2,
                'import_case_count': 1,
                'auto_case_count': 1,
                'assignments': {},
            }},
        },
        'build_chunk_candidates': candidates,
        'chunk': chunks,
    }


def test_build_chunks_manifest_is_single_lightweight_reconcilable_homepage_contract():
    inputs = _step1_manifest_contract_inputs()

    output = build_chunks_manifest(manifest_ctx(('chunk_0001', 'chunk_0002')), inputs)['build_chunks_manifest']

    assert set(output) == {
        'source', 'summary', 'filter_options', 'groups', 'documents', 'chunks', 'params', 'warnings',
    }
    assert output['source'] == {
        'kb_ids': ['kb-a'],
        'csv_present': True,
        'case_counts': {'target': 2, 'imported': 1, 'automatic': 1},
    }
    assert 'csv_path' not in output['source']
    assert output['summary']['document_counts'] == {'discovered': 2, 'scanned': 1, 'excluded': 1}
    assert output['summary']['chunk_counts'] == {
        'scanned': 5,
        'effective': 2,
        'selected': 2,
        'unselected_effective': 0,
        'candidate_target': 2,
        'shortfall': 0,
    }
    assert output['summary']['manual_exclusions'] == {'document_count': 1, 'chunk_count': 1}
    assert output['summary']['slots'] == {'total': 2, 'available': 2, 'placeholder': 0}
    assert all('text' not in item and 'embedding' not in item and 'metadata' not in item for item in output['chunks'])
    assert output['params'] == {'groups': ['block'], 'allowed_types': DEFAULT_ALLOWED_TYPES}
    assert output['warnings'] == []


def test_build_chunks_manifest_rejects_available_slot_and_selected_count_mismatch():
    inputs = _step1_manifest_contract_inputs()
    inputs['chunk'] = (
        inputs['chunk'][0],
        {
            'available': False,
            'chunk_id': 'unavailable:chunk_0002',
            'doc_id': '__unavailable__',
            'filename': '',
            'group': 'block',
            'type': 'placeholder',
        },
    )

    with pytest.raises(ValueError, match='available|selected'):
        build_chunks_manifest(manifest_ctx(('chunk_0001', 'chunk_0002')), inputs)


def test_build_chunks_manifest_warns_only_for_positive_capacity_shortfall():
    inputs = _step1_manifest_contract_inputs()
    inputs['build_chunk_candidates']['summary'].update({
        'selected_count': 1,
        'unselected_effective_count': 1,
        'shortfall_count': 1,
    })
    inputs['chunk'] = (
        inputs['chunk'][0],
        {
            'available': False,
            'chunk_id': 'unavailable:chunk_0002',
            'doc_id': '__unavailable__',
            'filename': '',
            'group': 'block',
            'type': 'placeholder',
        },
    )

    output = build_chunks_manifest(
        manifest_ctx(('chunk_0001', 'chunk_0002')), inputs,
    )['build_chunks_manifest']

    assert output['warnings'] == ['chunk candidate capacity is short by 1; selected 1 of 2']


def test_build_chunks_manifest_all_imported_run_keeps_placeholder_index_without_warning():
    placeholder = {
        'available': False,
        'chunk_id': 'unavailable:chunk_0001',
        'doc_id': '__unavailable__',
        'filename': '',
        'group': 'block',
        'type': 'placeholder',
    }
    inputs = {
        'selected_docs': {
            'kb_ids': ['kb-a'],
            'docs': [],
            'excluded_docs': [],
            'stats': {'discovered_count': 0, 'selected_count': 0, 'excluded_count': 0},
            'params': {'kb_ids': ['kb-a'], 'excluded_docs': []},
        },
        'import_cases_manifest': {
            'source': {'csv_path': '/private/data/cases.csv'},
            'stats': {'case_allocation': {
                'target_case_count': 2,
                'import_case_count': 2,
                'auto_case_count': 0,
                'assignments': {},
            }},
        },
        'build_chunk_candidates': {
            'chunks': [],
            'summary': {
                'candidate_limit': 0,
                'scanned_doc_count': 0,
                'scanned_chunk_count': 0,
                'effective_count': 0,
                'selected_count': 0,
                'unselected_effective_count': 0,
                'shortfall_count': 0,
            },
            'invalid_counts': {
                'filtered_by_type': 0,
                'empty_text': 0,
                'missing_embedding': 0,
                'invalid_embedding': 0,
            },
            'manual_exclusions': {'chunk_count': 0, 'chunks': []},
            'filter_options': {'available_groups': ['block'], 'available_types': DEFAULT_ALLOWED_TYPES},
            'groups': [],
            'documents': [],
            'params': DEFAULT_BUILD_PARAMS,
        },
        'chunk': (placeholder, {**placeholder, 'chunk_id': 'unavailable:chunk_0002'}),
    }

    output = build_chunks_manifest(
        manifest_ctx(('chunk_0001', 'chunk_0002')), inputs,
    )['build_chunks_manifest']

    assert output['summary']['slots'] == {'total': 2, 'available': 0, 'placeholder': 2}
    assert output['source']['case_counts'] == {'target': 2, 'imported': 2, 'automatic': 0}
    assert output['warnings'] == []
