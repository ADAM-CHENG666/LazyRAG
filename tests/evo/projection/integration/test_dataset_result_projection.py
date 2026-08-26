from __future__ import annotations

import asyncio
import csv
import io
from pathlib import Path

from fastapi.testclient import TestClient

from evo import artifacts as A
from evo.artifact_runtime import ArtifactKey, ArtifactRecord, ArtifactRef
from evo.service.api import create_app
from evo.service.projections import ProjectionService


class _ResultFlow:
    def __init__(self, versions: dict[int, dict]) -> None:
        self.versions = versions

    async def has_run(self, _: str) -> bool:
        return True

    async def head(self, _: str, key: ArtifactKey) -> ArtifactRecord | None:
        if key != ArtifactKey.scalar(A.EVAL_DATASET) or not self.versions:
            return None
        return self._record(max(self.versions))

    async def record(self, _: str, ref: ArtifactRef) -> ArtifactRecord | None:
        if ref.key != ArtifactKey.scalar(A.EVAL_DATASET) or ref.version not in self.versions:
            return None
        return self._record(ref.version)

    async def read(self, _: str, ref: ArtifactRef) -> dict:
        return self.versions[ref.version]

    @staticmethod
    def _record(version: int) -> ArtifactRecord:
        return ArtifactRecord(ArtifactRef(ArtifactKey.scalar(A.EVAL_DATASET), version), producer='test')


def _case(case_id: str, *, question_type: str = 'precision') -> dict:
    return {
        'case_id': case_id,
        'question': f'Question {case_id}',
        'question_type': question_type,
        'difficulty': '',
        'ground_truth': f'Answer {case_id}',
        'grading_guidance': 'Check the answer.',
        'key_points': [],
        'forbidden_claims': [],
        'reference_context': [],
        'reference_doc': [],
        'reference_doc_ids': [],
        'reference_chunk_ids': [],
        'generate_reason': '',
        'is_deleted': False,
    }


def test_result_pages_the_final_dataset_artifact() -> None:
    service = ProjectionService(_ResultFlow({3: {
        'case_num': 3,
        'failed_case_num': 1,
        'completed_with_problems': True,
        'cases': [_case('case_0001'), _case('case_0002'), _case('case_0003', question_type='reasoning')],
    }}), definition=None)

    first = asyncio.run(service.dataset_result('thr-1', page_size=2))
    second = asyncio.run(service.dataset_result('thr-1', page_size=2, page_token=first['next_page_token']))

    assert first['total_size'] == 3
    assert first['failed_case_count'] == 1
    assert first['completed_with_problems'] is True
    assert [item['case_id'] for item in first['items']] == ['case_0001', 'case_0002']
    assert [item['case_id'] for item in second['items']] == ['case_0003']
    assert first['revision'] == second['revision']
    assert second['next_page_token'] == ''


def test_result_download_uses_the_requested_revision_and_csv_contract() -> None:
    service = ProjectionService(_ResultFlow({1: {
        'case_num': 1,
        'failed_case_num': 0,
        'completed_with_problems': False,
        'cases': [{
            **_case('external-7'),
            'key_points': [{'statement': 'Point', 'evidence_chunk_ids': ['chunk-1']}],
            'reference_doc_ids': ['doc-1', 'doc-2'],
        }],
    }, 2: {'case_num': 0, 'failed_case_num': 0, 'completed_with_problems': False, 'cases': []}}), definition=None)
    revision = service._build_revision((ArtifactRef(ArtifactKey.scalar(A.EVAL_DATASET), 1),))

    filename, payload = asyncio.run(service.dataset_result_download('thr-1', revision))
    decoded = payload.decode('utf-8-sig')
    rows = list(csv.DictReader(io.StringIO(decoded)))

    assert filename == 'dataset-thr-1.csv'
    assert payload.startswith(b'\xef\xbb\xbf')
    assert list(rows[0]) == [
        'case_id', 'question', 'question_type', 'difficulty', 'ground_truth', 'grading_guidance',
        'key_points', 'forbidden_claims', 'reference_context', 'reference_doc', 'reference_doc_ids',
        'reference_chunk_ids', 'generate_reason', 'is_deleted',
    ]
    assert rows[0]['case_id'] == 'external-7'
    assert rows[0]['reference_doc_ids'] == 'doc-1,doc-2'
    assert rows[0]['key_points'] == '[{"statement":"Point","evidence_chunk_ids":["chunk-1"]}]'


def test_result_http_handlers_delegate_and_download_csv(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple] = []

    class _Projections:
        async def dataset_result(self, thread_id: str, *, page_size=None, page_token='') -> dict:
            calls.append(('result', thread_id, page_size, page_token))
            return {'thread_id': thread_id, 'items': []}

        async def dataset_result_download(self, thread_id: str, revision: str) -> tuple[str, bytes]:
            calls.append(('download', thread_id, revision))
            return 'dataset-thr-1.csv', b'\xef\xbb\xbfcase_id\r\n'

    class _Service:
        projections = _Projections()

        async def close(self) -> None:
            return None

    async def _open(_: Path) -> _Service:
        return _Service()

    monkeypatch.setattr('evo.service.api.EvoService.open', _open)
    with TestClient(create_app(tmp_path)) as client:
        result = client.get('/threads/thr-1/dataset/result', params={'page_size': '20', 'page_token': 'next'})
        download = client.get('/threads/thr-1/dataset/result:download', params={'format': 'csv', 'revision': 'rev-1'})

    assert result.status_code == 200
    assert download.status_code == 200
    assert download.headers['content-type'].startswith('text/csv')
    assert download.headers['content-disposition'] == 'attachment; filename="dataset-thr-1.csv"'
    assert calls == [('result', 'thr-1', 20, 'next'), ('download', 'thr-1', 'rev-1')]
