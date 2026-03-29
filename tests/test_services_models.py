"""Unit tests for app/services/models.py — ModelRegistrationService.

All database and Harbor I/O is replaced with unittest.mock fakes so these
tests run without a live database or Harbor instance.

HarborClient is injected directly into the constructor (no patching needed).
ModelArtifactRepository is intercepted via patch so the session mock is never
exercised — the service builds the repo internally and the test swaps it out.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    VersionAlreadyExistsError,
)
from app.harbor import (
    ArtifactNotFoundError,
    HarborAPIError,
    HarborAuthError,
    HarborConnectionError,
)
from app.schemas.models import RegisterModelVersionRequest
from app.services.models import ModelRegistrationService

# Context-manager helper used by many tests.
from contextlib import asynccontextmanager

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARTIFACT_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_HARBOR_REF = "registry.example.com/proj/my-model:v1"


def _make_request(**kwargs) -> RegisterModelVersionRequest:
    defaults = {"harbor_ref": _HARBOR_REF}
    defaults.update(kwargs)
    return RegisterModelVersionRequest(**defaults)


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


def _make_repo_mock(
    *,
    artifact_id: uuid.UUID = _ARTIFACT_ID,
    existing_versions=None,
    insert_side_effect=None,
) -> AsyncMock:
    repo = AsyncMock()
    repo.upsert_or_fetch_artifact.return_value = artifact_id
    repo.get_existing_auto_versions.return_value = (
        existing_versions if existing_versions is not None else []
    )
    if insert_side_effect is not None:
        repo.insert_artifact_version.side_effect = insert_side_effect
    else:
        repo.insert_artifact_version.return_value = None
    repo.touch_artifact_updated_at.return_value = None
    return repo


def _make_service(
    harbor: AsyncMock,
    repo: AsyncMock,
) -> tuple["ModelRegistrationService", "patch"]:
    """Return (service, active patch context) with ModelArtifactRepository swapped."""
    return ModelRegistrationService(harbor=harbor, session=AsyncMock()), repo


@asynccontextmanager
async def _svc(
    *,
    harbor=None,
    existing_versions=None,
    repo_overrides=None,
    insert_side_effect=None,
    upsert_side_effect=None,
):
    mock_harbor = harbor if harbor is not None else _make_harbor_mock()
    mock_repo = _make_repo_mock(
        existing_versions=existing_versions,
        insert_side_effect=insert_side_effect,
    )
    if upsert_side_effect is not None:
        mock_repo.upsert_or_fetch_artifact.side_effect = upsert_side_effect
    if repo_overrides:
        for attr, val in repo_overrides.items():
            setattr(mock_repo, attr, val)

    with patch("app.services.models.ModelArtifactRepository", return_value=mock_repo):
        svc = ModelRegistrationService(harbor=mock_harbor, session=AsyncMock())
        yield svc, mock_harbor, mock_repo


# ---------------------------------------------------------------------------
# Name-validation tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "A",
        "-startshyphen",
        ".startsdot",
        "UPPERCASE",
        "has space",
        "a" * 256,
    ],
)
async def test_invalid_artifact_name_raises(bad_name: str):
    async with _svc() as (svc, _, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.register_model_version(bad_name, _make_request())


@pytest.mark.parametrize(
    "good_name",
    ["a", "my-model", "model_v2", "model.v3", "a" + "b" * 254],
)
async def test_valid_artifact_name_does_not_raise_for_name(good_name: str):
    async with _svc() as (svc, _, __):
        result = await svc.register_model_version(
            good_name, _make_request(version="v1")
        )
    assert result.name == good_name


# ---------------------------------------------------------------------------
# Harbor-error mapping tests
# ---------------------------------------------------------------------------


async def test_harbor_not_found_raises_artifact_not_found_in_harbor():
    harbor = _make_harbor_mock(side_effect=ArtifactNotFoundError("not found"))
    async with _svc(harbor=harbor) as (svc, _, __):
        with pytest.raises(ArtifactNotFoundInHarborError):
            await svc.register_model_version("mymodel", _make_request())


@pytest.mark.parametrize(
    "exc",
    [
        HarborAuthError("auth failure"),
        HarborConnectionError("conn failure"),
        HarborAPIError("api failure", status_code=500),
    ],
    ids=["HarborAuthError", "HarborConnectionError", "HarborAPIError"],
)
async def test_harbor_errors_raise_harbor_verification_error(exc):
    harbor = _make_harbor_mock(side_effect=exc)
    async with _svc(harbor=harbor) as (svc, _, __):
        with pytest.raises(HarborVerificationError):
            await svc.register_model_version("mymodel", _make_request())


# ---------------------------------------------------------------------------
# Auto-versioning logic
# ---------------------------------------------------------------------------


async def test_auto_version_first_is_v1():
    async with _svc(existing_versions=[]) as (svc, _, __):
        result = await svc.register_model_version("mymodel", _make_request())
    assert result.version == "v1"


async def test_auto_version_increments_from_existing():
    async with _svc(existing_versions=["v1", "v2", "v3"]) as (svc, _, __):
        result = await svc.register_model_version("mymodel", _make_request())
    assert result.version == "v4"


async def test_auto_version_non_contiguous_gap():
    async with _svc(existing_versions=["v1", "v5"]) as (svc, _, __):
        result = await svc.register_model_version("mymodel", _make_request())
    assert result.version == "v6"


async def test_explicit_version_passed_through():
    async with _svc(existing_versions=["v1"]) as (svc, _, __):
        result = await svc.register_model_version(
            "mymodel", _make_request(version="2026-03")
        )
    assert result.version == "2026-03"


# ---------------------------------------------------------------------------
# Digest / size resolution
# ---------------------------------------------------------------------------


async def test_checksum_from_request_overrides_harbor():
    harbor = _make_harbor_mock(info=_make_harbor_info(digest="sha256:harbor-digest"))
    async with _svc(harbor=harbor) as (svc, _, mock_repo):
        await svc.register_model_version(
            "mymodel",
            _make_request(checksum="sha256:req-digest", version="v1"),
        )

    # repo.insert_artifact_version(artifact_id, version, harbor_ref, digest, ...)
    assert mock_repo.insert_artifact_version.call_args[0][3] == "sha256:req-digest"


async def test_digest_falls_back_to_harbor_when_not_in_request():
    harbor = _make_harbor_mock(info=_make_harbor_info(digest="sha256:from-harbor"))
    async with _svc(harbor=harbor) as (svc, _, mock_repo):
        await svc.register_model_version("mymodel", _make_request(version="v1"))

    assert mock_repo.insert_artifact_version.call_args[0][3] == "sha256:from-harbor"


# ---------------------------------------------------------------------------
# Domain-exception propagation
# ---------------------------------------------------------------------------


async def test_category_conflict_propagates():
    async with _svc(upsert_side_effect=ArtifactCategoryConflictError("conflict")) as (
        svc,
        _,
        __,
    ):
        with pytest.raises(ArtifactCategoryConflictError):
            await svc.register_model_version("mymodel", _make_request())


async def test_version_already_exists_propagates():
    async with _svc(insert_side_effect=VersionAlreadyExistsError("dup")) as (
        svc,
        _,
        __,
    ):
        with pytest.raises(VersionAlreadyExistsError):
            await svc.register_model_version("mymodel", _make_request())


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


async def test_response_shape():
    async with _svc() as (svc, _, __):
        result = await svc.register_model_version(
            "mymodel", _make_request(version="v7")
        )
    assert result.name == "mymodel"
    assert result.version == "v7"
    assert result.harbor_ref == _HARBOR_REF
    assert result.category == "model"
