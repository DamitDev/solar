"""Unit tests for app/routes/datasets.py."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_dataset_registration_service
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    VersionAlreadyExistsError,
)
from app.routes.datasets import router
from app.schemas.datasets import RegisterDatasetVersionResponse

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


def _make_client(return_value=None, side_effect=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    mock_service = MagicMock()
    mock_service.register_dataset_version = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )

    app.dependency_overrides[get_dataset_registration_service] = lambda: mock_service
    return TestClient(app, raise_server_exceptions=False)


def test_register_returns_201_on_success():
    client = _make_client(return_value=_SUCCESS_RESPONSE)
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "iris-tickets"
    assert data["version"] == "v1"
    assert data["category"] == "dataset"


def test_invalid_name_returns_422():
    client = _make_client(side_effect=InvalidArtifactNameError("bad name"))
    resp = client.post("/api/datasets/BAD/versions", json=_VALID_BODY)

    assert resp.status_code == 422


def test_harbor_not_found_returns_404():
    client = _make_client(side_effect=ArtifactNotFoundInHarborError("not found"))
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 404


def test_harbor_error_returns_502():
    client = _make_client(side_effect=HarborVerificationError("upstream error"))
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
    client = _make_client(side_effect=exc)
    resp = client.post("/api/datasets/iris-tickets/versions", json=_VALID_BODY)

    assert resp.status_code == 409


def test_name_collision_with_existing_model_returns_409_with_detail():
    client = _make_client(
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
    client = _make_client(return_value=_SUCCESS_RESPONSE)
    resp = client.post(
        "/api/datasets/iris-tickets/versions",
        json={
            "harbor_ref": _HARBOR_REF,
            "metadata": {"format": "csv"},
        },
    )

    assert resp.status_code == 422
