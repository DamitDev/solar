"""Unit tests for app/services/resolve.py and app/routes/resolve.py."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies import get_resolve_service
from app.exceptions import (
    CatalogArtifactNotFoundError,
    CatalogVersionNotFoundError,
    InvalidArtifactNameError,
)
from app.repositories.artifacts import ArtifactVersionRecord
from app.routes.resolve import router
from app.services.resolve import ResolveService

# ---------------------------------------------------------------------------
# Service Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_service_success():
    mock_session = AsyncMock()
    service = ResolveService(mock_session)

    # Mock repository
    record = ArtifactVersionRecord(
        name="iris-osl",
        version="v3",
        category="model",
        harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v3",
        size_bytes=1024,
        checksum="sha256:abc",
        created_at=datetime.now(UTC),
        metadata={"a": 1},
    )
    service._repo.resolve_artifact_version = AsyncMock(return_value=record)

    result = await service.resolve_uri("repo://iris-osl:v3")

    assert result.name == "iris-osl"
    assert result.version == "v3"
    assert result.harbor_ref == "imgrepo.damit.hu/supernova/iris-osl:v3"
    service._repo.resolve_artifact_version.assert_awaited_once_with(
        name="iris-osl", version="v3"
    )


@pytest.mark.asyncio
async def test_resolve_service_latest():
    mock_session = AsyncMock()
    service = ResolveService(mock_session)

    record = ArtifactVersionRecord(
        name="iris-osl",
        version="v10",
        category="model",
        harbor_ref="imgrepo.damit.hu/supernova/iris-osl:v10",
        size_bytes=2048,
        checksum="sha256:def",
        created_at=datetime.now(UTC),
        metadata={},
    )
    service._repo.resolve_artifact_version = AsyncMock(return_value=record)

    result = await service.resolve_uri("repo://iris-osl:latest")

    assert result.version == "v10"
    service._repo.resolve_artifact_version.assert_awaited_once_with(
        name="iris-osl", version="latest"
    )


@pytest.mark.asyncio
async def test_resolve_service_invalid_format():
    mock_session = AsyncMock()
    service = ResolveService(mock_session)

    with pytest.raises(InvalidArtifactNameError, match="Invalid URI format"):
        await service.resolve_uri("invalid://iris-osl:v3")


@pytest.mark.asyncio
async def test_resolve_service_invalid_name():
    mock_session = AsyncMock()
    service = ResolveService(mock_session)

    # Name with uppercase is invalid according to _NAME_RE in models.py
    with pytest.raises(InvalidArtifactNameError):
        await service.resolve_uri("repo://Iris-Osl:v3")


# ---------------------------------------------------------------------------
# Route Tests
# ---------------------------------------------------------------------------


def _client(mock_service: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_resolve_service] = lambda: mock_service
    return TestClient(app, raise_server_exceptions=False)


def test_route_resolve_success():
    mock_service = MagicMock()
    mock_service.resolve_uri = AsyncMock(
        return_value={
            "category": "model",
            "name": "iris-osl",
            "version": "v3",
            "harbor_ref": "imgrepo.damit.hu/supernova/iris-osl:v3",
            "size_bytes": 123,
            "checksum": "sha256:abc",
            "metadata": {},
            "created_at": datetime.now(UTC).isoformat(),
        }
    )

    client = _client(mock_service)
    resp = client.get("/api/resolve?uri=repo://iris-osl:v3")

    assert resp.status_code == 200
    assert resp.json()["name"] == "iris-osl"
    mock_service.resolve_uri.assert_awaited_once_with("repo://iris-osl:v3")


def test_route_resolve_404_artifact():
    mock_service = MagicMock()
    mock_service.resolve_uri = AsyncMock(
        side_effect=CatalogArtifactNotFoundError("Not found")
    )

    client = _client(mock_service)
    resp = client.get("/api/resolve?uri=repo://missing:v1")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


def test_route_resolve_404_version():
    mock_service = MagicMock()
    mock_service.resolve_uri = AsyncMock(
        side_effect=CatalogVersionNotFoundError("Version not found")
    )

    client = _client(mock_service)
    resp = client.get("/api/resolve?uri=repo://iris-osl:v99")

    assert resp.status_code == 404


def test_route_resolve_422_invalid_uri():
    mock_service = MagicMock()
    mock_service.resolve_uri = AsyncMock(
        side_effect=InvalidArtifactNameError("Invalid format")
    )

    client = _client(mock_service)
    resp = client.get("/api/resolve?uri=not-a-repo-uri")

    assert resp.status_code == 422
