from __future__ import annotations

import json
from types import SimpleNamespace

from evo.artifact_runtime import AttemptSnapshot
from evo.service.events import _execution_event_frame
from evo.service.projections import flow_events


def test_partitioned_attempt_uses_generic_partition_field() -> None:
    snapshot = SimpleNamespace(
        run_id='thread-1',
        runtime=SimpleNamespace(partition_sets={}),
    )
    definition = SimpleNamespace(
        stages=(SimpleNamespace(name='dataset'),),
        stage_index_for_operation=lambda operation_id: 0,
    )
    attempt = AttemptSnapshot(
        attempt_id='attempt-1',
        invocation_id='invocation-1',
        operation_id='dataset.label_embedding_cluster',
        partition_key='candidate-2',
        status='running',
        created_at=1.0,
    )

    items = flow_events(snapshot, (attempt,), (), {'dataset': ()}, {'dataset': ()}, {}, definition)

    assert items[0]['partition'] == {'id': 'candidate-2'}
    assert 'case' not in items[0]


def test_sse_event_name_is_not_duplicated_in_data() -> None:
    frame = _execution_event_frame({
        'event_id': 'thread-1:attempt-1:start',
        'event_type': 'dataset.label_embedding_cluster',
        'stage': 'dataset',
        'partition': {'id': 'candidate-2'},
        'status': 'running',
    })

    payload = json.loads(frame['data'])

    assert frame['id'] == 'thread-1:attempt-1:start'
    assert frame['event'] == 'dataset.label_embedding_cluster'
    assert payload['partition'] == {'id': 'candidate-2'}
    assert 'event_type' not in payload
    assert 'type' not in payload
