"""Unit tests for app/routes/models.py.

Uses FastAPI TestClient with ModelRegistrationService replaced via
app.dependency_overrides so HTTP-status mapping for every domain exception is
verified without a DB, Harbor, or singleton dependency.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import (
    get_model_deletion_service,
    get_model_query_service,
    get_model_registration_service,
)
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    VersionAlreadyExistsError,
)
from app.routes.models import router
from app.schemas.artifacts import ArtifactListResponse, ArtifactSummary
from app.schemas.models import (
    GetModelVersionResponse,
    ListModelVersionsResponse,
    ModelVersionListItem,
    RegisterModelVersionResponse,
)

_HARBOR_REF = "registry.example.com/proj/my-model:v1"
_VALID_BODY = {"harbor_ref": _HARBOR_REF, "version": "v1"}

_SUCCESS_RESPONSE = RegisterModelVersionResponse(
    name="mymodel",
    version="v1",
    harbor_ref=_HARBOR_REF,
    category="model",
)

_SUCCESS_GET_RESPONSE = GetModelVersionResponse(
    name="mymodel",
    version="v3",
    category="model",
    harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
    size_bytes=123,
    checksum="sha256:abc",
    created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
    metadata={"trainer": "etalon"},
)

_SUCCESS_LIST_RESPONSE = ListModelVersionsResponse(
    versions=[
        ModelVersionListItem(
            version="v3",
            harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
            created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
            size_bytes=123,
            checksum="sha256:abc",
            training_config={"epochs": 3},
            eval_metrics={"accuracy": 0.98},
        ),
        ModelVersionListItem(
            version="v2",
            harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v2",
            created_at=datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
            size_bytes=120,
            checksum="sha256:def",
            training_config=None,
            eval_metrics=None,
        ),
    ]
)


def _make_post_client(return_value=None, side_effect=None) -> TestClient:
    """Return a TestClient with ``ModelRegistrationService`` mocked for POST tests."""
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.register_model_version = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )
    app.dependency_overrides[get_model_registration_service] = lambda: mock_service

    return TestClient(app, raise_server_exceptions=False)


def _make_get_client(
    get_return_value=None,
    get_side_effect=None,
    list_return_value=None,
    list_side_effect=None,
) -> TestClient:
    """Return a TestClient with ``ModelQueryService`` mocked for GET tests."""
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.get_model_version = AsyncMock(
        return_value=get_return_value,
        side_effect=get_side_effect,
    )
    mock_service.list_model_versions = AsyncMock(
        return_value=list_return_value,
        side_effect=list_side_effect,
    )
    app.dependency_overrides[get_model_query_service] = lambda: mock_service

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 201 success
# ---------------------------------------------------------------------------


def test_register_returns_201_on_success():
    client = _make_post_client(return_value=_SUCCESS_RESPONSE)
    resp = client.post("/api/models/mymodel/versions", json=_VALID_BODY)

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "mymodel"
    assert data["version"] == "v1"
    assert data["category"] == "model"


# ---------------------------------------------------------------------------
# 422 — invalid artifact name
# ---------------------------------------------------------------------------


def test_invalid_name_returns_422():
    client = _make_post_client(side_effect=InvalidArtifactNameError("bad name"))
    resp = client.post("/api/models/BAD/versions", json=_VALID_BODY)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 404 — artifact not found in Harbor
# ---------------------------------------------------------------------------


def test_harbor_not_found_returns_404():
    client = _make_post_client(side_effect=ArtifactNotFoundInHarborError("not found"))
    resp = client.post("/api/models/mymodel/versions", json=_VALID_BODY)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 502 — Harbor verification error
# ---------------------------------------------------------------------------


def test_harbor_error_returns_502():
    client = _make_post_client(side_effect=HarborVerificationError("upstream error"))
    resp = client.post("/api/models/mymodel/versions", json=_VALID_BODY)

    assert resp.status_code == 502


# ---------------------------------------------------------------------------
# 409 — category conflict or version duplicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ArtifactCategoryConflictError("conflict"),
        VersionAlreadyExistsError("duplicate"),
    ],
)
def test_conflict_returns_409(exc):
    client = _make_post_client(side_effect=exc)
    resp = client.post("/api/models/mymodel/versions", json=_VALID_BODY)

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 422 — Pydantic validation: missing required field
# ---------------------------------------------------------------------------


def test_missing_harbor_ref_returns_422():
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.register_model_version = AsyncMock()
    app.dependency_overrides[get_model_registration_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/api/models/mymodel/versions", json={})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/models/{name}/versions/{version}
# ---------------------------------------------------------------------------


def test_get_model_version_returns_200_on_success():
    client = _make_get_client(get_return_value=_SUCCESS_GET_RESPONSE)
    resp = client.get("/api/models/mymodel/versions/v3")

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "mymodel"
    assert data["version"] == "v3"
    assert data["category"] == "model"
    assert data["harbor_ref"] == "imgrepo.damit.hu/supernova/iris-osl:v3"


@pytest.mark.parametrize(
    "exc",
    [
        ModelNotFoundError("missing model"),
        ModelVersionNotFoundError("missing version"),
    ],
)
def test_get_model_version_not_found_returns_404(exc):
    client = _make_get_client(get_side_effect=exc)
    resp = client.get("/api/models/mymodel/versions/v9")

    assert resp.status_code == 404


def test_get_model_version_invalid_name_returns_422():
    client = _make_get_client(get_side_effect=InvalidArtifactNameError("bad name"))
    resp = client.get("/api/models/BAD/versions/v1")

    assert resp.status_code == 422


def test_list_model_versions_returns_200_on_success():
    client = _make_get_client(list_return_value=_SUCCESS_LIST_RESPONSE)
    resp = client.get("/api/models/mymodel/versions")

    assert resp.status_code == 200
    data = resp.json()
    assert data["versions"][0]["version"] == "v3"
    assert data["versions"][0]["training_config"] == {"epochs": 3}
    assert data["versions"][1]["version"] == "v2"
    assert data["versions"][1]["eval_metrics"] is None


def test_list_model_versions_not_found_returns_404():
    client = _make_get_client(list_side_effect=ModelNotFoundError("missing model"))
    resp = client.get("/api/models/mymodel/versions")

    assert resp.status_code == 404


def test_list_model_versions_invalid_name_returns_422():
    client = _make_get_client(
        list_side_effect=InvalidArtifactNameError("bad name"),
    )
    resp = client.get("/api/models/BAD/versions")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /api/models/{name}/versions/{version}
# ---------------------------------------------------------------------------


def _make_delete_client(side_effect=None) -> TestClient:
    """Return a TestClient with ``ModelDeletionService`` mocked for DELETE tests."""
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.delete_model_version = AsyncMock(
        return_value=None,
        side_effect=side_effect,
    )
    app.dependency_overrides[get_model_deletion_service] = lambda: mock_service

    return TestClient(app, raise_server_exceptions=False)


def test_delete_model_version_returns_204_on_success():
    client = _make_delete_client()
    resp = client.delete("/api/models/mymodel/versions/v3")

    assert resp.status_code == 204
    assert resp.content == b""


@pytest.mark.parametrize(
    "exc",
    [
        ModelNotFoundError("missing model"),
        ModelVersionNotFoundError("missing version"),
    ],
)
def test_delete_model_version_not_found_returns_404(exc):
    client = _make_delete_client(side_effect=exc)
    resp = client.delete("/api/models/mymodel/versions/v99")

    assert resp.status_code == 404


def test_delete_model_version_invalid_name_returns_422():
    client = _make_delete_client(side_effect=InvalidArtifactNameError("bad name"))
    resp = client.delete("/api/models/BAD/versions/v1")

    assert resp.status_code == 422


def test_delete_model_version_latest_alias_returns_422():
    client = _make_delete_client(
        side_effect=InvalidArtifactNameError("'latest' is a reserved alias")
    )
    resp = client.delete("/api/models/mymodel/versions/latest")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/models  (list models)
# ---------------------------------------------------------------------------

_LIST_MODELS_RESPONSE = ArtifactListResponse[ArtifactSummary](
    total=2,
    items=[
        ArtifactSummary(
            name="iris-osl",
            category="model",
            description="Iris OSL model",
            versions_count=3,
            latest_version="v3",
            created_at=datetime(2026, 4, 2, 10, 0, tzinfo=timezone.utc),
        ),
        ArtifactSummary(
            name="bert-base",
            category="model",
            description=None,
            versions_count=1,
            latest_version="v1",
            created_at=datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
        ),
    ],
)


def _make_list_models_client(return_value=None, side_effect=None) -> TestClient:
    """Return a TestClient with ModelQueryService.list_models mocked."""
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.list_models = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )
    app.dependency_overrides[get_model_query_service] = lambda: mock_service
    return TestClient(app, raise_server_exceptions=False)


def test_list_models_returns_200_with_items():
    client = _make_list_models_client(return_value=_LIST_MODELS_RESPONSE)
    resp = client.get("/api/models")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["name"] == "iris-osl"
    assert data["items"][0]["versions_count"] == 3
    assert data["items"][0]["latest_version"] == "v3"
    assert data["items"][1]["name"] == "bert-base"
    assert data["items"][1]["description"] is None


def test_list_models_returns_200_empty():
    empty_response = ArtifactListResponse[ArtifactSummary](total=0, items=[])
    client = _make_list_models_client(return_value=empty_response)
    resp = client.get("/api/models")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


def test_list_models_passes_search_limit_offset():
    mock_service = MagicMock()
    mock_service.list_models = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_model_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/models?search=iris&limit=10&offset=5")

    assert resp.status_code == 200
    mock_service.list_models.assert_awaited_once_with(search="iris", limit=10, offset=5)


def test_list_models_default_pagination():
    mock_service = MagicMock()
    mock_service.list_models = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_model_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/models")

    assert resp.status_code == 200
    mock_service.list_models.assert_awaited_once_with(search=None, limit=50, offset=0)


def test_list_models_page_based_pagination():
    mock_service = MagicMock()
    mock_service.list_models = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_model_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/models?page=2&page_size=10")

    assert resp.status_code == 200
    mock_service.list_models.assert_awaited_once_with(search=None, limit=10, offset=10)


def test_list_models_page_only_uses_default_page_size():
    mock_service = MagicMock()
    mock_service.list_models = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_model_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/models?page=3")

    assert resp.status_code == 200
    mock_service.list_models.assert_awaited_once_with(search=None, limit=50, offset=100)


def test_list_models_trailing_slash():
    """Starlette redirects collection URLs with a trailing slash by default."""
    mock_service = MagicMock()
    mock_service.list_models = AsyncMock(
        return_value=ArtifactListResponse[ArtifactSummary](total=0, items=[])
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_model_query_service] = lambda: mock_service

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/models/", follow_redirects=False)

    assert resp.status_code in (200, 307, 308)


def test_list_models_invalid_limit_returns_422():
    client = _make_list_models_client(return_value=_LIST_MODELS_RESPONSE)
    resp = client.get("/api/models?limit=0")

    assert resp.status_code == 422


def test_list_models_limit_exceeds_max_returns_422():
    client = _make_list_models_client(return_value=_LIST_MODELS_RESPONSE)
    resp = client.get("/api/models?limit=1001")

    assert resp.status_code == 422


def test_list_models_negative_offset_returns_422():
    client = _make_list_models_client(return_value=_LIST_MODELS_RESPONSE)
    resp = client.get("/api/models?offset=-1")

    assert resp.status_code == 422
