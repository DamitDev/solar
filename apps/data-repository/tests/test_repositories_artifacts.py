"""Unit tests for app/repositories/artifacts.py — ArtifactRepository.

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
    CatalogArtifactNotFoundError,
    CatalogVersionNotFoundError,
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    VersionAlreadyExistsError,
)
from app.repositories.artifacts import ArtifactRepository

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


def _make_artifact_row(
    artifact_id: uuid.UUID = _ARTIFACT_ID,
    *,
    name: str = "mymodel",
    category: str = "model",
    description: str | None = "desc",
    created_at: datetime = datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
):
    row = MagicMock()
    row.id = artifact_id
    row.name = name
    row.category = category
    row.description = description
    row.created_at = created_at
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
    repo = ArtifactRepository(mock_session)

    result = await repo.upsert_or_fetch_artifact("my-model")

    assert result == _ARTIFACT_ID


async def test_upsert_falls_back_to_select_for_existing_model(mock_session):
    """INSERT hits the conflict path (returns None); SELECT finds a model row."""
    insert_result = MagicMock()
    insert_result.fetchone.return_value = None
    select_result = MagicMock()
    select_result.fetchone.return_value = _make_row()

    mock_session.execute = AsyncMock(side_effect=[insert_result, select_result])
    repo = ArtifactRepository(mock_session)

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
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ArtifactCategoryConflictError):
        await repo.upsert_or_fetch_artifact("my-model")


async def test_upsert_raises_category_conflict_for_non_dataset(mock_session):
    """INSERT returns None; SELECT finds a 'model' row for dataset request."""
    insert_result = MagicMock()
    insert_result.fetchone.return_value = None
    select_result = MagicMock()
    select_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[insert_result, select_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(
        ArtifactCategoryConflictError, match="cannot register as a 'dataset'"
    ):
        await repo.upsert_or_fetch_artifact("iris-tickets", category="dataset")


async def test_upsert_raises_runtime_error_when_row_missing(mock_session):
    """INSERT and SELECT both return None — RuntimeError raised."""
    none_result = MagicMock()
    none_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[none_result, none_result])
    repo = ArtifactRepository(mock_session)

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

    repo = ArtifactRepository(mock_session)
    versions = await repo.get_existing_auto_versions(_ARTIFACT_ID)

    assert list(versions) == ["v1", "v2", "v3"]
    mock_session.execute.assert_called_once()


async def test_get_existing_auto_versions_returns_empty_list(mock_session):
    scalars = MagicMock()
    scalars.all.return_value = []
    result = MagicMock()
    result.scalars.return_value = scalars
    mock_session.execute = AsyncMock(return_value=result)

    repo = ArtifactRepository(mock_session)
    versions = await repo.get_existing_auto_versions(_ARTIFACT_ID)

    assert list(versions) == []


# ---------------------------------------------------------------------------
# insert_artifact_version
# ---------------------------------------------------------------------------


async def test_insert_artifact_version_succeeds(mock_session):
    """flush() succeeds — add() is called once and no exception is raised."""
    repo = ArtifactRepository(mock_session)

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
    repo = ArtifactRepository(mock_session)

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
    repo = ArtifactRepository(mock_session)

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
    repo = ArtifactRepository(mock_session)

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
    repo = ArtifactRepository(mock_session)

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
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.get_model_version(name="mymodel", version="v1")


async def test_get_model_version_raises_when_name_belongs_to_dataset(mock_session):
    """JOIN misses (category filter), existence check finds a dataset row."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.get_model_version(name="mymodel", version="v1")


async def test_get_model_version_raises_when_version_missing(mock_session):
    """JOIN misses, existence check finds a model row — ModelVersionNotFoundError."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelVersionNotFoundError):
        await repo.get_model_version(name="mymodel", version="v99")


async def test_get_model_version_latest_no_versions_gives_clear_message(mock_session):
    """Model exists but has zero versions — error message says 'has no versions'."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelVersionNotFoundError, match="has no versions"):
        await repo.get_model_version(name="mymodel", version="latest")


