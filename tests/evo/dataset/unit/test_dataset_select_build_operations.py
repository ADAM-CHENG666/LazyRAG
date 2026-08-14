from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from evo.artifact_runtime import ArtifactKey
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


def documents(rows, excluded=(), knowledge_bases=None, *, target=2, imported=0, knowledge_base_names=None):
    knowledge_bases = knowledge_bases or [
        {'kb_id': kb_id, 'included': True} for kb_id in rows
    ]
    return select_docs(None, {
        'source_config': {'kb_ids': list(rows), 'knowledge_base_names': knowledge_base_names or {}},
        'select_docs_params': {
            'knowledge_bases': knowledge_bases,
            'excluded_docs': [{'kb_id': kb_id, 'doc_id': doc_id} for kb_id, doc_id in excluded],
        },
        'import_cases_manifest': import_manifest(target, imported),
    }, FakeDiscoveryClient(rows))['selected_docs']


def candidates(selected, client, target=2, params=None):
    return build_chunk_candidates(None, {
        'selected_docs': selected,
        'import_cases_manifest': import_manifest(target),
        'build_chunks_params': params or {'groups': ['block']},
    }, client)['build_chunk_candidates']


def chunk_ctx(partition):
    return SimpleNamespace(output_key_by_name={'chunk': ArtifactKey('dataset.chunk', partition)})


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


def test_docs_persist_core_authoritative_knowledge_base_display_name():
    output = documents(
        {'kb-a': [{'doc_id': 'a-1', 'knowledge_base_name': 'untrusted doc server name'}]},
        knowledge_base_names={'kb-a': '产品知识库'},
    )

    assert output['documents'][0]['knowledge_base_name'] == '产品知识库'


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


def test_docs_keep_disabled_knowledge_base_visible_but_excluded():
    output = documents(
        {'kb-a': [{'doc_id': 'one'}], 'kb-b': [{'doc_id': 'two'}]},
        knowledge_bases=[
            {'kb_id': 'kb-a', 'included': False},
            {'kb_id': 'kb-b', 'included': True},
        ],
    )

    assert [(item['kb_id'], item['included']) for item in output['documents']] == [
        ('kb-a', False), ('kb-b', True),
    ]


def test_docs_knowledge_base_toggle_preserves_document_exclusions():
    rows = {'kb-a': [{'doc_id': 'one'}, {'doc_id': 'two'}]}
    disabled = documents(rows, [('kb-a', 'two')], [{'kb_id': 'kb-a', 'included': False}])
    enabled = documents(rows, [('kb-a', 'two')], [{'kb_id': 'kb-a', 'included': True}])

    assert [item['included'] for item in disabled['documents']] == [False, False]
    assert [item['included'] for item in enabled['documents']] == [True, False]


def test_docs_are_skipped_for_imported_only_configuration():
    client = FakeDiscoveryClient({'kb-a': [{'doc_id': 'one'}]})
    output = select_docs(None, {
        'source_config': {'kb_ids': ['kb-a']},
        'select_docs_params': {'knowledge_bases': [{'kb_id': 'kb-a', 'included': True}], 'excluded_docs': []},
        'import_cases_manifest': import_manifest(1, 1),
    }, client)['selected_docs']

    assert output == {
        'documents': [],
        'stats': {'discovered_count': 0, 'included_count': 0, 'excluded_count': 0},
    }
    assert client.calls == []


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
        select_docs(None, {
            'source_config': {'kb_ids': ['kb-a']},
            'select_docs_params': {'knowledge_bases': [{'kb_id': 'kb-a', 'included': True}], 'excluded_docs': excluded},
        },
                    FakeDiscoveryClient({'kb-a': []}))


