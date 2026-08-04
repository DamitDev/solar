"""Unit tests for dataset registration, update, and deletion services."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.harbor as harbor_mod
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    InvalidArtifactNameError,
)
from app.repositories.artifacts import ArtifactMetadataRecord
from app.schemas.datasets import (
    RegisterDatasetVersionRequest,
    UpdateDatasetMetadataRequest,
)
from app.services.models import (
    DatasetDeletionService,
    DatasetRegistrationService,
    DatasetUpdateService,
)

pytestmark = pytest.mark.asyncio

_HARBOR_REF = "registry.example.com/proj/iris-tickets:2026-03"


def _make_request(**kwargs) -> RegisterDatasetVersionRequest:
    defaults: dict[str, Any] = {"harbor_ref": _HARBOR_REF}
    defaults.update(kwargs)
    return RegisterDatasetVersionRequest.model_validate(defaults)


def _make_harbor_info(digest="sha256:abc", content_length=1024):
    info = MagicMock()
    info.digest = digest
    info.content_length = content_length
    return info


def _make_harbor_mock(info=None, side_effect=None) -> AsyncMock:
    client = AsyncMock()
    if side_effect is not None:
        client.verify_artifact.side_effect = side_effect
    else:
        client.verify_artifact.return_value = info or _make_harbor_info()
    return client


def _make_repo_mock(existing_versions=None) -> AsyncMock:
    repo = AsyncMock()
    repo.upsert_or_fetch_artifact.return_value = "artifact-id"
    repo.get_existing_auto_versions.return_value = (
        existing_versions if existing_versions is not None else []
    )
    repo.insert_artifact_version.return_value = None
    repo.touch_artifact_updated_at.return_value = None
    repo.delete_dataset_version.return_value = None
    repo.get_artifact_metadata.return_value = ArtifactMetadataRecord(
        name="iris-tickets",
        category="dataset",
        description="desc",
        metadata={},
        created_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        versions_count=1,
    )
    return repo


@asynccontextmanager
async def _svc(
    service_class, *, harbor=None, existing_versions=None, upsert_side_effect=None
):
    """Factory to instantiate the correct service class with shared repo mocks."""
    mock_harbor = harbor if harbor is not None else _make_harbor_mock()
    mock_repo = _make_repo_mock(existing_versions=existing_versions)

    if upsert_side_effect is not None:
        mock_repo.upsert_or_fetch_artifact.side_effect = upsert_side_effect

    with patch("app.services.models.ArtifactRepository", return_value=mock_repo):
        session = AsyncMock()
        if service_class == DatasetRegistrationService:
            svc = service_class(harbor=mock_harbor, session=session)
        else:
            svc = service_class(session=session)
        yield svc, mock_repo


# --- REGISTRATION TESTS ---


async def test_response_shape_dataset_category():
    async with _svc(DatasetRegistrationService) as (svc, _):
        result = await svc.register_dataset_version(
            "iris-tickets", _make_request(version="v1")
        )

    assert result.name == "iris-tickets"
    assert result.version == "v1"
    assert result.harbor_ref == _HARBOR_REF
    assert result.category == "dataset"


async def test_auto_version_first_is_v1():
    async with _svc(DatasetRegistrationService, existing_versions=[]) as (svc, _):
        result = await svc.register_dataset_version("iris-tickets", _make_request())

    assert result.version == "v1"


async def test_invalid_name_raises():
    async with _svc(DatasetRegistrationService) as (svc, _):
        with pytest.raises(InvalidArtifactNameError):
            await svc.register_dataset_version("BAD", _make_request())


@pytest.mark.parametrize("reserved", ["latest", "Latest", "LATEST"])
async def test_register_rejects_latest_as_version(reserved: str):
    async with _svc(DatasetRegistrationService) as (svc, _):
        with pytest.raises(InvalidArtifactNameError, match="reserved"):
            await svc.register_dataset_version(
                "iris-tickets", _make_request(version=reserved)
            )


async def test_harbor_not_found_maps_to_domain_error():
    harbor = _make_harbor_mock(
        side_effect=harbor_mod.ArtifactNotFoundError("not found")
    )
    async with _svc(DatasetRegistrationService, harbor=harbor) as (svc, _):
        with pytest.raises(ArtifactNotFoundInHarborError):
            await svc.register_dataset_version("iris-tickets", _make_request())


async def test_metadata_dumped_to_dict_for_insert():
    async with _svc(DatasetRegistrationService) as (svc, repo):
        await svc.register_dataset_version(
            "iris-tickets",
            _make_request(metadata={"description": "tickets", "format": "hdf5"}),
        )

    # Repository call validation
    assert repo.insert_artifact_version.call_args[0][5] == {
        "description": "tickets",
        "format": "hdf5",
    }


async def test_category_conflict_propagates():
    async with _svc(
        DatasetRegistrationService,
        upsert_side_effect=ArtifactCategoryConflictError("conflict"),
    ) as (svc, _):
        with pytest.raises(ArtifactCategoryConflictError):
            await svc.register_dataset_version("iris-tickets", _make_request())


# --- UPDATE TESTS ---


async def test_update_dataset_metadata_passes_dataset_metadata_to_repo():
    current = ArtifactMetadataRecord(
        name="iris-tickets",
        category="dataset",
        description="desc",
        metadata={},
        created_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        versions_count=1,
    )

    async with _svc(DatasetUpdateService) as (svc, repo):
        repo.get_artifact_metadata = AsyncMock(return_value=current)
        repo.update_artifact_metadata = AsyncMock(return_value=current)
        await svc.update_dataset_metadata(
            "iris-tickets",
            UpdateDatasetMetadataRequest(
                description="updated",
                training_config={"format": "parquet", "record_count": 42},
            ),
        )

    repo.get_artifact_metadata.assert_awaited_once_with(
        category="dataset",
        name="iris-tickets",
    )
    repo.update_artifact_metadata.assert_awaited_once()


async def test_update_dataset_metadata_without_metadata_does_not_touch_latest_version():
    current = ArtifactMetadataRecord(
        name="iris-tickets",
        category="dataset",
        description="desc",
        metadata={"training_config": {"format": "json"}},
        created_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        versions_count=1,
    )

    async with _svc(DatasetUpdateService) as (svc, repo):
        repo.get_artifact_metadata = AsyncMock(return_value=current)
        repo.update_artifact_metadata = AsyncMock(return_value=current)
        await svc.update_dataset_metadata(
            "iris-tickets",
            UpdateDatasetMetadataRequest(description="only description"),
        )

    repo.update_artifact_metadata.assert_awaited_once_with(
        category="dataset",
        name="iris-tickets",
        description="only description",
        set_description=True,
        raw_metadata=None,
        set_metadata=False,
    )


# --- DELETION TESTS ---


async def test_delete_dataset_version_calls_repo():
    async with _svc(DatasetDeletionService) as (svc, repo):
        await svc.delete_dataset_version("iris-tickets", "v2")

    repo.delete_dataset_version.assert_awaited_once_with(
        name="iris-tickets",
        version="v2",
    )


async def test_delete_dataset_version_invalid_name_raises():
    async with _svc(DatasetDeletionService) as (svc, _):
        with pytest.raises(InvalidArtifactNameError):
            await svc.delete_dataset_version("BAD", "v1")


async def test_delete_dataset_version_rejects_latest_alias():
    async with _svc(DatasetDeletionService) as (svc, _):
        # Match matches the message in BaseArtifactDeletionService
        with pytest.raises(InvalidArtifactNameError, match="reserved alias"):
            await svc.delete_dataset_version("iris-tickets", "latest")


@pytest.mark.parametrize(
    "exc",
    [DatasetNotFoundError("missing"), DatasetVersionNotFoundError("missing")],
)
async def test_delete_dataset_version_not_found_propagates(exc):
    async with _svc(DatasetDeletionService) as (svc, repo):
        repo.delete_dataset_version = AsyncMock(side_effect=exc)
        with pytest.raises(type(exc)):
            await svc.delete_dataset_version("iris-tickets", "v1")
