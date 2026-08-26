"""Regression: user artifact commits must work when the run is failed.

Topic rename / material apply go through commit_values → _commit_artifacts.
A failed topic-discovery (or later) run used to reject those commits with
'cannot commit artifact from failed', blocking apply in the UI.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evo.artifact_runtime import ArtifactCommit, ArtifactDraft, ArtifactKey
from evo.artifact_runtime.errors import DefinitionError
from evo.artifact_runtime.session import RunSession


def _session(*, status: str) -> RunSession:
    session = object.__new__(RunSession)
    session.run_id = 'thr-failed'
    session._status = status
    session._definition = SimpleNamespace(validate_commit=lambda _commit: None)
    session._store = SimpleNamespace(commit=AsyncMock(return_value=SimpleNamespace(status='ok')))
    session._refresh_plan = AsyncMock()
    session._continue_after_change = AsyncMock()
    session._enter_running = AsyncMock()
    session._persist_status = AsyncMock()
    session._schedule = AsyncMock()
    session._publish = AsyncMock()
    session._decision = None
    session._active = {}
    return session


def _commit() -> ArtifactCommit:
    key = ArtifactKey.scalar('dataset.topic_manifest')
    return ArtifactCommit(
        'dataset-topic-names:test',
        'user:apply',
        (ArtifactDraft(key, {'topics': []}),),
        {key: None},
    )


def test_commit_artifacts_allows_failed_status() -> None:
    session = _session(status='failed')

    async def run() -> None:
        await session._commit_artifacts(_commit())

    asyncio.run(run())

    session._store.commit.assert_awaited_once()
    session._continue_after_change.assert_awaited_once_with('failed', cancel_invalidated=True)


def test_commit_artifacts_still_rejects_terminal_non_editable() -> None:
    session = _session(status='canceled')

    async def run() -> None:
        with pytest.raises(DefinitionError, match='cannot commit artifact from canceled'):
            await session._commit_artifacts(_commit())

    asyncio.run(run())
    session._store.commit.assert_not_awaited()


def test_continue_after_change_recovers_from_failed() -> None:
    session = _session(status='failed')
    # Use the real method under test (not the AsyncMock stub).
    session._continue_after_change = RunSession._continue_after_change.__get__(session, RunSession)

    async def run() -> None:
        await session._continue_after_change('failed', cancel_invalidated=False)

    asyncio.run(run())
    session._enter_running.assert_awaited_once_with()
    session._schedule.assert_not_awaited()
    session._publish.assert_not_awaited()
