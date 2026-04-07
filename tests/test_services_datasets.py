"""Unit tests for dataset registration service."""

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.harbor as harbor_mod
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
)
from app.schemas.datasets import RegisterDatasetVersionRequest
from app.services.models import DatasetRegistrationService

pytestmark = pytest.mark.asyncio

_HARBOR_REF = "registry.example.com/proj/iris-tickets:2026-03"


def _make_request(**kwargs) -> RegisterDatasetVersionRequest:
    defaults: dict[str, Any] = {"harbor_ref": _HARBOR_REF}
    defaults.update(kwargs)
    return RegisterDatasetVersionRequest.model_validate(defaults)


def _make_harbor_info(*, digest="sha256:abc", content_length=1024):
    info = MagicMock()
    info.digest = digest
    info.content_length = content_length
    return info


def _make_harbor_mock(*, info=None, side_effect=None) -> AsyncMock:
    client = AsyncMock()
    if side_effect is not None:
        client.verify_artifact.side_effect = side_effect
    else:
        client.verify_artifact.return_value = info or _make_harbor_info()
    return client


def _make_repo_mock(*, existing_versions=None) -> AsyncMock:
    repo = AsyncMock()
    repo.upsert_or_fetch_artifact.return_value = "artifact-id"
    repo.get_existing_auto_versions.return_value = (
        existing_versions if existing_versions is not None else []
    )
    repo.insert_artifact_version.return_value = None
    repo.touch_artifact_updated_at.return_value = None
    return repo


@asynccontextmanager
async def _svc(*, harbor=None, existing_versions=None, upsert_side_effect=None):
    mock_harbor = harbor if harbor is not None else _make_harbor_mock()
    mock_repo = _make_repo_mock(existing_versions=existing_versions)
    if upsert_side_effect is not None:
        mock_repo.upsert_or_fetch_artifact.side_effect = upsert_side_effect

    with patch("app.services.models.ArtifactRepository", return_value=mock_repo):
        svc = DatasetRegistrationService(harbor=mock_harbor, session=AsyncMock())
        yield svc, mock_harbor, mock_repo


async def test_response_shape_dataset_category():
    async with _svc() as (svc, _, __):
        result = await svc.register_dataset_version(
            "iris-tickets", _make_request(version="v1")
        )

    assert result.name == "iris-tickets"
    assert result.version == "v1"
    assert result.harbor_ref == _HARBOR_REF
    assert result.category == "dataset"


async def test_auto_version_first_is_v1():
    async with _svc(existing_versions=[]) as (svc, _, __):
        result = await svc.register_dataset_version("iris-tickets", _make_request())

    assert result.version == "v1"


async def test_invalid_name_raises():
    async with _svc() as (svc, _, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.register_dataset_version("BAD", _make_request())


@pytest.mark.parametrize("reserved", ["latest", "Latest", "LATEST"])
async def test_register_rejects_latest_as_version(reserved: str):
    async with _svc() as (svc, _, __):
        with pytest.raises(InvalidArtifactNameError, match="reserved"):
            await svc.register_dataset_version(
                "iris-tickets", _make_request(version=reserved)
            )


async def test_harbor_not_found_maps_to_domain_error():
    harbor = _make_harbor_mock(
        side_effect=harbor_mod.ArtifactNotFoundError("not found")
    )
    async with _svc(harbor=harbor) as (svc, _, __):
        with pytest.raises(ArtifactNotFoundInHarborError):
            await svc.register_dataset_version("iris-tickets", _make_request())


async def test_harbor_api_error_maps_to_verification_error():
    harbor = _make_harbor_mock(
        side_effect=harbor_mod.HarborAPIError("api failure", status_code=500)
    )
    async with _svc(harbor=harbor) as (svc, _, __):
        with pytest.raises(HarborVerificationError):
            await svc.register_dataset_version("iris-tickets", _make_request())


async def test_metadata_dumped_to_dict_for_insert():
    async with _svc() as (svc, _, repo):
        await svc.register_dataset_version(
            "iris-tickets",
            _make_request(metadata={"description": "tickets", "format": "hdf5"}),
        )

    assert repo.insert_artifact_version.call_args[0][5] == {
        "description": "tickets",
        "format": "hdf5",
    }


async def test_category_conflict_propagates():
    async with _svc(upsert_side_effect=ArtifactCategoryConflictError("conflict")) as (
        svc,
        _,
        __,
    ):
        with pytest.raises(ArtifactCategoryConflictError):
            await svc.register_dataset_version("iris-tickets", _make_request())
