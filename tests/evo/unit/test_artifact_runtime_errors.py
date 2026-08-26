import pytest

from evo.artifact_runtime.errors import DefinitionError, _text


def test_text_returns_validated_non_empty_string() -> None:
    assert _text('case-1', 'case_id') == 'case-1'


def test_text_rejects_blank_string() -> None:
    with pytest.raises(DefinitionError, match='case_id must be non-empty'):
        _text('   ', 'case_id')


def test_case_id_normalization_preserves_unique_ids() -> None:
    # case_operation_statuses normalizes via _text; returning None would
    # falsely trigger "case_ids must not contain duplicates".
    case_ids = ('case-1', 'case-2', 'case-3')
    normalized = tuple(_text(case_id, 'case_id') for case_id in case_ids)
    assert normalized == case_ids
    assert len(set(normalized)) == len(normalized)
