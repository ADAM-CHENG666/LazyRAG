import pytest

from evo.operations.dataset.entities import chunk_entities_extract_manifest


def _built_chunks(chunks=None):
    return {
        'chunks': chunks or [
            {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block', 'partition': 'chunk-1'},
            {'available': False, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'line', 'partition': 'chunk-2'},
        ],
    }


def _chunk_entities(items=None):
    return items or (
        {'available': False, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'line', 'entities': []},
        {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block', 'entities': ['Tesla']},
    )


def _inputs(*, built_chunks=None, chunk_entities=None, params=None):
    return {
        'build_chunks_manifest': built_chunks or _built_chunks(),
        'chunk_entities': chunk_entities or _chunk_entities(),
        'chunk_entities_extract_manifest_params': params or {},
    }


def test_chunk_entities_extract_manifest_preserves_built_chunk_order():
    output = chunk_entities_extract_manifest(None, _inputs())

    assert output == {
        'chunk_entities_extract_manifest': {
            'chunks': [
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                 'partition': 'chunk-1', 'entities': ['Tesla']},
                {'available': False, 'chunk_id': 'chunk-2', 'doc_id': 'doc-2', 'group': 'line',
                 'partition': 'chunk-2', 'entities': []},
            ],
            'stats': {
                'slot_count': 2,
                'available_count': 1,
                'placeholder_count': 1,
                'entity_count': 1,
                'empty_entity_count': 0,
                'doc_count': 1,
                'group_counts': {'block': 1},
            },
            'params': {'max_entities_per_chunk': 10},
        }
    }


def test_chunk_entities_extract_manifest_uses_default_params():
    output = chunk_entities_extract_manifest(None, _inputs(params={}))

    assert output['chunk_entities_extract_manifest']['params'] == {'max_entities_per_chunk': 10}


@pytest.mark.parametrize(
    ('inputs', 'match'),
    [
        ({'build_chunks_manifest': {'chunks': []}, 'chunk_entities': _chunk_entities(),
          'chunk_entities_extract_manifest_params': {}}, 'built_chunks.chunks must be a non-empty list'),
        ({'build_chunks_manifest': _built_chunks(), 'chunk_entities': [], 'chunk_entities_extract_manifest_params': {}},
         'chunk_entities input must be a partitioned tuple'),
        ({'build_chunks_manifest': _built_chunks(), 'chunk_entities': _chunk_entities(),
          'chunk_entities_extract_manifest_params': {'max_entities_per_chunk': 0}},
         'max_entities_per_chunk must be a positive integer'),
    ],
)
def test_chunk_entities_extract_manifest_rejects_invalid_input(inputs, match):
    with pytest.raises(ValueError, match=match):
        chunk_entities_extract_manifest(None, inputs)


def test_chunk_entities_extract_manifest_rejects_missing_chunk_entity():
    with pytest.raises(ValueError, match=r"missing ChunkEntity for chunk ids: \['chunk-2'\]"):
        chunk_entities_extract_manifest(
            None,
            _inputs(chunk_entities=(
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                 'entities': ['Tesla']},
            )),
        )


def test_chunk_entities_extract_manifest_rejects_duplicate_chunk_entity():
    with pytest.raises(ValueError, match='duplicate ChunkEntity.chunk_id: chunk-1'):
        chunk_entities_extract_manifest(
            None,
            _inputs(chunk_entities=(
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                 'entities': ['Tesla']},
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                 'entities': ['SpaceX']},
            )),
        )


def test_chunk_entities_extract_manifest_reports_stats_for_multiple_available_chunks():
    output = chunk_entities_extract_manifest(
        None,
        _inputs(
            built_chunks={
                'chunks': [
                    {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                     'partition': 'chunk-1'},
                    {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-1', 'group': 'block',
                     'partition': 'chunk-2'},
                    {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-2', 'group': 'table',
                     'partition': 'chunk-3'},
                ],
            },
            chunk_entities=(
                {'available': True, 'chunk_id': 'chunk-3', 'doc_id': 'doc-2', 'group': 'table',
                 'entities': []},
                {'available': True, 'chunk_id': 'chunk-1', 'doc_id': 'doc-1', 'group': 'block',
                 'entities': ['Tesla', 'Shanghai']},
                {'available': True, 'chunk_id': 'chunk-2', 'doc_id': 'doc-1', 'group': 'block',
                 'entities': []},
            ),
        ),
    )

    assert output['chunk_entities_extract_manifest']['stats'] == {
        'slot_count': 3,
        'available_count': 3,
        'placeholder_count': 0,
        'entity_count': 2,
        'empty_entity_count': 2,
        'doc_count': 2,
        'group_counts': {'block': 2, 'table': 1},
    }
