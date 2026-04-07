"""Unit tests for app/repositories/models.py — ModelArtifactRepository.

All SQLAlchemy I/O is replaced with AsyncMock so these tests run without a
live database.  The session is injected via the constructor, making it trivial
to substitute.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.exceptions import (
    ArtifactCategoryConflictError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    VersionAlreadyExistsError,
)
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


# ---------------------------------------------------------------------------
# get_model_version
# ---------------------------------------------------------------------------


def _make_version_row(
    *,
    version: str = "v3",
    harbor_ref: str = "imgrepo.damit.hu/supernova/mymodel:v3",
    size_bytes: int | None = 1024,
    digest: str | None = "sha256:abc",
    created_at: datetime = datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
    metadata: dict | None = None,
):
    row = MagicMock()
    row.version = version
    row.harbor_ref = harbor_ref
    row.size_bytes = size_bytes
    row.digest = digest
    row.created_at = created_at
    row.metadata_ = metadata if metadata is not None else {"a": 1}
    return row


async def test_get_model_version_exact_returns_record(mock_session):
    """JOIN query hits — single execute call returns the version row."""
    version_row = _make_version_row(version="v3")
    join_result = MagicMock()
    join_result.fetchone.return_value = version_row

    mock_session.execute = AsyncMock(return_value=join_result)
    repo = ModelArtifactRepository(mock_session)

    result = await repo.get_model_version(name="mymodel", version="v3")

    assert result.name == "mymodel"
    assert result.version == "v3"
    assert result.category == "model"
    assert result.harbor_ref == "imgrepo.damit.hu/supernova/mymodel:v3"
    assert result.checksum == "sha256:abc"
    assert isinstance(result.created_at, datetime)
    assert result.created_at.tzinfo is not None
    mock_session.execute.assert_awaited_once()


async def test_get_model_version_latest_returns_most_recent_row(mock_session):
    """JOIN query with ORDER BY created_at DESC LIMIT 1 returns the newest row."""
    latest_row = _make_version_row(version="v7")
    join_result = MagicMock()
    join_result.fetchone.return_value = latest_row

    mock_session.execute = AsyncMock(return_value=join_result)
    repo = ModelArtifactRepository(mock_session)

    result = await repo.get_model_version(name="mymodel", version="latest")

    assert result.version == "v7"
    mock_session.execute.assert_awaited_once()


async def test_get_model_version_raises_when_model_missing(mock_session):
    """JOIN misses, existence check returns None — ModelNotFoundError."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ModelArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.get_model_version(name="mymodel", version="v1")


async def test_get_model_version_raises_when_name_belongs_to_dataset(mock_session):
    """JOIN misses (category filter), existence check finds a dataset row."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ModelArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.get_model_version(name="mymodel", version="v1")


async def test_get_model_version_raises_when_version_missing(mock_session):
    """JOIN misses, existence check finds a model row — ModelVersionNotFoundError."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ModelArtifactRepository(mock_session)

    with pytest.raises(ModelVersionNotFoundError):
        await repo.get_model_version(name="mymodel", version="v99")


async def test_get_model_version_latest_no_versions_gives_clear_message(mock_session):
    """Model exists but has zero versions — error message says 'has no versions'."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ModelArtifactRepository(mock_session)

    with pytest.raises(ModelVersionNotFoundError, match="has no versions"):
        await repo.get_model_version(name="mymodel", version="latest")
