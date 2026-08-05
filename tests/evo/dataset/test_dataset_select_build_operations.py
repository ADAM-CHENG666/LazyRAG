from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from evo.artifact_runtime.kernel import ArtifactKey, ArtifactRef
from evo.operations.dataset import BuildChunksParams, build_chunk_candidates, build_chunks, build_chunks_manifest, select_docs
from evo.operations.dataset.chunks_build import validate_chunk_selection


class FakeDiscoveryClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list_documents(self, kb_id):
        self.calls.append(kb_id)
        return list(self.rows.get(kb_id, []))


class FakeCandidateClient:
    def __init__(self, counts, chunks):
        self.counts = counts
        self.chunks = chunks
        self.count_calls = []
        self.fetch_calls = []

    def count_valid_chunks(self, kb_id, doc_ids, groups, allowed_types, max_scan_chunks, **_):
        self.count_calls.append((kb_id, tuple(doc_ids), tuple(groups)))
        return self.counts[kb_id]

    def fetch_valid_chunks(self, kb_id, doc_id, group, allowed_types, limit, **_):
        self.fetch_calls.append((kb_id, doc_id, group, limit))
        values = list(self.chunks.get((kb_id, doc_id, group), []))
        values.sort(key=lambda item: hashlib.sha256(item.uid.encode()).hexdigest())
        return values[:limit]


def node(uid, *, doc_id='doc-1', group='block', number=1, kind='text'):
    return SimpleNamespace(
        uid=uid, text=f'text:{uid}', embedding={'default': [1.0]}, number=number,
        metadata={'type': kind}, global_metadata={'filename': f'{doc_id}.pdf'},
    )


def import_manifest(target=2, imported=0):
    return {'source': {'csv_path': ''}, 'stats': {'case_allocation': {
        'target_case_count': target, 'import_case_count': imported,
        'auto_case_count': target - imported, 'assignments': {},
    }}}


def counts(capacities, scanned=None):
    effective = sum(sum(value.values()) for value in capacities.values())
    return {
        'scanned_count': effective if scanned is None else scanned,
        'effective_count': effective,
        'capacities': capacities,
        'filtered_count_by_type': {},
        'invalid_count_by_reason': {},
        'observed_types': ['text'],
    }


def documents(rows, excluded=()):
    return select_docs(None, {
        'source_config': {'kb_ids': list(rows)},
        'select_docs_params': {'excluded_docs': [
            {'kb_id': kb_id, 'doc_id': doc_id} for kb_id, doc_id in excluded
        ]},
    }, FakeDiscoveryClient(rows))['selected_docs']


def candidates(selected, client, target=2, params=None):
    return build_chunk_candidates(None, {
        'selected_docs': selected,
        'import_cases_manifest': import_manifest(target),
        'build_chunks_params': params or {'groups': ['block']},
    }, client)['build_chunk_candidates']


def chunk_ctx(partition):
    return SimpleNamespace(output_key_by_name={'chunk': ArtifactKey('dataset.chunk', partition)})


def manifest_ctx(partitions):
    refs = {ArtifactKey('dataset.chunk', partition): ArtifactRef(ArtifactKey('dataset.chunk', partition), 1)
            for partition in partitions}
    return SimpleNamespace(input_ref_by_key=refs)


# select_docs.yaml: discovery_and_inclusion
def test_docs_keep_kb_then_source_order_and_monotonic_discovery_index():
    output = documents({
        'kb-a': [{'doc_id': 'a-1'}, {'doc_id': 'a-2'}],
        'kb-b': [{'doc_id': 'b-1'}],
    })

    assert [(item['kb_id'], item['doc_id']) for item in output['documents']] == [
        ('kb-a', 'a-1'), ('kb-a', 'a-2'), ('kb-b', 'b-1'),
    ]
    assert [item['discovery_index'] for item in output['documents']] == [0, 1, 2]


def test_docs_keep_excluded_document_in_unified_list():
    output = documents({'kb-a': [{'doc_id': 'one'}, {'doc_id': 'two'}]}, [('kb-a', 'two')])

    assert [(item['doc_id'], item['included']) for item in output['documents']] == [('one', True), ('two', False)]
    assert output['stats'] == {'discovered_count': 2, 'included_count': 1, 'excluded_count': 1}