@pytest.mark.parametrize('knowledge_bases', [
    [],
    [{'kb_id': 'kb-a', 'included': True}],
    [{'kb_id': 'kb-a', 'included': True}, {'kb_id': 'kb-b', 'included': True}, {'kb_id': 'kb-b', 'included': False}],
])
def test_docs_reject_knowledge_base_configuration_outside_source_scope(knowledge_bases):
    with pytest.raises(ValueError, match='knowledge_bases'):
        select_docs(None, {
            'source_config': {'kb_ids': ['kb-a', 'kb-b']},
            'select_docs_params': {'knowledge_bases': knowledge_bases, 'excluded_docs': []},
        }, FakeDiscoveryClient({'kb-a': [], 'kb-b': []}))


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


def test_candidates_normalize_reader_layout_types_before_allowed_type_filtering():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    client = FakeCandidateClient(
        {'kb-a': counts({'block': {'one': 2}})},
        {('kb-a', 'one', 'block'): [node('image', kind='image', number=1), node('equation', kind='equation', number=2)]},
    )

    output = candidates(selected, client, target=1, params={
        'groups': ['block'], 'allowed_types': ['figure', 'formula'],
    })

    assert [item['type'] for item in output['chunks']] == ['figure', 'formula']


def test_candidates_reject_non_standard_allowed_type_ids():
    with pytest.raises(ValueError, match='allowed_types'):
        BuildChunksParams.from_dict({'allowed_types': ['not-a-layout-type']})


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


def test_candidates_use_chunk_id_as_the_only_selected_chunk_identity():
    selected = documents({'kb-a': [{'doc_id': 'one'}]})
    client = FakeCandidateClient({'kb-a': counts({'block': {'one': 5}})}, {
        ('kb-a', 'one', 'block'): [node(str(index), number=index) for index in range(5)],
    })

    output = candidates(selected, client, target=2)
    assert all('selection_index' not in item for item in output['chunks'])


def test_candidates_reject_duplicate_chunk_id_across_knowledge_bases():
    selected = documents({'kb-a': [{'doc_id': 'one'}], 'kb-b': [{'doc_id': 'two'}]})
    client = FakeCandidateClient(
        {'kb-a': counts({'block': {'one': 1}}), 'kb-b': counts({'block': {'two': 1}})},
        {
            ('kb-a', 'one', 'block'): [node('shared-chunk', doc_id='one')],
            ('kb-b', 'two', 'block'): [node('shared-chunk', doc_id='two')],
        },
    )

    with pytest.raises(ValueError, match='duplicate chunk id: shared-chunk'):
        candidates(selected, client, target=1)


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
    chosen.update(selected=False)
    replacement.update(selected=True)

    validate_chunk_selection(edited)
    assert {item['chunk_id'] for item in edited['chunks']} == {item['chunk_id'] for item in payload['chunks']}


