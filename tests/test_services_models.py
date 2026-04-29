"""Unit tests for app/services/models.py services.

All database and Harbor I/O is replaced with unittest.mock fakes so these
tests run without a live database or Harbor instance.

HarborClient is injected directly into the constructor (no patching needed).
ArtifactRepository is intercepted via patch so the session mock is never
exercised — the service builds the repo internally and the test swaps it out.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    HarborVerificationError,
    InvalidArtifactNameError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    VersionAlreadyExistsError,
)
from app.harbor import (
    ArtifactNotFoundError,
    HarborAPIError,
    HarborAuthError,
    HarborConnectionError,
)
from app.repositories.artifacts import ArtifactVersionRecord
from app.schemas.models import RegisterModelVersionRequest
from app.services.models import (
    DatasetDeletionService,
    DatasetQueryService,
    ModelDeletionService,
    ModelQueryService,
    ModelRegistrationService,
)

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
    """Return (service, active patch context) with ArtifactRepository swapped."""
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

    with patch("app.services.models.ArtifactRepository", return_value=mock_repo):
        svc = ModelRegistrationService(harbor=mock_harbor, session=AsyncMock())
        yield svc, mock_harbor, mock_repo


@asynccontextmanager
async def _query_svc(*, repo_overrides=None):
    mock_repo = _make_repo_mock()
    if repo_overrides:
        for attr, val in repo_overrides.items():
            setattr(mock_repo, attr, val)

    with patch("app.services.models.ArtifactRepository", return_value=mock_repo):
        svc = ModelQueryService(session=AsyncMock())
        yield svc, mock_repo


@asynccontextmanager
async def _dataset_query_svc(*, repo_overrides=None):
    mock_repo = _make_repo_mock()
    if repo_overrides:
        for attr, val in repo_overrides.items():
            setattr(mock_repo, attr, val)

    with patch("app.services.models.ArtifactRepository", return_value=mock_repo):
        svc = DatasetQueryService(session=AsyncMock())
        yield svc, mock_repo


@asynccontextmanager
async def _model_delete_svc(*, repo_overrides=None):
    mock_repo = _make_repo_mock()
    if repo_overrides:
        for attr, val in repo_overrides.items():
            setattr(mock_repo, attr, val)

    with patch("app.services.models.ArtifactRepository", return_value=mock_repo):
        svc = ModelDeletionService(session=AsyncMock())
        yield svc, mock_repo


@asynccontextmanager
async def _dataset_delete_svc(*, repo_overrides=None):
    mock_repo = _make_repo_mock()
    if repo_overrides:
        for attr, val in repo_overrides.items():
            setattr(mock_repo, attr, val)

    with patch("app.services.models.ArtifactRepository", return_value=mock_repo):
        svc = DatasetDeletionService(session=AsyncMock())
        yield svc, mock_repo


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


@pytest.mark.parametrize("reserved", ["latest", "Latest", "LATEST"])
async def test_register_rejects_latest_as_version(reserved: str):
    async with _svc() as (svc, _, __):
        with pytest.raises(InvalidArtifactNameError, match="reserved"):
            await svc.register_model_version("mymodel", _make_request(version=reserved))


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


# ---------------------------------------------------------------------------
# Retrieval logic (GET /models/{name}/versions/{version})
# ---------------------------------------------------------------------------


async def test_get_model_version_success():
    record = ArtifactVersionRecord(
        name="mymodel",
        version="v3",
        category="model",
        harbor_ref="imgrepo.damit.hu/supernova/mymodel:v3",
        size_bytes=2048,
        checksum="sha256:abc",
        created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
        metadata={"k": "v"},
    )

    async with _query_svc(
        repo_overrides={"get_model_version": AsyncMock(return_value=record)}
    ) as (svc, __):
        result = await svc.get_model_version("mymodel", "v3")

    assert result.name == "mymodel"
    assert result.version == "v3"
    assert result.category == "model"
    assert result.harbor_ref == "imgrepo.damit.hu/supernova/mymodel:v3"
    assert result.size_bytes == 2048
    assert result.checksum == "sha256:abc"
    assert result.metadata == {"k": "v"}


async def test_get_model_version_latest_alias_passed_through():
    record = ArtifactVersionRecord(
        name="mymodel",
        version="v7",
        category="model",
        harbor_ref="imgrepo.damit.hu/supernova/mymodel:v7",
        size_bytes=None,
        checksum=None,
        created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
        metadata={},
    )
    get_mock = AsyncMock(return_value=record)

    async with _query_svc(repo_overrides={"get_model_version": get_mock}) as (
        svc,
        __,
    ):
        result = await svc.get_model_version("mymodel", "latest")

    assert result.version == "v7"
    get_mock.assert_awaited_once_with(name="mymodel", version="latest")


async def test_get_model_version_invalid_name_raises():
    async with _query_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.get_model_version("BAD", "v1")


@pytest.mark.parametrize(
    "exc",
    [
        ModelNotFoundError("missing model"),
        ModelVersionNotFoundError("missing version"),
    ],
)
async def test_get_model_version_not_found_propagates(exc):
    async with _query_svc(
        repo_overrides={"get_model_version": AsyncMock(side_effect=exc)}
    ) as (svc, __):
        with pytest.raises(type(exc)):
            await svc.get_model_version("mymodel", "v99")


async def test_list_model_versions_success_extracts_top_level_metadata():
    records = [
        ArtifactVersionRecord(
            name="mymodel",
            version="v3",
            category="model",
            harbor_ref="imgrepo.damit.hu/supernova/mymodel:v3",
            size_bytes=2048,
            checksum="sha256:abc",
            created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
            metadata={
                "training_config": {"epochs": 3},
                "eval_metrics": {"accuracy": 0.98},
            },
        ),
        ArtifactVersionRecord(
            name="mymodel",
            version="v2",
            category="model",
            harbor_ref="imgrepo.damit.hu/supernova/mymodel:v2",
            size_bytes=1024,
            checksum="sha256:def",
            created_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
            metadata={},
        ),
    ]

    async with _query_svc(
        repo_overrides={"list_model_versions": AsyncMock(return_value=records)}
    ) as (svc, __):
        result = await svc.list_model_versions("mymodel")

    assert len(result.versions) == 2
    assert result.versions[0].version == "v3"
    assert result.versions[0].training_config == {"epochs": 3}
    assert result.versions[0].eval_metrics == {"accuracy": 0.98}
    assert result.versions[1].version == "v2"
    assert result.versions[1].training_config is None
    assert result.versions[1].eval_metrics is None


async def test_list_model_versions_invalid_name_raises():
    async with _query_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.list_model_versions("BAD")


async def test_list_model_versions_not_found_propagates():
    async with _query_svc(
        repo_overrides={
            "list_model_versions": AsyncMock(
                side_effect=ModelNotFoundError("missing model")
            )
        }
    ) as (svc, __):
        with pytest.raises(ModelNotFoundError):
            await svc.list_model_versions("mymodel")


async def test_get_dataset_version_success():
    record = ArtifactVersionRecord(
        name="iris-tickets",
        version="v3",
        category="dataset",
        harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v3",
        size_bytes=2048,
        checksum="sha256:def",
        created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
        metadata={"format": "parquet"},
    )

    async with _dataset_query_svc(
        repo_overrides={"get_dataset_version": AsyncMock(return_value=record)}
    ) as (svc, __):
        result = await svc.get_dataset_version("iris-tickets", "v3")

    assert result.name == "iris-tickets"
    assert result.version == "v3"
    assert result.category == "dataset"
    assert result.harbor_ref == "imgrepo.damit.hu/supernova/iris-tickets:v3"
    assert result.size_bytes == 2048
    assert result.checksum == "sha256:def"
    assert result.metadata == {"format": "parquet"}


async def test_get_dataset_version_latest_alias_passed_through():
    record = ArtifactVersionRecord(
        name="iris-tickets",
        version="v7",
        category="dataset",
        harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v7",
        size_bytes=None,
        checksum=None,
        created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
        metadata={},
    )
    get_mock = AsyncMock(return_value=record)

    async with _dataset_query_svc(repo_overrides={"get_dataset_version": get_mock}) as (
        svc,
        __,
    ):
        result = await svc.get_dataset_version("iris-tickets", "latest")

    assert result.version == "v7"
    get_mock.assert_awaited_once_with(name="iris-tickets", version="latest")


async def test_get_dataset_version_invalid_name_raises():
    async with _dataset_query_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.get_dataset_version("BAD", "v1")


@pytest.mark.parametrize(
    "exc",
    [
        DatasetNotFoundError("missing dataset"),
        DatasetVersionNotFoundError("missing version"),
    ],
)
async def test_get_dataset_version_not_found_propagates(exc):
    async with _dataset_query_svc(
        repo_overrides={"get_dataset_version": AsyncMock(side_effect=exc)}
    ) as (svc, __):
        with pytest.raises(type(exc)):
            await svc.get_dataset_version("iris-tickets", "v99")


async def test_list_dataset_versions_success_returns_core_fields():
    records = [
        ArtifactVersionRecord(
            name="iris-tickets",
            version="v4",
            category="dataset",
            harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v4",
            size_bytes=4096,
            checksum="sha256:ghi",
            created_at=datetime(2026, 4, 3, 10, 0, tzinfo=timezone.utc),
            metadata={"description": "2026-03 export", "format": "parquet"},
        ),
        ArtifactVersionRecord(
            name="iris-tickets",
            version="v3",
            category="dataset",
            harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v3",
            size_bytes=2048,
            checksum="sha256:def",
            created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
            metadata={},
        ),
    ]

    async with _dataset_query_svc(
        repo_overrides={"list_dataset_versions": AsyncMock(return_value=records)}
    ) as (svc, __):
        result = await svc.list_dataset_versions("iris-tickets")

    assert len(result.versions) == 2
    assert result.versions[0].version == "v4"
    assert result.versions[0].harbor_ref == (
        "imgrepo.damit.hu/supernova/iris-tickets:v4"
    )
    assert result.versions[0].size_bytes == 4096
    assert result.versions[0].checksum == "sha256:ghi"
    assert result.versions[1].version == "v3"
    first = result.versions[0].model_dump()
    assert "training_config" not in first
    assert "eval_metrics" not in first


async def test_list_dataset_versions_invalid_name_raises():
    async with _dataset_query_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.list_dataset_versions("BAD")


async def test_list_dataset_versions_not_found_propagates():
    async with _dataset_query_svc(
        repo_overrides={
            "list_dataset_versions": AsyncMock(
                side_effect=DatasetNotFoundError("missing dataset")
            )
        }
    ) as (svc, __):
        with pytest.raises(DatasetNotFoundError):
            await svc.list_dataset_versions("iris-tickets")


# ---------------------------------------------------------------------------
# Delete logic (DELETE /models/{name}/versions/{version})
# ---------------------------------------------------------------------------


async def test_delete_model_version_success_delegates_to_repo():
    delete_mock = AsyncMock(return_value=None)
    async with _model_delete_svc(
        repo_overrides={"delete_model_version": delete_mock}
    ) as (svc, __):
        await svc.delete_model_version("mymodel", "v3")

    delete_mock.assert_awaited_once_with(name="mymodel", version="v3")


async def test_delete_model_version_invalid_name_raises():
    async with _model_delete_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.delete_model_version("BAD", "v1")


@pytest.mark.parametrize("reserved", ["latest", "Latest", "LATEST"])
async def test_delete_model_version_rejects_latest_alias(reserved):
    async with _model_delete_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError, match="reserved"):
            await svc.delete_model_version("mymodel", reserved)


@pytest.mark.parametrize(
    "exc",
    [
        ModelNotFoundError("missing model"),
        ModelVersionNotFoundError("missing version"),
    ],
)
async def test_delete_model_version_not_found_propagates(exc):
    async with _model_delete_svc(
        repo_overrides={"delete_model_version": AsyncMock(side_effect=exc)}
    ) as (svc, __):
        with pytest.raises(type(exc)):
            await svc.delete_model_version("mymodel", "v99")


async def test_delete_dataset_version_success_delegates_to_repo():
    delete_mock = AsyncMock(return_value=None)
    async with _dataset_delete_svc(
        repo_overrides={"delete_dataset_version": delete_mock}
    ) as (svc, __):
        await svc.delete_dataset_version("iris-tickets", "v3")

    delete_mock.assert_awaited_once_with(name="iris-tickets", version="v3")


async def test_delete_dataset_version_invalid_name_raises():
    async with _dataset_delete_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError):
            await svc.delete_dataset_version("BAD", "v1")


@pytest.mark.parametrize("reserved", ["latest", "Latest", "LATEST"])
async def test_delete_dataset_version_rejects_latest_alias(reserved):
    async with _dataset_delete_svc() as (svc, __):
        with pytest.raises(InvalidArtifactNameError, match="reserved"):
            await svc.delete_dataset_version("iris-tickets", reserved)


@pytest.mark.parametrize(
    "exc",
    [
        DatasetNotFoundError("missing dataset"),
        DatasetVersionNotFoundError("missing version"),
    ],
)
async def test_delete_dataset_version_not_found_propagates(exc):
    async with _dataset_delete_svc(
        repo_overrides={"delete_dataset_version": AsyncMock(side_effect=exc)}
    ) as (svc, __):
        with pytest.raises(type(exc)):
            await svc.delete_dataset_version("iris-tickets", "v99")


# ---------------------------------------------------------------------------
# list_models (ModelQueryService)
# ---------------------------------------------------------------------------

from app.repositories.artifacts import ArtifactListRecord  # noqa: E402


def _make_list_record(
    *,
    name: str = "my-model",
    category: str = "model",
    description: str | None = None,
    versions_count: int = 2,
    latest_version: str | None = "v2",
    created_at: datetime = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
) -> ArtifactListRecord:
    return ArtifactListRecord(
        name=name,
        category=category,
        description=description,
        versions_count=versions_count,
        latest_version=latest_version,
        created_at=created_at,
    )


async def test_list_models_returns_paginated_response():
    records = [
        _make_list_record(name="alpha", versions_count=3, latest_version="v3"),
        _make_list_record(name="beta", versions_count=1, latest_version="v1"),
    ]
    async with _query_svc(
        repo_overrides={
            "list_artifacts_by_category": AsyncMock(return_value=(2, records)),
        }
    ) as (svc, mock_repo):
        result = await svc.list_models()

    assert result.total == 2
    assert len(result.items) == 2
    assert result.items[0].name == "alpha"
    assert result.items[0].versions_count == 3
    assert result.items[0].latest_version == "v3"
    assert result.items[1].name == "beta"


async def test_list_models_calls_repo_with_correct_category_and_filters():
    list_mock = AsyncMock(return_value=(0, []))
    async with _query_svc(
        repo_overrides={
            "list_artifacts_by_category": list_mock,
        }
    ) as (svc, __):
        await svc.list_models(search="iris", limit=10, offset=5)

    list_mock.assert_awaited_once_with("model", search="iris", limit=10, offset=5)


async def test_list_models_normalizes_search_whitespace():
    list_mock = AsyncMock(return_value=(0, []))
    async with _query_svc(
        repo_overrides={
            "list_artifacts_by_category": list_mock,
        }
    ) as (svc, __):
        await svc.list_models(search="  \t  ")

    list_mock.assert_awaited_once_with("model", search=None, limit=50, offset=0)


async def test_list_models_strips_search_edges():
    list_mock = AsyncMock(return_value=(0, []))
    async with _query_svc(
        repo_overrides={
            "list_artifacts_by_category": list_mock,
        }
    ) as (svc, __):
        await svc.list_models(search="  iris  ")

    list_mock.assert_awaited_once_with("model", search="iris", limit=50, offset=0)


async def test_list_models_empty_returns_zero_total():
    async with _query_svc(
        repo_overrides={
            "list_artifacts_by_category": AsyncMock(return_value=(0, [])),
        }
    ) as (svc, __):
        result = await svc.list_models()

    assert result.total == 0
    assert result.items == []


async def test_list_models_maps_description_and_created_at():
    ts = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    records = [
        _make_list_record(
            name="described-model", description="A great model", created_at=ts
        )
    ]
    async with _query_svc(
        repo_overrides={
            "list_artifacts_by_category": AsyncMock(return_value=(1, records)),
        }
    ) as (svc, __):
        result = await svc.list_models()

    item = result.items[0]
    assert item.description == "A great model"
    assert item.created_at == ts
    assert item.category == "model"


# ---------------------------------------------------------------------------
# list_datasets (DatasetQueryService)
# ---------------------------------------------------------------------------


async def test_list_datasets_returns_paginated_response():
    records = [
        _make_list_record(
            name="iris-tickets",
            category="dataset",
            versions_count=4,
            latest_version="v4",
        ),
        _make_list_record(
            name="cifar-10", category="dataset", versions_count=2, latest_version="v2"
        ),
    ]
    async with _dataset_query_svc(
        repo_overrides={
            "list_artifacts_by_category": AsyncMock(return_value=(2, records)),
        }
    ) as (svc, __):
        result = await svc.list_datasets()

    assert result.total == 2
    assert len(result.items) == 2
    assert result.items[0].name == "iris-tickets"
    assert result.items[0].versions_count == 4
    assert result.items[0].latest_version == "v4"
    assert result.items[1].name == "cifar-10"


async def test_list_datasets_calls_repo_with_correct_category_and_filters():
    list_mock = AsyncMock(return_value=(0, []))
    async with _dataset_query_svc(
        repo_overrides={
            "list_artifacts_by_category": list_mock,
        }
    ) as (svc, __):
        await svc.list_datasets(search="cifar", limit=20, offset=10)

    list_mock.assert_awaited_once_with("dataset", search="cifar", limit=20, offset=10)


async def test_list_datasets_empty_returns_zero_total():
    async with _dataset_query_svc(
        repo_overrides={
            "list_artifacts_by_category": AsyncMock(return_value=(0, [])),
        }
    ) as (svc, __):
        result = await svc.list_datasets()

    assert result.total == 0
    assert result.items == []


async def test_list_datasets_maps_description_and_created_at():
    ts = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    records = [
        _make_list_record(
            name="annotated-data",
            category="dataset",
            description="Annotated parquet dataset",
            created_at=ts,
        )
    ]
    async with _dataset_query_svc(
        repo_overrides={
            "list_artifacts_by_category": AsyncMock(return_value=(1, records)),
        }
    ) as (svc, __):
        result = await svc.list_datasets()

    item = result.items[0]
    assert item.description == "Annotated parquet dataset"
    assert item.created_at == ts
    assert item.category == "dataset"