def test_docs_keep_unmatched_exclusion_dormant():
    output = documents({'kb-a': [{'doc_id': 'one'}]}, [('kb-a', 'missing')])

    assert output['documents'][0]['included'] is True
    assert output['stats'] == {'discovered_count': 1, 'included_count': 1, 'excluded_count': 0}


def test_docs_use_kb_and_document_as_composite_identity():
    output = documents({'kb-a': [{'doc_id': 'same'}], 'kb-b': [{'doc_id': 'same'}]}, [('kb-a', 'same')])

    assert [item['included'] for item in output['documents']] == [False, True]


def test_docs_are_available_for_imported_only_configuration():
    client = FakeDiscoveryClient({'kb-a': [{'doc_id': 'one'}]})
    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a']},
        'select_docs_params': {'excluded_docs': []},
        'import_cases_manifest': import_manifest(1, 1),
    }, client)['selected_docs']

    assert output['documents'][0]['doc_id'] == 'one'
    assert client.calls == ['kb-a']


def test_docs_allow_empty_discovery():
    output = documents({'kb-a': []})
    assert output == {'documents': [], 'stats': {'discovered_count': 0, 'included_count': 0, 'excluded_count': 0}}


@pytest.mark.parametrize('excluded', [
    [{'kb_id': '', 'doc_id': 'one'}],
    [{'kb_id': 'kb-a', 'doc_id': ' '}],
    [{'kb_id': 'kb-a', 'doc_id': 'one'}, {'kb_id': 'kb-a', 'doc_id': 'one'}],
])
def test_docs_reject_invalid_selection_references(excluded):
    with pytest.raises(ValueError, match='excluded_docs'):
        select_docs(None, {'source_config': {'kb_ids': ['kb-a']}, 'select_docs_params': {'excluded_docs': excluded}},
                    FakeDiscoveryClient({'kb-a': []}))


# chunk_candidates.yaml: effective_chunk_snapshot
def test_candidates_scan_only_included_documents_and_keep_all_effective_chunks():
    selected = documents({'kb-a': [{'doc_id': 'one'}, {'doc_id': 'two'}]}, [('kb-a', 'two')])
    client = FakeCandidateClient(
        {'kb-a': counts({'block': {'one': 3}})},
        {('kb-a', 'one', 'block'): [node(f'one-{i}', doc_id='one', number=i) for i in range(3)]},
    )

    output = candidates(selected, client, target=1)

    assert client.count_calls == [('kb-a', ('one',), ('block',))]
    assert [item['chunk_id'] for item in output['chunks']] == ['one-0', 'one-1', 'one-2']
    assert [item['discovery_index'] for item in output['chunks']] == [0, 1, 2]


def test_candidates_keep_full_payload_and_filter_invalid_chunks_from_snapshot():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    client = FakeCandidateClient(
        {'kb-a': counts({'block': {'one': 2}}, scanned=5)},
        {('kb-a', 'one', 'block'): [node('one'), node('two', number=2)]},
    )

    output = candidates(selected, client, target=1, params={'groups': ['block'], 'allowed_types': ['text']})

    assert output['summary']['effective_count'] == 2
    assert output['summary']['scanned_chunk_count'] == 5
    assert set(output['chunks'][0]) >= {'text', 'embedding', 'metadata', 'kb_id', 'doc_id', 'chunk_id'}


def test_candidates_fail_on_scan_limit_without_partial_snapshot():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    client = FakeCandidateClient({'kb-a': counts({'block': {'one': 2}}, scanned=3)}, {})

    with pytest.raises(ValueError, match='max_scan_chunks'):
        candidates(selected, client, params={'groups': ['block'], 'max_scan_chunks': 2})


