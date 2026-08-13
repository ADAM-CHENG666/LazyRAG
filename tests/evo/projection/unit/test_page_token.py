from __future__ import annotations

import pytest

from evo.artifact_runtime import ArtifactKey, ArtifactRef
from evo.service.contracts import ServiceError
from evo.service.projections import ProjectionService


def _revision() -> str:
    return ProjectionService._build_revision((
        ArtifactRef(ArtifactKey.scalar('dataset.topic_manifest'), 3),
    ))


def _token(*, offset: int = 50) -> str:
    return ProjectionService._build_page_token(
        thread_id='thr-1',
        list_name='dataset.topics',
        revision=_revision(),
        filters=ProjectionService._normalize_filters({'question_type': 'precision'}),
        page_size=50,
        next_offset=offset,
    )


def test_page_token_recovers_the_next_page_query_context() -> None:
    token = _token()

    context = ProjectionService._resolve_page_token(
        token,
        thread_id='thr-1',
        list_name='dataset.topics',
        filters=ProjectionService._normalize_filters({'question_type': 'precision'}),
        page_size=50,
    )

    assert context['revision'] == _revision()
    assert context['next_offset'] == 50


def test_next_page_token_advances_only_the_next_page_position() -> None:
    first = _token(offset=50)
    second = _token(offset=100)

    first_context = ProjectionService._resolve_page_token(
        first, thread_id='thr-1', list_name='dataset.topics',
        filters=ProjectionService._normalize_filters({'question_type': 'precision'}), page_size=50,
    )
    second_context = ProjectionService._resolve_page_token(
        second, thread_id='thr-1', list_name='dataset.topics',
        filters=ProjectionService._normalize_filters({'question_type': 'precision'}), page_size=50,
    )

    assert first_context['revision'] == second_context['revision']
    assert first_context['next_offset'] == 50
    assert second_context['next_offset'] == 100


@pytest.mark.parametrize('thread_id,list_name,filters,page_size', [
    ('thr-2', 'dataset.topics', {'question_type': 'precision'}, 50),
    ('thr-1', 'dataset.cases', {'question_type': 'precision'}, 50),
    ('thr-1', 'dataset.topics', {'question_type': 'reasoning'}, 50),
    ('thr-1', 'dataset.topics', {'question_type': 'precision'}, 100),
])
def test_page_token_rejects_a_different_query_context(thread_id: str, list_name: str,
                                                      filters: dict[str, str], page_size: int) -> None:
    with pytest.raises(ServiceError) as error:
        ProjectionService._resolve_page_token(
            _token(), thread_id=thread_id, list_name=list_name,
            filters=ProjectionService._normalize_filters(filters), page_size=page_size,
        )

    assert error.value.status_code == 400


@pytest.mark.parametrize('token', ['', 'not-a-page-token', '%%%%'])
def test_page_token_rejects_malformed_values(token: str) -> None:
    with pytest.raises(ServiceError) as error:
        ProjectionService._resolve_page_token(
            token, thread_id='thr-1', list_name='dataset.topics',
            filters=ProjectionService._normalize_filters({'question_type': 'precision'}), page_size=50,
        )

    assert error.value.status_code == 400
