"""Unit tests for app/routes/datasets.py."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import (
    get_dataset_deletion_service,
    get_dataset_query_service,
    get_dataset_registration_service,
    get_dataset_update_service,
)
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    HarborVerificationError,
    InvalidArtifactNameError,
    InvalidLineageReferenceError,
    LineageReferenceNotFoundError,
    VersionAlreadyExistsError,
)
from app.routes.datasets import router
from app.schemas.datasets import (
    ArtifactListResponse,
    ArtifactSummary,
    DatasetVersionListItem,
    GetDatasetMetadataResponse,
    GetDatasetVersionResponse,
    ListDatasetVersionsResponse,
    RegisterDatasetVersionResponse,
)
from app.schemas.models import LineageMetadata

_HARBOR_REF = "registry.example.com/proj/iris-tickets:2026-03"
_VALID_BODY = {
    "harbor_ref": _HARBOR_REF,
    "version": "v1",
    "metadata": {"description": "Iris tickets", "format": "parquet"},
}

_SUCCESS_RESPONSE = RegisterDatasetVersionResponse(
    name="iris-tickets",
    version="v1",
    harbor_ref=_HARBOR_REF,
    category="dataset",
)

_SUCCESS_GET_RESPONSE = GetDatasetVersionResponse(
    name="iris-tickets",
    version="v3",
    category="dataset",
    harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v3",
    size_bytes=345,
    checksum="sha256:def",
    created_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
    metadata={"format": "parquet"},
)

_SUCCESS_LIST_RESPONSE = ListDatasetVersionsResponse(
    versions=[
        DatasetVersionListItem(
            version="v3",
            harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v3",
            created_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
            size_bytes=345,
            checksum="sha256:def",
        ),
        DatasetVersionListItem(
            version="v2",
            harbor_ref="imgrepo.damit.hu/supernova/iris-tickets:v2",
            created_at=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
            size_bytes=300,
            checksum="sha256:aaa",
        ),
    ]
)

_SUCCESS_METADATA_RESPONSE = GetDatasetMetadataResponse(
    name="iris-tickets",
    category="dataset",
    description="Ticket dataset",
    training_config={"format": "parquet"},
    eval_metrics=None,
    lineage=LineageMetadata(
        parent_model=None,
        source_dataset="iris-tickets:v2",
        source_trainer="supernova-job-12345",
    ),
    created_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
    versions_count=2,
)


def _make_post_client(return_value=None, side_effect=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.register_dataset_version = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )
    mock_service.update_dataset_metadata = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )
    mock_service.delete_dataset_version = AsyncMock(side_effect=side_effect)
    mock_service.get_dataset_metadata = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )

    app.dependency_overrides[get_dataset_registration_service] = lambda: mock_service
    return TestClient(app, raise_server_exceptions=False)


def _make_get_client(
    get_return_value=None,
    get_side_effect=None,
    list_return_value=None,
    list_side_effect=None,
    metadata_return_value=None,
    metadata_side_effect=None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.get_dataset_version = AsyncMock(
        return_value=get_return_value,
        side_effect=get_side_effect,
    )
    mock_service.list_dataset_versions = AsyncMock(
        return_value=list_return_value,
        side_effect=list_side_effect,
    )
    mock_service.get_dataset_metadata = AsyncMock(
        return_value=metadata_return_value,
        side_effect=metadata_side_effect,
    )

    app.dependency_overrides[get_dataset_query_service] = lambda: mock_service
    return TestClient(app, raise_server_exceptions=False)


def _make_metadata_put_client(return_value=None, side_effect=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.update_dataset_metadata = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )
    app.dependency_overrides[get_dataset_update_service] = lambda: mock_service

    return TestClient(app, raise_server_exceptions=False)


def test_register_returns_201_on_success():
    client = _make_post_client(return_value=_SUCCESS_RESPONSE)
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "iris-tickets"
    assert data["version"] == "v1"
    assert data["category"] == "dataset"


def test_invalid_name_returns_422():
    client = _make_post_client(side_effect=InvalidArtifactNameError("bad name"))
    resp = client.post("/api/datasets/BAD/versions", json=_VALID_BODY)

    assert resp.status_code == 422


def test_harbor_not_found_returns_404():
    client = _make_post_client(side_effect=ArtifactNotFoundInHarborError("not found"))
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 404


def test_harbor_error_returns_502():
    client = _make_post_client(side_effect=HarborVerificationError("upstream error"))
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 502


@pytest.mark.parametrize(
    "exc",
    [
        ArtifactCategoryConflictError("conflict"),
        VersionAlreadyExistsError("duplicate"),
    ],
)
def test_conflict_returns_409(exc):
    client = _make_post_client(side_effect=exc)
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 409


def test_name_collision_with_existing_model_returns_409_with_detail():
    client = _make_post_client(
        side_effect=ArtifactCategoryConflictError(
            "Artifact 'iris-tickets' already exists as a 'model'; "
            "cannot register as a 'dataset'."
        )
    )
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 409
    assert "already exists as a 'model'" in resp.json()["detail"]
    assert "cannot register as a 'dataset'" in resp.json()["detail"]


def test_invalid_dataset_format_returns_422():
    client = _make_post_client(return_value=_SUCCESS_RESPONSE)
    resp = client.post(
        "/api/datasets/iris-tickets/versions",
        json={
            "harbor_ref": _HARBOR_REF,
            "metadata": {"format": "csv"},
        },
    )

    assert resp.status_code == 422


def test_get_dataset_version_returns_200_on_success():
    client = _make_get_client(get_return_value=_SUCCESS_GET_RESPONSE)
    resp = client.get("/api/datasets/iris-tickets/versions/v3")

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "iris-tickets"
    assert data["version"] == "v3"
    assert data["category"] == "dataset"
    assert data["harbor_ref"] == "imgrepo.damit.hu/supernova/iris-tickets:v3"


def test_get_dataset_version_latest_returns_200_on_success():
    client = _make_get_client(get_return_value=_SUCCESS_GET_RESPONSE)
    resp = client.get("/api/datasets/iris-tickets/versions/latest")

    assert resp.status_code == 200


@pytest.mark.parametrize(
    "exc",
    [
        DatasetNotFoundError("missing dataset"),
        DatasetVersionNotFoundError("missing version"),
    ],
)
def test_get_dataset_version_not_found_returns_404(exc):
    client = _make_get_client(get_side_effect=exc)
    resp = client.get("/api/datasets/iris-tickets/versions/v9")

    assert resp.status_code == 404


def test_get_dataset_version_invalid_name_returns_422():
    client = _make_get_client(get_side_effect=InvalidArtifactNameError("bad name"))
    resp = client.get("/api/datasets/BAD/versions/v1")

    assert resp.status_code == 422


def test_list_dataset_versions_returns_200_on_success():
    client = _make_get_client(list_return_value=_SUCCESS_LIST_RESPONSE)
    resp = client.get("/api/datasets/iris-tickets/versions")

    assert resp.status_code == 200
    data = resp.json()
    assert data["versions"][0]["version"] == "v3"
    assert data["versions"][0]["harbor_ref"] == (
        "imgrepo.damit.hu/supernova/iris-tickets:v3"
    )
    assert data["versions"][0]["size_bytes"] == 345
    assert data["versions"][0]["checksum"] == "sha256:def"
    assert data["versions"][1]["version"] == "v2"
    assert "training_config" not in data["versions"][0]
    assert "eval_metrics" not in data["versions"][0]


def test_list_dataset_versions_not_found_returns_404():
    client = _make_get_client(
        list_side_effect=DatasetNotFoundError("missing dataset"),
    )
    resp = client.get("/api/datasets/iris-tickets/versions")

    assert resp.status_code == 404


def test_list_dataset_versions_invalid_name_returns_422():
    client = _make_get_client(
        list_side_effect=InvalidArtifactNameError("bad name"),
    )
    resp = client.get("/api/datasets/BAD/versions")

    assert resp.status_code == 422


def test_get_dataset_metadata_returns_200_on_success():
    client = _make_get_client(metadata_return_value=_SUCCESS_METADATA_RESPONSE)
    resp = client.get("/api/datasets/iris-tickets")

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "iris-tickets"
    assert data["lineage"]["source_trainer"] == "supernova-job-12345"


def test_get_dataset_metadata_not_found_returns_404():
    client = _make_get_client(
        metadata_side_effect=DatasetNotFoundError("missing dataset"),
    )
    resp = client.get("/api/datasets/iris-tickets")

    assert resp.status_code == 404


def test_get_dataset_metadata_invalid_name_returns_422():
    client = _make_get_client(
        metadata_side_effect=InvalidArtifactNameError("bad name"),
    )
    resp = client.get("/api/datasets/BAD")

    assert resp.status_code == 422


def test_put_dataset_metadata_returns_200_on_success():
    client = _make_metadata_put_client(return_value=_SUCCESS_METADATA_RESPONSE)
    resp = client.put(
        "/api/datasets/iris-tickets",
        json={
            "description": "Ticket dataset",
            "training_config": {"format": "parquet"},
            "lineage": {
                "source_dataset": "iris-tickets:v2",
                "source_trainer": "supernova-job-12345",
            },
        },
    )

    assert resp.status_code == 200
    assert resp.json()["description"] == "Ticket dataset"


def test_put_dataset_metadata_not_found_returns_404():
    client = _make_metadata_put_client(
        side_effect=LineageReferenceNotFoundError("missing reference"),
    )
    resp = client.put(
        "/api/datasets/iris-tickets",
        json={"lineage": {"source_dataset": "missing:v1"}},
    )

    assert resp.status_code == 404


def test_put_dataset_metadata_invalid_lineage_returns_422():
    client = _make_metadata_put_client(
        side_effect=InvalidLineageReferenceError("bad lineage"),
    )
    resp = client.put(
        "/api/datasets/iris-tickets",
        json={"lineage": {"source_dataset": "bad"}},
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/datasets/{name}/versions/{version}
# ---------------------------------------------------------------------------


def _make_delete_client(side_effect=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.delete_dataset_version = AsyncMock(
        return_value=None,
        side_effect=side_effect,
    )
    app.dependency_overrides[get_dataset_deletion_service] = lambda: mock_service

    return TestClient(app, raise_server_exceptions=False)


def test_delete_dataset_version_returns_204_on_success():
    client = _make_delete_client()
    resp = client.delete("/api/datasets/iris-tickets/versions/v3")

    assert resp.status_code == 204
    assert resp.content == b""


@pytest.mark.parametrize(
    "exc",
    [
        DatasetNotFoundError("missing dataset"),
        DatasetVersionNotFoundError("missing version"),
    ],
)
def test_delete_dataset_version_not_found_returns_404(exc):
    client = _make_delete_client(side_effect=exc)
    resp = client.delete("/api/datasets/iris-tickets/versions/v99")

    assert resp.status_code == 404


def test_delete_dataset_version_invalid_name_returns_422():
    client = _make_delete_client(side_effect=InvalidArtifactNameError("bad name"))
    resp = client.delete("/api/datasets/BAD/versions/v1")

    assert resp.status_code == 422


def test_delete_dataset_version_latest_alias_returns_422():
    client = _make_delete_client(
        side_effect=InvalidArtifactNameError("'latest' is a reserved alias")
    )
    resp = client.delete("/api/datasets/iris-tickets/versions/latest")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/datasets  (list datasets)
# ---------------------------------------------------------------------------

_LIST_DATASETS_RESPONSE = ArtifactListResponse[ArtifactSummary](
    total=2,
    items=[
        ArtifactSummary(
            name="iris-tickets",
            category="dataset",
            description="Iris tickets export",
            versions_count=4,
            latest_version="v4",
            created_at=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        ),
        ArtifactSummary(
            name="cifar-10",
            category="dataset",
            description=None,
            versions_count=1,
            latest_version="v1",
            created_at=datetime(2026, 3, 1, 8, 0, tzinfo=UTC),
        ),
    ],
)


def _make_list_datasets_client(return_value=None, side_effect=None) -> TestClient:
    """Return a TestClient with DatasetQueryService.list_datasets mocked."""
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.list_datasets = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )
    app.dependency_overrides[get_dataset_query_service] = lambda: mock_service
    return TestClient(app, raise_server_exceptions=False)


def test_list_datasets_returns_200_with_items():
    client = _make_list_datasets_client(return_value=_LIST_DATASETS_RESPONSE)
    resp = client.get("/api/datasets")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["name"] == "iris-tickets"
    assert data["items"][0]["versions_count"] == 4
    assert data["items"][0]["latest_version"] == "v4"
    assert data["items"][1]["name"] == "cifar-10"
    assert data["items"][1]["description"] is None


def test_list_datasets_returns_200_empty():
    empty_response = ArtifactListResponse[ArtifactSummary](total=0, items=[])
    client = _make_list_datasets_client(return_value=empty_response)
    resp = client.get("/api/datasets")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_list_datasets_passes_search_limit_offset():
    mock_service = MagicMock()
    mock_service.list_datasets = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dataset_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/datasets?search=cifar&limit=20&offset=10")

    assert resp.status_code == 200
    mock_service.list_datasets.assert_awaited_once_with(
        search="cifar", limit=20, offset=10
    )


def test_list_datasets_default_pagination():
    mock_service = MagicMock()
    mock_service.list_datasets = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dataset_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/datasets")

    assert resp.status_code == 200
    mock_service.list_datasets.assert_awaited_once_with(search=None, limit=50, offset=0)


def test_list_datasets_page_based_pagination():
    mock_service = MagicMock()
    mock_service.list_datasets = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dataset_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/datasets?page=2&page_size=15")

    assert resp.status_code == 200
    mock_service.list_datasets.assert_awaited_once_with(
        search=None, limit=15, offset=15
    )


def test_list_datasets_trailing_slash():
    mock_service = MagicMock()
    mock_service.list_datasets = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dataset_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/datasets/", follow_redirects=False)

    assert resp.status_code in (200, 307, 308)


def test_list_datasets_invalid_limit_returns_422():
    client = _make_list_datasets_client(return_value=_LIST_DATASETS_RESPONSE)
    resp = client.get("/api/datasets?limit=0")

    assert resp.status_code == 422


def test_list_datasets_limit_exceeds_max_returns_422():
    client = _make_list_datasets_client(return_value=_LIST_DATASETS_RESPONSE)
    resp = client.get("/api/datasets?limit=1001")

    assert resp.status_code == 422


def test_list_datasets_negative_offset_returns_422():
    client = _make_list_datasets_client(return_value=_LIST_DATASETS_RESPONSE)
    resp = client.get("/api/datasets?offset=-1")

    assert resp.status_code == 422
