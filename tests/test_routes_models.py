"""Unit tests for app/routes/models.py.

Uses FastAPI TestClient with ModelRegistrationService replaced via
app.dependency_overrides so HTTP-status mapping for every domain exception is
verified without a DB, Harbor, or singleton dependency.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_model_registration_service
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    VersionAlreadyExistsError,
)
from app.routes.models import router
from app.schemas.models import RegisterModelVersionResponse

_HARBOR_REF = "registry.example.com/proj/my-model:v1"
_VALID_BODY = {"harbor_ref": _HARBOR_REF, "version": "v1"}

_SUCCESS_RESPONSE = RegisterModelVersionResponse(
    name="mymodel",
    version="v1",
    harbor_ref=_HARBOR_REF,
    category="model",
)


def _make_client(return_value=None, side_effect=None) -> TestClient:
    """Return a TestClient where ModelRegistrationService is a controlled mock.

    The ``get_model_registration_service`` dependency is overridden to return a
    mock whose ``register_model_version`` coroutine either returns *return_value*
    or raises *side_effect*.
    """
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.register_model_version = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )

    app.dependency_overrides[get_model_registration_service] = lambda: mock_service

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 201 success
# ---------------------------------------------------------------------------


def test_register_returns_201_on_success():
    client = _make_client(return_value=_SUCCESS_RESPONSE)
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
    client = _make_client(side_effect=InvalidArtifactNameError("bad name"))
    resp = client.post("/api/models/BAD/versions", json=_VALID_BODY)

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 404 — artifact not found in Harbor
# ---------------------------------------------------------------------------


def test_harbor_not_found_returns_404():
    client = _make_client(side_effect=ArtifactNotFoundInHarborError("not found"))
    resp = client.post("/api/models/mymodel/versions", json=_VALID_BODY)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 502 — Harbor verification error
# ---------------------------------------------------------------------------


def test_harbor_error_returns_502():
    client = _make_client(side_effect=HarborVerificationError("upstream error"))
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
    client = _make_client(side_effect=exc)
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
