"""Unit tests for app/routes/artifacts.py — unified ``GET /api/artifacts``."""

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_dataset_query_service, get_model_query_service
from app.routes.artifacts import router
from app.schemas.artifacts import ArtifactListResponse, ArtifactSummary

_EMPTY = ArtifactListResponse[ArtifactSummary](total=0, items=[])


def _client(
    *,
    model_return=None,
    dataset_return=None,
) -> tuple[TestClient, MagicMock, MagicMock]:
    app = FastAPI()
    app.include_router(router)
    mock_model = MagicMock()
    mock_model.list_models = AsyncMock(return_value=model_return or _EMPTY)
    mock_ds = MagicMock()
    mock_ds.list_datasets = AsyncMock(return_value=dataset_return or _EMPTY)
    app.dependency_overrides[get_model_query_service] = lambda: mock_model
    app.dependency_overrides[get_dataset_query_service] = lambda: mock_ds
    return (
        TestClient(app, raise_server_exceptions=False),
        mock_model,
        mock_ds,
    )


def test_list_artifacts_missing_category_returns_422():
    client, _, _ = _client()
    resp = client.get("/api/artifacts")
    assert resp.status_code == 422


def test_list_artifacts_model_calls_list_models_only():
    client, mock_model, mock_ds = _client()
    resp = client.get("/api/artifacts?category=model&search=iris&limit=10&offset=2")

    assert resp.status_code == 200
    mock_model.list_models.assert_awaited_once_with(search="iris", limit=10, offset=2)
    mock_ds.list_datasets.assert_not_called()


def test_list_artifacts_dataset_calls_list_datasets_only():
    client, mock_model, mock_ds = _client()
    resp = client.get("/api/artifacts?category=dataset&page=2&page_size=15")

    assert resp.status_code == 200
    mock_ds.list_datasets.assert_awaited_once_with(search=None, limit=15, offset=15)
    mock_model.list_models.assert_not_called()


def test_list_artifacts_invalid_category_returns_422():
    client, _, _ = _client()
    resp = client.get("/api/artifacts?category=other")
    assert resp.status_code == 422
