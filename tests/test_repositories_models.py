"""Unit tests for app/repositories/models.py — ModelArtifactRepository.

All SQLAlchemy I/O is replaced with AsyncMock so these tests run without a
live database.  The session is injected via the constructor, making it trivial
to substitute.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.exceptions import ArtifactCategoryConflictError, VersionAlreadyExistsError
from app.repositories.models import ModelArtifactRepository

pytestmark = pytest.mark.asyncio

_ARTIFACT_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(artifact_id: uuid.UUID = _ARTIFACT_ID, category: str = "model"):
    row = MagicMock()
    row.id = artifact_id
    row.category = category
    return row


def _execute_returning(row):
    """Return an AsyncMock for session.execute() whose .fetchone() gives *row*."""
    result = MagicMock()
    result.fetchone.return_value = row
    execute_mock = AsyncMock(return_value=result)
    return execute_mock


# ---------------------------------------------------------------------------
# upsert_or_fetch_artifact
# ---------------------------------------------------------------------------


async def test_upsert_inserts_new_artifact_and_returns_id(mock_session):
    """INSERT succeeds (row returned) — returns the artifact UUID directly."""
    mock_session.execute = _execute_returning(_make_row())
    repo = ModelArtifactRepository(mock_session)

    result = await repo.upsert_or_fetch_artifact("my-model")

    assert result == _ARTIFACT_ID


async def test_upsert_falls_back_to_select_for_existing_model(mock_session):
    """INSERT hits the conflict path (returns None); SELECT finds a model row."""
    insert_result = MagicMock()
    insert_result.fetchone.return_value = None
    select_result = MagicMock()
    select_result.fetchone.return_value = _make_row()

    mock_session.execute = AsyncMock(side_effect=[insert_result, select_result])
    repo = ModelArtifactRepository(mock_session)

    result = await repo.upsert_or_fetch_artifact("my-model")

    assert result == _ARTIFACT_ID
    assert mock_session.execute.call_count == 2


async def test_upsert_raises_category_conflict_for_non_model(mock_session):
    """INSERT returns None; SELECT finds a 'dataset' row — conflict raised."""
    insert_result = MagicMock()
    insert_result.fetchone.return_value = None
    select_result = MagicMock()
    select_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[insert_result, select_result])
    repo = ModelArtifactRepository(mock_session)

    with pytest.raises(ArtifactCategoryConflictError):
        await repo.upsert_or_fetch_artifact("my-model")


async def test_upsert_raises_runtime_error_when_row_missing(mock_session):
    """INSERT and SELECT both return None — RuntimeError raised."""
    none_result = MagicMock()
    none_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[none_result, none_result])
    repo = ModelArtifactRepository(mock_session)

    with pytest.raises(RuntimeError, match="Could not find or create"):
        await repo.upsert_or_fetch_artifact("my-model")


# ---------------------------------------------------------------------------
# get_existing_auto_versions
# ---------------------------------------------------------------------------


async def test_get_existing_auto_versions_returns_list(mock_session):
    scalars = MagicMock()
    scalars.all.return_value = ["v1", "v2", "v3"]
    result = MagicMock()
    result.scalars.return_value = scalars
    mock_session.execute = AsyncMock(return_value=result)

    repo = ModelArtifactRepository(mock_session)
    versions = await repo.get_existing_auto_versions(_ARTIFACT_ID)

    assert list(versions) == ["v1", "v2", "v3"]
    mock_session.execute.assert_called_once()


async def test_get_existing_auto_versions_returns_empty_list(mock_session):
    scalars = MagicMock()
    scalars.all.return_value = []
    result = MagicMock()
    result.scalars.return_value = scalars
    mock_session.execute = AsyncMock(return_value=result)

    repo = ModelArtifactRepository(mock_session)
    versions = await repo.get_existing_auto_versions(_ARTIFACT_ID)

    assert list(versions) == []


# ---------------------------------------------------------------------------
# insert_artifact_version
# ---------------------------------------------------------------------------


async def test_insert_artifact_version_succeeds(mock_session):
    """flush() succeeds — add() is called once and no exception is raised."""
    repo = ModelArtifactRepository(mock_session)

    await repo.insert_artifact_version(
        artifact_id=_ARTIFACT_ID,
        version="v1",
        harbor_ref="registry.example.com/proj/model:v1",
        digest="sha256:abc",
        size_bytes=1024,
        metadata={},
    )

    mock_session.add.assert_called_once()
    mock_session.flush.assert_called_once()


async def test_insert_artifact_version_raises_on_duplicate(mock_session):
    """flush() raises IntegrityError — VersionAlreadyExistsError propagated."""
    mock_session.flush = AsyncMock(
        side_effect=IntegrityError(None, None, Exception("duplicate"))
    )
    repo = ModelArtifactRepository(mock_session)

    with pytest.raises(VersionAlreadyExistsError, match="v1"):
        await repo.insert_artifact_version(
            artifact_id=_ARTIFACT_ID,
            version="v1",
            harbor_ref="registry.example.com/proj/model:v1",
            digest=None,
            size_bytes=None,
            metadata={},
        )


# ---------------------------------------------------------------------------
# touch_artifact_updated_at
# ---------------------------------------------------------------------------


async def test_touch_artifact_updated_at_executes_once(mock_session):
    repo = ModelArtifactRepository(mock_session)

    await repo.touch_artifact_updated_at(_ARTIFACT_ID)

    mock_session.execute.assert_called_once()
