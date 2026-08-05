"""Tests for the /api/uploads routes (S-047).

The service layer is faked; these tests pin the HTTP surface: auth, status
mapping, streaming pass-through, and response shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.models.uploads import (
    CompleteUploadResponse,
    CreateUploadResponse,
    UploadFileResult,
    UploadFileStatus,
    UploadStatusResponse,
)

# Read from settings: a local .env may override the packaged default.
API_KEY = settings.management_api_key


class FakeUploadService:
    """Scriptable stand-in for UploadService (same async method surface)."""

    def __init__(self) -> None:
        self.create = AsyncMock(
            return_value=CreateUploadResponse(
                upload_id="up-1",
                harbor_ref="harbor.test/supernova/my-model:v1",
                name="my-model",
                version="v1",
                expires_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            )
        )
        self.put_file = AsyncMock(
            return_value=UploadFileResult(
                path="config.json", digest="sha256:abc", size=10
            )
        )
        self.get_status = AsyncMock(
            return_value=UploadStatusResponse(
                upload_id="up-1",
                state="uploading",
                files=[
                    UploadFileStatus(
                        path="config.json", size=10, digest="sha256:abc", uploaded=True
                    ),
                    UploadFileStatus(path="model.gguf", size=1000, uploaded=False),
                ],
                bytes_total=1010,
                bytes_done=10,
            )
        )
        self.complete = AsyncMock(
            return_value=CompleteUploadResponse(
                name="my-model",
                version="v1",
                category="model",
                harbor_ref="harbor.test/supernova/my-model:v1",
                size_bytes=1010,
                registration={"name": "my-model", "version": "v1"},
            )
        )
        self.abort = AsyncMock(return_value=None)


@pytest.fixture
def fake_service():
    return FakeUploadService()


@pytest.fixture
def client(fake_service):
    from app.main import app

    with patch(
        "app.routes.management.uploads.build_upload_service",
        return_value=fake_service,
    ):
        yield TestClient(app)


def _headers() -> dict:
    return {"X-API-Key": API_KEY}


def _create_body() -> dict:
    return {
        "category": "model",
        "name": "my-model",
        "version": "v1",
        "files": [
            {"path": "config.json", "size": 10},
            {"path": "model.gguf", "size": 1000},
        ],
    }


def test_upload_endpoints_require_management_key(client: TestClient):
    assert client.post("/api/uploads", json=_create_body()).status_code == 401
    assert (
        client.put("/api/uploads/up-1/files?path=a.bin", content=b"x").status_code
        == 401
    )
    assert client.get("/api/uploads/up-1").status_code == 401
    assert client.post("/api/uploads/up-1/complete").status_code == 401
    assert client.delete("/api/uploads/up-1").status_code == 401


def test_create_returns_201_with_session(
    client: TestClient, fake_service: FakeUploadService
):
    resp = client.post("/api/uploads", json=_create_body(), headers=_headers())
    assert resp.status_code == 201
    data = resp.json()
    assert data["upload_id"] == "up-1"
    assert data["harbor_ref"] == "harbor.test/supernova/my-model:v1"
    assert data["version"] == "v1"
    fake_service.create.assert_awaited_once()


def test_put_file_streams_body_to_service(
    client: TestClient, fake_service: FakeUploadService
):
    body = b"file-bytes" * 100
    resp = client.put(
        "/api/uploads/up-1/files?path=model.gguf",
        content=body,
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"path": "config.json", "digest": "sha256:abc", "size": 10}

    args, _kwargs = fake_service.put_file.await_args  # type: ignore[union-attr]
    assert args[0] == "up-1"
    assert args[1] == "model.gguf"
    # The third argument is the streamed request body iterator.
    assert hasattr(args[2], "__aiter__")


def test_put_file_unknown_session_404(
    client: TestClient, fake_service: FakeUploadService
):
    fake_service.put_file.side_effect = HTTPException(
        status_code=404, detail="Unknown upload 'nope'"
    )
    resp = client.put(
        "/api/uploads/nope/files?path=a.bin", content=b"x", headers=_headers()
    )
    assert resp.status_code == 404
    assert "Unknown upload" in resp.json()["detail"]


def test_put_file_undeclared_path_422(
    client: TestClient, fake_service: FakeUploadService
):
    fake_service.put_file.side_effect = HTTPException(
        status_code=422, detail="Path 'x.bin' was not declared for upload 'up-1'"
    )
    resp = client.put(
        "/api/uploads/up-1/files?path=x.bin", content=b"x", headers=_headers()
    )
    assert resp.status_code == 422
    assert "not declared" in resp.json()["detail"]


def test_get_status_reports_progress(client: TestClient):
    resp = client.get("/api/uploads/up-1", headers=_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "uploading"
    assert data["bytes_total"] == 1010
    assert data["bytes_done"] == 10
    uploaded = {f["path"]: f for f in data["files"]}
    assert uploaded["config.json"]["uploaded"] is True
    assert uploaded["model.gguf"]["uploaded"] is False


def test_complete_returns_harbor_ref_and_version(client: TestClient):
    resp = client.post("/api/uploads/up-1/complete", headers=_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "my-model"
    assert data["version"] == "v1"
    assert data["harbor_ref"] == "harbor.test/supernova/my-model:v1"
    assert data["size_bytes"] == 1010
    assert data["registration"]["version"] == "v1"


def test_abort_returns_204(client: TestClient, fake_service: FakeUploadService):
    resp = client.delete("/api/uploads/up-1", headers=_headers())
    assert resp.status_code == 204
    fake_service.abort.assert_awaited_once_with("up-1")
