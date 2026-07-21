from __future__ import annotations

from types import SimpleNamespace

import pytest

from evo.artifact_runtime.evo import catalog as C
from evo.artifact_runtime.evo.adapter import build_evo_artifact_adapter
from evo.artifact_runtime.evo.flow import DatasetFlowSpec
from evo.artifact_runtime.evo.flow_ops import dataset_evo_ops
from evo.artifact_runtime.kernel import ArtifactKey, SQLiteArtifactStore
from evo.operations.dataset.select_docs import select_docs


class FakeKnowledgeBaseClient:
    def __init__(self, documents=None):
        self.documents = documents or []
        self.list_calls = []

    def list_documents(self, kb_id):
        self.list_calls.append(kb_id)
        return list(self.documents)


def test_select_docs_materializer_returns_selected_docs_payload():
    client = FakeKnowledgeBaseClient([
        {'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'upload_status': 'success',
         'group_counts': {'block': 8}},
        {'doc_id': 'doc-2', 'filename': 'b.pdf', 'file_type': 'pdf'},
    ])

    output = select_docs(
        SimpleNamespace(),
        {'source_config': {'kb_ids': ['kb-1'], 'max_docs': 1},
         'import_cases_manifest': _import_manifest(auto_case_count=7)},
        kb_client=client,
    )

    assert set(output) == {'selected_docs'}
    assert output['selected_docs'] == {
        'kb_ids': ['kb-1'],
        'docs': [
            {'kb_id': 'kb-1', 'doc_id': 'doc-1', 'filename': 'a.pdf', 'file_type': 'pdf', 'status': 'success',
             'group_counts': {'block': 8}},
        ],
        'stats': {'matched_by_kb': {'kb-1': 2}, 'selected_by_kb': {'kb-1': 1}, 'matched': 2, 'selected': 1},
        'params': {'kb_ids': ['kb-1'], 'max_docs': 1, 'auto_case_count': 7},
    }
    assert client.list_calls == ['kb-1']


def test_select_docs_materializer_rejects_empty_selection():
    client = FakeKnowledgeBaseClient([])

    with pytest.raises(ValueError, match='selected no documents'):
        select_docs(SimpleNamespace(), {
            'source_config': {'kb_ids': ['kb-1']},
            'import_cases_manifest': _import_manifest(auto_case_count=1),
        }, kb_client=client)


def test_select_docs_fixed_op_materializes_dataset_selected_docs(tmp_path):
    spec = DatasetFlowSpec.from_case_count(1)
    select_docs_op = next(op for op in dataset_evo_ops(spec) if op.op_id == 'dataset.select_docs')
    store = SQLiteArtifactStore(tmp_path / 'store')
    client = FakeKnowledgeBaseClient([{'doc_id': 'doc-1', 'filename': 'a.pdf'}])
    adapter = build_evo_artifact_adapter(
        store,
        (select_docs_op,),
        {
            'dataset.select_docs': lambda ctx, inputs: select_docs(ctx, inputs, kb_client=client),
        },
    )
    run_id = 'run-1'
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.CORPUS_SOURCE_CONFIG),
        {'kb_ids': ['kb-1']},
        idempotency_key='seed:source_config',
    )
    adapter.commit_external(
        run_id,
        ArtifactKey.of(C.DATASET_IMPORT_CASES_MANIFEST),
        _import_manifest(auto_case_count=1),
        idempotency_key='seed:import_cases_manifest',
    )

    tick = adapter.tick(run_id)

    assert tick.status == 'ok'
    ref = adapter.effective_artifacts(run_id)[ArtifactKey.of(C.DATASET_SELECTED_DOCS)]
    record = adapter.get(run_id, ref)
    assert record is not None
    assert record.value['kb_ids'] == ['kb-1']
    assert record.value['docs'][0]['doc_id'] == 'doc-1'


def _import_manifest(*, auto_case_count):
    return {'stats': {'case_allocation': {'auto_case_count': auto_case_count}}}
