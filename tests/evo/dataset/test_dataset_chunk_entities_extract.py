import pytest

from evo.operations.dataset.entities import chunk_entities_extract


def _chunk(**overrides):
    chunk = {
        'available': True,
        'chunk_id': 'chunk-1',
        'doc_id': 'doc-1',
        'group': 'block',
        'text': 'Elon Musk leads Tesla.',
    }
    chunk.update(overrides)
    return chunk


def _inputs(*, chunk=None, params=None):
    return {
        'chunk': _chunk(**(chunk or {})),
        'chunk_entities_extract_params': params or {},
    }


def test_chunk_entities_extract_returns_business_payload():
    output = chunk_entities_extract(
        None,
        _inputs(params={'max_entities_per_chunk': 3}),
        llm_complete=lambda prompt: '{"entities":["Elon Musk","Tesla"]}',
    )

    assert output == {
        'chunk_entity': {
            'available': True,
            'chunk_id': 'chunk-1',
            'doc_id': 'doc-1',
            'group': 'block',
            'entities': ['Elon Musk', 'Tesla'],
        }
    }


def test_chunk_entities_extract_uses_default_params_in_prompt():
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return '{"entities":[]}'

    output = chunk_entities_extract(None, _inputs(), llm_complete=complete)

    assert output['chunk_entity']['entities'] == []
    assert len(prompts) == 1
    assert 'max_num: 10' in prompts[0]
    assert 'Elon Musk leads Tesla.' in prompts[0]


def test_chunk_entities_extract_skips_llm_for_placeholder_chunk():
    called = False

    def complete(prompt):
        nonlocal called
        called = True
        return '{"entities":["unused"]}'

    output = chunk_entities_extract(
        None,
        _inputs(chunk={
            'available': False,
            'chunk_id': 'unavailable:case_0002',
            'doc_id': '__unavailable__',
            'group': 'block',
            'text': '',
        }),
        llm_complete=complete,
    )

    assert called is False
    assert output == {
        'chunk_entity': {
            'available': False,
            'chunk_id': 'unavailable:case_0002',
            'doc_id': '__unavailable__',
            'group': 'block',
            'entities': [],
        }
    }


@pytest.mark.parametrize(
    ('inputs', 'match'),
    [
        ({'chunk': 'bad', 'chunk_entities_extract_params': {}}, 'chunk must be a mapping'),
        ({'chunk': _chunk(chunk_id=''), 'chunk_entities_extract_params': {}}, 'chunk_id must be a non-empty string'),
        ({'chunk': _chunk(doc_id=''), 'chunk_entities_extract_params': {}}, 'doc_id must be a non-empty string'),
        ({'chunk': _chunk(group=''), 'chunk_entities_extract_params': {}}, 'group must be a non-empty string'),
        ({'chunk': _chunk(text=''), 'chunk_entities_extract_params': {}}, 'chunk.text must be a non-empty string'),
        ({'chunk': _chunk(), 'chunk_entities_extract_params': {'max_entities_per_chunk': 0}},
         'max_entities_per_chunk must be a positive integer'),
        ({'chunk': _chunk(), 'chunk_entities_extract_params': {'max_entities_per_chunk': 101}},
         'max_entities_per_chunk must be <= 100'),
    ],
)
def test_chunk_entities_extract_rejects_invalid_input(inputs, match):
    with pytest.raises(ValueError, match=match):
        chunk_entities_extract(None, inputs, llm_complete=lambda prompt: '{"entities":[]}')


def test_chunk_entities_extract_allows_empty_entities():
    output = chunk_entities_extract(
        None,
        _inputs(params={'max_entities_per_chunk': 2}),
        llm_complete=lambda prompt: '{"entities":[]}',
    )

    assert output['chunk_entity']['entities'] == []


def test_chunk_entities_extract_retries_once_after_invalid_llm_output():
    prompts = []
    responses = iter([
        'not-json',
        '{"entities":["Elon Musk"]}',
    ])

    def complete(prompt):
        prompts.append(prompt)
        return next(responses)

    output = chunk_entities_extract(None, _inputs(), llm_complete=complete)

    assert output['chunk_entity']['entities'] == ['Elon Musk']
    assert len(prompts) == 2
    assert prompts[1] == prompts[0]


def test_chunk_entities_extract_fails_after_json_helper_retry_exhausted():
    def complete(prompt):
        return '{"entities":["a","b","c"]}'

    with pytest.raises(ValueError, match='LLM JSON call failed after 2 attempts'):
        chunk_entities_extract(
            None,
            _inputs(params={'max_entities_per_chunk': 2}),
            llm_complete=complete,
        )


def test_chunk_entities_extract_uses_json_mode_and_recovers_decorated_json():
    options = []

    def complete(prompt, **kwargs):
        options.append(kwargs)
        return '<think>internal</think> answer: {"entities":["Tesla"]}'

    output = chunk_entities_extract(None, _inputs(), llm_complete=complete)

    assert output['chunk_entity']['entities'] == ['Tesla']
    assert options == [{
        'stream': False,
        'response_format': {'type': 'json_object'},
        'timeout': 180,
    }]


def test_chunk_entities_extract_rejects_too_many_entities():
    with pytest.raises(ValueError, match='LLM JSON call failed after 2 attempts'):
        chunk_entities_extract(
            None,
            _inputs(params={'max_entities_per_chunk': 1}),
            llm_complete=lambda prompt: '{"entities":["Elon Musk","Tesla"]}',
        )
