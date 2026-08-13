from types import SimpleNamespace

import pytest

from evo.operations.dataset import Chunk, ChunkSource, chunk_from_docnode, chunks_from_docnodes


def node(**kwargs):
    defaults = {
        'uid': 'chunk-1',
        'text': 'chunk text',
        'embedding': {'__default__': [1.0, 2.0]},
        'group': 'block',
        'metadata': {'type': 'table'},
        'global_metadata': {'filename': 'fallback.pdf'},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_chunk_from_docnode_builds_standard_chunk_and_copies_mutable_fields():
    source_node = node()
    doc = {'doc_id': 'doc-1', 'filename': 'file.pdf', 'extra': {'kept': True}}

    chunk = chunk_from_docnode(source_node, kb_id='kb-1', doc_id='doc-1', group='line', doc=doc)

    assert chunk == Chunk(
        chunk_id='chunk-1',
        text='chunk text',
        embedding={'model': '__default__', 'vector': [0.4472135954999579, 0.8944271909999159]},
        entities=[],
        group='block',
        type='table',
        source=ChunkSource(
            kb_id='kb-1',
            doc_id='doc-1',
            filename='file.pdf',
            metadata={
                'doc': doc,
                'node_metadata': {'type': 'table'},
                'node_global_metadata': {'filename': 'fallback.pdf'},
            },
        ),
    )

    source_node.embedding['__default__'].append(3.0)
    source_node.metadata['type'] = 'changed'
    doc['filename'] = 'changed.pdf'
    assert chunk.embedding == {'model': '__default__', 'vector': [0.4472135954999579, 0.8944271909999159]}
    assert chunk.type == 'table'
    assert chunk.source.filename == 'file.pdf'


def test_chunk_from_docnode_uses_group_argument_and_node_type_fallbacks():
    chunk = chunk_from_docnode(
        node(group='', metadata={'node_type': 'formula'}, global_metadata={'file_name': 'global.pdf'}),
        kb_id='kb-1',
        doc_id='doc-1',
        group='line',
        doc={'display_name': 'display.pdf'},
    )

    assert chunk.group == 'line'
    assert chunk.type == 'formula'
    assert chunk.source.filename == 'display.pdf'


def test_chunk_from_docnode_normalizes_numeric_embedding_values_to_floats():
    chunk = chunk_from_docnode(
        node(embedding={'embed_main': ['1', '-0.25', 3]}),
        kb_id='kb-1',
        doc_id='doc-1',
        group='block',
    )

    assert chunk.embedding == {
        'model': 'embed_main',
        'vector': [0.31524416249564025, -0.07881104062391006, 0.9457324874869207],
    }


@pytest.mark.parametrize('embedding', [
    {'embed_main': ['not-a-number']},
    {'embed_main': [float('nan')]},
    {'embed_main': [True]},
])
def test_chunk_from_docnode_rejects_invalid_embedding_values(embedding):
    with pytest.raises(ValueError, match='embedding vector'):
        chunk_from_docnode(
            node(embedding=embedding),
            kb_id='kb-1',
            doc_id='doc-1',
            group='block',
        )


def test_chunk_from_docnode_preserves_original_text():
    chunk = chunk_from_docnode(node(text='  original text\n'), kb_id='kb-1', doc_id='doc-1', group='block')

    assert chunk.text == '  original text\n'


@pytest.mark.parametrize(
    ('kwargs', 'match'),
    [
        ({'uid': ''}, 'chunk_id is required'),
        ({'text': ''}, 'text is required'),
        ({'group': ''}, 'group is required'),
    ],
)
def test_chunk_from_docnode_requires_core_node_fields(kwargs, match):
    with pytest.raises(ValueError, match=match):
        chunk_from_docnode(node(**kwargs), kb_id='kb-1', doc_id='doc-1', group='')


@pytest.mark.parametrize(
    ('kb_id', 'doc_id', 'match'),
    [
        ('', 'doc-1', 'kb_id is required'),
        ('kb-1', '', 'doc_id is required'),
    ],
)
def test_chunk_from_docnode_requires_source_identity(kb_id, doc_id, match):
    with pytest.raises(ValueError, match=match):
        chunk_from_docnode(node(), kb_id=kb_id, doc_id=doc_id, group='block')


def test_chunks_from_docnodes_converts_batch():
    chunks = chunks_from_docnodes(
        [node(uid='chunk-1'), node(uid='chunk-2')],
        kb_id='kb-1',
        doc_id='doc-1',
        group='block',
        doc={'filename': 'file.pdf'},
    )

    assert [chunk.chunk_id for chunk in chunks] == ['chunk-1', 'chunk-2']
    assert [chunk.source.filename for chunk in chunks] == ['file.pdf', 'file.pdf']