# chunk_candidates.yaml: initial_selection_and_quota
def test_candidates_select_deterministically_and_freeze_document_group_quota():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    raw = [node(f'chunk-{index}', number=index) for index in range(5)]
    client = FakeCandidateClient({'kb-a': counts({'block': {'one': 5}})}, {('kb-a', 'one', 'block'): raw})

    first, second = candidates(selected, client, target=2), candidates(selected, client, target=2)

    assert [item['chunk_id'] for item in first['chunks'] if item['selected']] == [
        item['chunk_id'] for item in second['chunks'] if item['selected']
    ]
    assert first['quotas'] == [{'kb_id': 'kb-a', 'doc_id': 'one', 'group': 'block', 'required': 3}]
    assert sum(item['selected'] for item in first['chunks']) == 3


def test_candidates_assign_selection_index_only_to_selected_chunks():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    client = FakeCandidateClient({'kb-a': counts({'block': {'one': 5}})}, {
        ('kb-a', 'one', 'block'): [node(str(index), number=index) for index in range(5)],
    })

    output = candidates(selected, client, target=2)
    assert [item['selection_index'] for item in output['chunks'] if item['selected']] == [0, 1, 2]
    assert all(item['selection_index'] is None for item in output['chunks'] if not item['selected'])


def test_candidates_return_all_effective_chunks_when_capacity_is_short():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    client = FakeCandidateClient({'kb-a': counts({'block': {'one': 2}})}, {
        ('kb-a', 'one', 'block'): [node('one'), node('two', number=2)],
    })

    output = candidates(selected, client, target=3)
    assert len(output['chunks']) == 2
    assert output['summary'] == {'scanned_chunk_count': 2, 'effective_count': 2, 'selected_count': 2, 'shortfall_count': 3}


def test_candidates_fail_with_no_effective_capacity_when_generation_is_required():
    with pytest.raises(ValueError, match='effective capacity'):
        candidates(documents({'kb-a': [{'doc_id': 'one'}]}),
                   FakeCandidateClient({'kb-a': counts({'block': {'one': 0}})}, {}), target=1)


# chunk_candidates.yaml: manual_selection_update_contract
def test_manual_selection_replacement_keeps_effective_snapshot_and_is_valid():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    payload = candidates(selected, FakeCandidateClient({'kb-a': counts({'block': {'one': 4}})}, {
        ('kb-a', 'one', 'block'): [node(str(index), number=index) for index in range(4)],
    }), target=1)
    edited = {**payload, 'chunks': [dict(item) for item in payload['chunks']]}
    chosen, replacement = [item for item in edited['chunks'] if item['selected']][0], [item for item in edited['chunks'] if not item['selected']][0]
    chosen.update(selected=False, selection_index=None)
    replacement.update(selected=True, selection_index=0)

    validate_chunk_selection(edited)
    assert {item['chunk_id'] for item in edited['chunks']} == {item['chunk_id'] for item in payload['chunks']}