def test_manual_selection_rejects_quota_mismatch_without_a_selection_order():
    payload = {
        'chunks': [{'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'one', 'group': 'block', 'selected': True}],
        'quotas': [{'kb_id': 'kb', 'doc_id': 'doc', 'group': 'block', 'required': 1}],
    }
    payload['quotas'][0]['required'] = 2
    with pytest.raises(ValueError, match='quota'):
        validate_chunk_selection(payload)


def test_direct_candidate_value_update_changes_only_build_chunks_downstream_input():
    payload = {
        'chunks': [
            {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'one', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
             'text': 'one', 'embedding': {'default': [1.0]}, 'metadata': {}, 'selected': True},
            {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'two', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
             'text': 'two', 'embedding': {'default': [1.0]}, 'metadata': {}, 'selected': False},
        ],
        'quotas': [{'kb_id': 'kb', 'doc_id': 'doc', 'group': 'block', 'required': 1}],
    }
    assert build_chunks(chunk_ctx('one'), {'build_chunk_candidates': payload})['chunk']['chunk_id'] == 'one'
    payload['chunks'][0]['selected'] = False
    payload['chunks'][1]['selected'] = True
    validate_chunk_selection(payload)
    assert build_chunks(chunk_ctx('two'), {'build_chunk_candidates': payload})['chunk']['chunk_id'] == 'two'


# chunks_build.yaml
def test_build_chunks_reads_the_selected_chunk_with_its_chunk_id_partition_key():
    payload = {'chunks': [
        {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'later', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
         'text': 'later', 'embedding': {'default': [1.0]}, 'metadata': {'page': 2}, 'selected': True},
        {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'first', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
         'text': 'first', 'embedding': {'default': [1.0]}, 'metadata': {'page': 1}, 'selected': True},
        {'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'hidden', 'filename': 'd.pdf', 'group': 'block', 'type': 'text',
         'text': 'hidden', 'embedding': {'default': [1.0]}, 'metadata': {}, 'selected': False},
    ]}

    first = build_chunks(chunk_ctx('first'), {'build_chunk_candidates': payload})['chunk']
    second = build_chunks(chunk_ctx('later'), {'build_chunk_candidates': payload})['chunk']
    assert first == {'available': True, **payload['chunks'][1]}
    assert second == {'available': True, **payload['chunks'][0]}


def test_build_chunks_preserves_normalized_layout_type():
    payload = {'chunks': [{
        'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'figure', 'filename': 'd.pdf', 'group': 'block',
        'type': 'figure', 'text': 'caption', 'embedding': {'default': [1.0]}, 'metadata': {},
        'selected': True,
    }]}

    assert build_chunks(chunk_ctx('figure'), {'build_chunk_candidates': payload})['chunk']['type'] == 'figure'


# chunks_manifest.yaml
def test_manifest_is_lightweight_stable_overview_and_uses_chunk_id_partition():
    chosen = {'available': True, 'kb_id': 'kb', 'doc_id': 'doc', 'chunk_id': 'one', 'filename': 'd.pdf',
              'group': 'block', 'type': 'text', 'text': 'secret', 'embedding': {'default': [1.0]}, 'metadata': {}}
    inputs = {
        'import_cases_manifest': import_manifest(2, 1),
        'build_chunk_candidates': {'summary': {
            'scanned_chunk_count': 4, 'effective_count': 2, 'selected_count': 1, 'shortfall_count': 1,
        }},
        'chunk': (chosen,),
    }
    output = build_chunks_manifest(None, inputs)['build_chunks_manifest']

    assert output['source'] == {'csv_present': False, 'case_counts': {'target': 2, 'imported': 1, 'automatic': 1}}
    assert output['summary']['chunk_counts'] == {'scanned': 4, 'effective': 2, 'selected': 1, 'shortfall': 1}
    assert output['warnings']
    assert output['chunks'][0]['partition'] == 'one'
    assert all('text' not in item and 'embedding' not in item and 'metadata' not in item for item in output['chunks'])


def test_manifest_rejects_selected_slot_count_mismatch():
    inputs = {
        'import_cases_manifest': import_manifest(1),
        'build_chunk_candidates': {'summary': {'scanned_chunk_count': 1, 'effective_count': 1, 'selected_count': 1, 'shortfall_count': 0}},
        'chunk': (),
    }
    with pytest.raises(ValueError, match='available|selected'):
        build_chunks_manifest(None, inputs)


def test_manifest_imported_only_has_no_chunks_and_no_warning():
    inputs = {
        'import_cases_manifest': import_manifest(1, 1),
        'build_chunk_candidates': {'summary': {'scanned_chunk_count': 0, 'effective_count': 0, 'selected_count': 0, 'shortfall_count': 0}},
        'chunk': (),
    }
    output = build_chunks_manifest(None, inputs)['build_chunks_manifest']
    assert output['warnings'] == []
    assert output['chunks'] == []


def test_build_chunks_params_reject_legacy_excluded_chunks_and_invalid_limits():
    with pytest.raises(ValueError, match='excluded_chunks'):
        BuildChunksParams.from_dict({'excluded_chunks': []})
    with pytest.raises(ValueError, match='max_scan_chunks'):
        BuildChunksParams.from_dict({'max_scan_chunks': 0})