async def test_list_model_versions_returns_rows_in_db_order(mock_session):
    list_result = MagicMock()
    list_result.fetchall.return_value = [
        _make_version_row(version="v3"),
        _make_version_row(version="v2"),
    ]
    mock_session.execute = AsyncMock(return_value=list_result)
    repo = ArtifactRepository(mock_session)

    result = await repo.list_model_versions(name="mymodel")

    assert [item.version for item in result] == ["v3", "v2"]
    mock_session.execute.assert_awaited_once()


async def test_list_model_versions_empty_when_model_exists(mock_session):
    list_result = MagicMock()
    list_result.fetchall.return_value = []
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[list_result, exists_result])
    repo = ArtifactRepository(mock_session)

    result = await repo.list_model_versions(name="mymodel")

    assert result == []


async def test_list_model_versions_raises_when_model_missing(mock_session):
    list_result = MagicMock()
    list_result.fetchall.return_value = []
    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[list_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.list_model_versions(name="mymodel")


async def test_list_model_versions_raises_when_name_belongs_to_dataset(mock_session):
    """JOIN misses (category filter), existence check finds a dataset row."""
    list_result = MagicMock()
    list_result.fetchall.return_value = []
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[list_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.list_model_versions(name="iris-tickets")


# ---------------------------------------------------------------------------
# delete_model_version
# ---------------------------------------------------------------------------


def _delete_result(rowcount: int):
    result = MagicMock()
    result.rowcount = rowcount
    return result


async def test_delete_model_version_success(mock_session):
    """DELETE affects one row — no existence probe is issued."""
    mock_session.execute = AsyncMock(return_value=_delete_result(1))
    repo = ArtifactRepository(mock_session)

    await repo.delete_model_version(name="mymodel", version="v3")

    mock_session.execute.assert_awaited_once()


async def test_delete_model_version_raises_when_model_missing(mock_session):
    """DELETE affected zero rows and artifact row is absent — model 404."""
    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[_delete_result(0), exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.delete_model_version(name="mymodel", version="v3")


async def test_delete_model_version_raises_when_version_missing(mock_session):
    """DELETE affected zero rows but the model exists — version 404."""
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[_delete_result(0), exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelVersionNotFoundError, match="v99"):
        await repo.delete_model_version(name="mymodel", version="v99")


async def test_delete_model_version_raises_when_name_belongs_to_dataset(mock_session):
    """DELETE affected zero rows (category filter) and exists row is dataset."""
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[_delete_result(0), exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.delete_model_version(name="iris-tickets", version="v1")


async def test_get_dataset_version_exact_returns_record(mock_session):
    """JOIN query hits — single execute call returns the dataset version row."""
    version_row = _make_version_row(
        version="v3",
        harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v3",
    )
    join_result = MagicMock()
    join_result.fetchone.return_value = version_row

    mock_session.execute = AsyncMock(return_value=join_result)
    repo = ArtifactRepository(mock_session)

    result = await repo.get_dataset_version(name="iris-tickets", version="v3")

    assert result.name == "iris-tickets"
    assert result.version == "v3"
    assert result.category == "dataset"
    assert result.harbor_ref == "imgrepo.damit.hu/supernova/iris-tickets:v3"
    assert result.checksum == "sha256:abc"
    assert isinstance(result.created_at, datetime)
    assert result.created_at.tzinfo is not None
    mock_session.execute.assert_awaited_once()


async def test_get_dataset_version_latest_returns_most_recent_row(mock_session):
    """JOIN query with ORDER BY created_at DESC LIMIT 1 returns newest row."""
    latest_row = _make_version_row(version="v7")
    join_result = MagicMock()
    join_result.fetchone.return_value = latest_row

    mock_session.execute = AsyncMock(return_value=join_result)
    repo = ArtifactRepository(mock_session)

    result = await repo.get_dataset_version(name="iris-tickets", version="latest")

    assert result.version == "v7"
    mock_session.execute.assert_awaited_once()


async def test_get_dataset_version_raises_when_dataset_missing(mock_session):
    """JOIN misses, existence check returns None — DatasetNotFoundError."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetNotFoundError):
        await repo.get_dataset_version(name="iris-tickets", version="v1")


async def test_get_dataset_version_raises_when_name_belongs_to_model(mock_session):
    """JOIN misses (category filter), existence check finds a model row."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetNotFoundError):
        await repo.get_dataset_version(name="iris-tickets", version="v1")


async def test_get_dataset_version_raises_when_version_missing(mock_session):
    """JOIN misses, existence check finds dataset row — DatasetVersionNotFoundError."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetVersionNotFoundError):
        await repo.get_dataset_version(name="iris-tickets", version="v99")


async def test_get_dataset_version_latest_no_versions_gives_clear_message(mock_session):
    """Dataset exists but has zero versions — error says 'has no versions'."""
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetVersionNotFoundError, match="has no versions"):
        await repo.get_dataset_version(name="iris-tickets", version="latest")


async def test_list_dataset_versions_returns_rows_in_db_order(mock_session):
    list_result = MagicMock()
    list_result.fetchall.return_value = [
        _make_version_row(version="v7"),
        _make_version_row(version="v6"),
    ]
    mock_session.execute = AsyncMock(return_value=list_result)
    repo = ArtifactRepository(mock_session)

    result = await repo.list_dataset_versions(name="iris-tickets")

    assert [item.version for item in result] == ["v7", "v6"]
    mock_session.execute.assert_awaited_once()


async def test_list_dataset_versions_empty_when_dataset_exists(mock_session):
    """Dataset row exists but has zero versions — returns []."""
    list_result = MagicMock()
    list_result.fetchall.return_value = []
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[list_result, exists_result])
    repo = ArtifactRepository(mock_session)

    result = await repo.list_dataset_versions(name="iris-tickets")

    assert result == []


async def test_list_dataset_versions_raises_when_dataset_missing(mock_session):
    list_result = MagicMock()
    list_result.fetchall.return_value = []
    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[list_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetNotFoundError):
        await repo.list_dataset_versions(name="iris-tickets")


async def test_list_dataset_versions_raises_when_name_belongs_to_model(mock_session):
    """JOIN misses (category filter), existence check finds a model row."""
    list_result = MagicMock()
    list_result.fetchall.return_value = []
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[list_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetNotFoundError):
        await repo.list_dataset_versions(name="mymodel")


# ---------------------------------------------------------------------------
# delete_dataset_version
# ---------------------------------------------------------------------------


async def test_delete_dataset_version_success(mock_session):
    mock_session.execute = AsyncMock(return_value=_delete_result(1))
    repo = ArtifactRepository(mock_session)

    await repo.delete_dataset_version(name="iris-tickets", version="v3")

    mock_session.execute.assert_awaited_once()


async def test_delete_dataset_version_raises_when_dataset_missing(mock_session):
    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[_delete_result(0), exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetNotFoundError):
        await repo.delete_dataset_version(name="iris-tickets", version="v3")


async def test_delete_dataset_version_raises_when_version_missing(mock_session):
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="dataset")

    mock_session.execute = AsyncMock(side_effect=[_delete_result(0), exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetVersionNotFoundError, match="v99"):
        await repo.delete_dataset_version(name="iris-tickets", version="v99")


async def test_delete_dataset_version_raises_when_name_belongs_to_model(mock_session):
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row(category="model")

    mock_session.execute = AsyncMock(side_effect=[_delete_result(0), exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(DatasetNotFoundError):
        await repo.delete_dataset_version(name="mymodel", version="v1")


# ---------------------------------------------------------------------------
# list_artifacts_by_category
# ---------------------------------------------------------------------------


def _make_list_row(
    *,
    name: str | None = "mymodel",
    category: str | None = "model",
    description: str | None = None,
    versions_count: int | None = 2,
    latest_version: str | None = "v2",
    created_at: datetime | None = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
    match_total: int = 2,
):
    row = MagicMock()
    row.match_total = match_total
    row.name = name
    row.category = category
    row.description = description
    row.versions_count = versions_count
    row.latest_version = latest_version
    row.created_at = created_at
    return row


async def test_list_artifacts_by_category_returns_records(mock_session):
    """Returns ``(total, records)`` with one ArtifactListRecord per page row."""
    list_result = MagicMock()
    list_result.fetchall.return_value = [
        _make_list_row(name="model-a", versions_count=3, latest_version="v3"),
        _make_list_row(name="model-b", versions_count=1, latest_version="v1"),
    ]
    mock_session.execute = AsyncMock(return_value=list_result)
    repo = ArtifactRepository(mock_session)

    total, result = await repo.list_artifacts_by_category("model")

    assert total == 2
    assert len(result) == 2
    assert result[0].name == "model-a"
    assert result[0].versions_count == 3
    assert result[0].latest_version == "v3"
    assert result[1].name == "model-b"
    mock_session.execute.assert_awaited_once()


async def test_list_artifacts_by_category_returns_empty_when_no_matches(mock_session):
    """Single summary row with ``total`` 0 and no page rows yields empty list."""
    list_result = MagicMock()
    list_result.fetchall.return_value = [
        _make_list_row(
            name=None,
            category=None,
            description=None,
            versions_count=None,
            latest_version=None,
            created_at=None,
            match_total=0,
        )
    ]
    mock_session.execute = AsyncMock(return_value=list_result)
    repo = ArtifactRepository(mock_session)

    total, result = await repo.list_artifacts_by_category("model")

    assert total == 0
    assert result == []


async def test_list_artifacts_by_category_zero_versions_coerced(mock_session):
    """versions_count of None (no versions, outer join) is coerced to 0."""
    list_result = MagicMock()
    row = _make_list_row(versions_count=None, latest_version=None)  # type: ignore[arg-type]
    list_result.fetchall.return_value = [row]
    mock_session.execute = AsyncMock(return_value=list_result)
    repo = ArtifactRepository(mock_session)

    total, result = await repo.list_artifacts_by_category("model")

    assert total == 2
    assert result[0].versions_count == 0
    assert result[0].latest_version is None


async def test_list_artifacts_by_category_passes_search_and_pagination(mock_session):
    """Verify execute is called once (single round-trip) for search + pagination."""
    list_result = MagicMock()
    list_result.fetchall.return_value = []
    mock_session.execute = AsyncMock(return_value=list_result)
    repo = ArtifactRepository(mock_session)

    await repo.list_artifacts_by_category("dataset", search="iris", limit=10, offset=5)

    mock_session.execute.assert_awaited_once()


async def test_list_artifacts_by_category_total_when_page_is_empty(mock_session):
    """Offset past last row: one row with ``match_total`` and null page columns."""
    list_result = MagicMock()
    list_result.fetchall.return_value = [
        _make_list_row(
            name=None,
            category=None,
            description=None,
            versions_count=None,
            latest_version=None,
            created_at=None,
            match_total=42,
        )
    ]
    mock_session.execute = AsyncMock(return_value=list_result)
    repo = ArtifactRepository(mock_session)

    total, result = await repo.list_artifacts_by_category("model", limit=10, offset=999)

    assert total == 42
    assert result == []


async def test_get_artifact_metadata_returns_latest_metadata_and_count(mock_session):
    artifact_result = MagicMock()
    artifact_result.fetchone.return_value = _make_artifact_row(
        name="mymodel",
        category="model",
        description="Classifier",
    )

    count_result = MagicMock()
    count_result.scalar_one.return_value = 3

    latest_row = MagicMock()
    latest_row.metadata_ = {"training_config": {"epochs": 3}}
    latest_result = MagicMock()
    latest_result.fetchone.return_value = latest_row

    mock_session.execute = AsyncMock(
        side_effect=[artifact_result, count_result, latest_result]
    )
    repo = ArtifactRepository(mock_session)

    result = await repo.get_artifact_metadata(category="model", name="mymodel")

    assert result.name == "mymodel"
    assert result.category == "model"
    assert result.description == "Classifier"
    assert result.versions_count == 3
    assert result.metadata == {"training_config": {"epochs": 3}}


async def test_get_artifact_metadata_raises_not_found_for_missing_model(mock_session):
    artifact_result = MagicMock()
    artifact_result.fetchone.return_value = None

    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[artifact_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(ModelNotFoundError):
        await repo.get_artifact_metadata(category="model", name="mymodel")


async def test_update_artifact_metadata_updates_description_and_latest_version_jsonb(
    mock_session,
):
    first_artifact_result = MagicMock()
    first_artifact_result.fetchone.return_value = _make_artifact_row(
        name="mymodel",
        category="model",
        description="old",
    )

    update_artifact_result = MagicMock()

    latest_id_row = MagicMock()
    latest_id_row.id = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
    latest_id_result = MagicMock()
    latest_id_result.fetchone.return_value = latest_id_row

    update_latest_result = MagicMock()

    second_artifact_result = MagicMock()
    second_artifact_result.fetchone.return_value = _make_artifact_row(
        name="mymodel",
        category="model",
        description="new",
    )

    count_result = MagicMock()
    count_result.scalar_one.return_value = 2

    final_latest_row = MagicMock()
    final_latest_row.metadata_ = {"eval_metrics": {"accuracy": 0.99}}
    final_latest_result = MagicMock()
    final_latest_result.fetchone.return_value = final_latest_row

    mock_session.execute = AsyncMock(
        side_effect=[
            first_artifact_result,
            update_artifact_result,
            latest_id_result,
            update_latest_result,
            second_artifact_result,
            count_result,
            final_latest_result,
        ]
    )
    repo = ArtifactRepository(mock_session)

    result = await repo.update_artifact_metadata(
        category="model",
        name="mymodel",
        description="new",
        set_description=True,
        raw_metadata={"eval_metrics": {"accuracy": 0.99}},
        set_metadata=True,
    )

    assert result.description == "new"
    assert result.metadata == {"eval_metrics": {"accuracy": 0.99}}


async def test_artifact_version_reference_exists_respects_category_filter(mock_session):
    exists_result = MagicMock()
    exists_result.fetchone.return_value = MagicMock()

    mock_session.execute = AsyncMock(return_value=exists_result)
    repo = ArtifactRepository(mock_session)

    result = await repo.artifact_version_reference_exists(
        name="mymodel",
        version="v3",
        category="model",
    )

    assert result is True
    mock_session.execute.assert_awaited_once()


async def test_update_artifact_version_metadata_updates_resolved_version(mock_session):
    current_version_row = _make_version_row(version="v3", metadata={"a": 1})
    current_result = MagicMock()
    current_result.fetchone.return_value = current_version_row

    updated_version_row = _make_version_row(version="v3", metadata={"a": 2})
    updated_result = MagicMock()
    updated_result.fetchone.return_value = updated_version_row

    mock_session.execute = AsyncMock(side_effect=[current_result, updated_result])
    repo = ArtifactRepository(mock_session)

    result = await repo.update_artifact_version_metadata(
        category="model",
        name="mymodel",
        version="latest",
        metadata={"a": 2},
    )

    assert result.version == "v3"
    assert result.metadata == {"a": 2}


# ---------------------------------------------------------------------------
# resolve_artifact_version
# ---------------------------------------------------------------------------


async def test_resolve_artifact_version_exact_returns_record(mock_session):
    """JOIN query hits — returns the artifact version row regardless of category."""
    version_row = _make_version_row(version="v3")
    version_row.category = "model"
    join_result = MagicMock()
    join_result.fetchone.return_value = version_row

    mock_session.execute = AsyncMock(return_value=join_result)
    repo = ArtifactRepository(mock_session)

    result = await repo.resolve_artifact_version(name="any-artifact", version="v3")

    assert result.name == "any-artifact"
    assert result.version == "v3"
    assert result.category == "model"
    mock_session.execute.assert_awaited_once()


async def test_resolve_artifact_version_latest_returns_newest(mock_session):
    latest_row = _make_version_row(version="v10")
    latest_row.category = "dataset"
    join_result = MagicMock()
    join_result.fetchone.return_value = latest_row

    mock_session.execute = AsyncMock(return_value=join_result)
    repo = ArtifactRepository(mock_session)

    result = await repo.resolve_artifact_version(name="any-artifact", version="latest")

    assert result.version == "v10"
    assert result.category == "dataset"


async def test_resolve_artifact_version_raises_when_artifact_missing(mock_session):
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = None

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(CatalogArtifactNotFoundError):
        await repo.resolve_artifact_version(name="missing", version="v1")


async def test_resolve_artifact_version_raises_when_version_missing(mock_session):
    join_result = MagicMock()
    join_result.fetchone.return_value = None
    exists_result = MagicMock()
    exists_result.fetchone.return_value = _make_row()

    mock_session.execute = AsyncMock(side_effect=[join_result, exists_result])
    repo = ArtifactRepository(mock_session)

    with pytest.raises(CatalogVersionNotFoundError):
        await repo.resolve_artifact_version(name="exists", version="v99")