def test_manual_selection_rejects_quota_mismatch_and_bad_selection_index():
    payload = {
        'chunks': [{'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'one', 'group': 'block', 'selected': True, 'selection_index': 1}],
        'quotas': [{'kb_id': 'kb', 'doc_id': 'doc', 'group': 'block', 'required': 1}],
    }
    with pytest.raises(ValueError, match='selection_index'):
        validate_chunk_selection(payload)
    payload['chunks'][0]['selection_index'] = 0
    payload['quotas'][0]['required'] = 2
    with pytest.raises(ValueError, match='quota'):
        validate_chunk_selection(payload)


def test_direct_candidate_value_update_changes_only_build_chunks_downstream_input():
    payload = {
        'chunks': [
            {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'one', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
             'text': 'one', 'embedding': {'default': [1.0]}, 'metadata': {}, 'selected': True, 'selection_index': 0},
            {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'two', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
             'text': 'two', 'embedding': {'default': [1.0]}, 'metadata': {}, 'selected': False, 'selection_index': None},
        ],
        'quotas': [{'kb_id': 'kb', 'doc_id': 'doc', 'group': 'block', 'required': 1}],
    }
    assert build_chunks(chunk_ctx('chunk_0001'), {'build_chunk_candidates': payload})['chunk']['chunk_id'] == 'one'
    payload['chunks'][0].update(selected=False, selection_index=None)
    payload['chunks'][1].update(selected=True, selection_index=0)
    validate_chunk_selection(payload)
    assert build_chunks(chunk_ctx('chunk_0001'), {'build_chunk_candidates': payload})['chunk']['chunk_id'] == 'two'


# chunks_build.yaml
def test_build_chunks_maps_only_selected_chunks_in_selection_order_and_preserves_payload():
    payload = {'chunks': [
        {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'later', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
         'text': 'later', 'embedding': {'default': [1.0]}, 'metadata': {'page': 2}, 'selected': True, 'selection_index': 1},
        {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'first', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
         'text': 'first', 'embedding': {'default': [1.0]}, 'metadata': {'page': 1}, 'selected': True, 'selection_index': 0},
        {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'hidden', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
         'text': 'hidden', 'embedding': {'default': [1.0]}, 'metadata': {}, 'selected': False, 'selection_index': None},
    ]}

    first = build_chunks(chunk_ctx('chunk_0001'), {'build_chunk_candidates': payload})['chunk']
    second = build_chunks(chunk_ctx('chunk_0002'), {'build_chunk_candidates': payload})['chunk']
    placeholder = build_chunks(chunk_ctx('chunk_0003'), {'build_chunk_candidates': payload})['chunk']

    assert first == {'available': True, **payload['chunks'][1]}
    assert second == {'available': True, **payload['chunks'][0]}
    assert placeholder['available'] is False


# chunks_manifest.yaml
def test_manifest_is_lightweight_stable_overview_and_slot_index():
    chosen = {'available': True, 'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'one', 'filename': 'd.pdf',
              'group': 'block', 'type': 'text', 'text': 'secret', 'embedding': {'default': [1.0]}, 'metadata': {}}
    inputs = {
        'import_cases_manifest': import_manifest(2, 1),
        'build_chunk_candidates': {'summary': {
            'scanned_chunk_count': 4, 'effective_count': 2, 'selected_count': 1, 'shortfall_count': 1,
        }},
        'chunk': (chosen, {'available': False, 'chunk_id': 'unavailable:chunk_0002', 'doc_id': '__unavailable__',
                            'filename': '', 'group': '', 'type': 'placeholder'}),
    }
    output = build_chunks_manifest(manifest_ctx(('chunk_0001', 'chunk_0002')), inputs)['build_chunks_manifest']

    assert output['source'] == {'csv_present': False, 'case_counts': {'target': 2, 'imported': 1, 'automatic': 1}}
    assert output['summary']['chunk_counts'] == {'scanned': 4, 'effective': 2, 'selected': 1, 'shortfall': 1}
    assert output['warnings']
    assert all('text' not in item and 'embedding' not in item and 'metadata' not in item for item in output['chunks'])


def test_manifest_rejects_selected_slot_count_mismatch():
    inputs = {
        'import_cases_manifest': import_manifest(1),
        'build_chunk_candidates': {'summary': {'scanned_chunk_count': 1, 'effective_count': 1, 'selected_count': 1, 'shortfall_count': 0}},
        'chunk': ({'available': False, 'chunk_id': 'unavailable:chunk_0001', 'doc_id': '__unavailable__',
                   'filename': '', 'group': '', 'type': 'placeholder'},),
    }
    with pytest.raises(ValueError, match='available|selected'):
        build_chunks_manifest(manifest_ctx(('chunk_0001',)), inputs)


def test_manifest_imported_only_allows_placeholder_slots_without_warning():
    inputs = {
        'import_cases_manifest': import_manifest(1, 1),
        'build_chunk_candidates': {'summary': {'scanned_chunk_count': 0, 'effective_count': 0, 'selected_count': 0, 'shortfall_count': 0}},
        'chunk': ({'available': False, 'chunk_id': 'unavailable:chunk_0001', 'doc_id': '__unavailable__',
                   'filename': '', 'group': '', 'type': 'placeholder'},),
    }
    output = build_chunks_manifest(manifest_ctx(('chunk_0001',)), inputs)['build_chunks_manifest']
    assert output['warnings'] == []


def test_build_chunks_params_reject_legacy_excluded_chunks_and_invalid_limits():
    with pytest.raises(ValueError, match='excluded_chunks'):
        BuildChunksParams.from_dict({'excluded_chunks': []})
    with pytest.raises(ValueError, match='max_scan_chunks'):
        BuildChunksParams.from_dict({'max_scan_chunks': 0})
