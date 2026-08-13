import pytest

from evo.operations.dataset.llm_json import call_json


def test_call_json_returns_validated_object():
    result = call_json(
        lambda _prompt: '{"value":"ok","extra":"ignored"}',
        'base prompt',
        lambda value: value['value'],
    )

    assert result == 'ok'


def test_call_json_repairs_json_before_validation():
    result = call_json(
        lambda _prompt: "{'value': 'ok'}",
        'base prompt',
        lambda value: value['value'],
    )

    assert result == 'ok'


def test_call_json_retries_once_with_repair_instruction_after_content_failure():
    prompts = []
    responses = iter(('{"value":"wrong"}', '{"value":"ok"}'))

    def complete(prompt):
        prompts.append(prompt)
        return next(responses)

    def validate(value):
        if value.get('value') != 'ok':
            raise ValueError('value must be ok')
        return value['value']

    result = call_json(
        complete,
        'base prompt',
        validate,
        repair_instruction=lambda error: f'Repair this: {error}',
    )

    assert result == 'ok'
    assert prompts == [
        'base prompt',
        'base prompt\n\nRepair this: value must be ok',
    ]


def test_call_json_retries_once_with_original_prompt_without_repair_instruction():
    prompts = []
    responses = iter(('not json', '{"value":"ok"}'))

    def complete(prompt):
        prompts.append(prompt)
        return next(responses)

    result = call_json(complete, 'base prompt', lambda value: value['value'])

    assert result == 'ok'
    assert prompts == ['base prompt', 'base prompt']


def test_call_json_propagates_request_error_without_retrying():
    calls = 0

    def complete(_prompt):
        nonlocal calls
        calls += 1
        raise TimeoutError('request timed out')

    with pytest.raises(TimeoutError, match='request timed out'):
        call_json(complete, 'base prompt', lambda value: value)

    assert calls == 1


def test_call_json_raises_concise_error_after_second_content_failure():
    with pytest.raises(
        ValueError,
        match=r'^LLM JSON call failed after 2 attempts: value must be ok$',
    ) as error:
        call_json(
            lambda _prompt: '{"value":"wrong"}',
            'base prompt',
            lambda _value: (_ for _ in ()).throw(ValueError('value must be ok')),
        )

    assert 'diagnostics=' not in str(error.value)
