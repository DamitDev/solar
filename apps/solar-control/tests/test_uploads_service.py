"""Tests for app.services.uploads — validation, pre-flight, orchestration.

The Redis-backed store runs against a dict-based fake Redis (the store
itself is the real ``UploadSessionStore``), the OCI client and the Data
Repository are fakes. This keeps the service tests hermetic while still
exercising the real session persistence code.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import settings
from app.harbor.oci_push import OciPushError
from app.models.uploads import CreateUploadRequest, UploadFileDeclaration
from app.redis_state.uploads import UploadSessionStore
from app.services.uploads import DataRepoClient, UploadService

# ---------------------------------------------------------------------------
# Fake Redis (dict-backed, async interface used by UploadSessionStore)
# ---------------------------------------------------------------------------


class _FakePipe:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[Any, ...]] = []

    def hset(self, key, field, value):
        self._ops.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def delete(self, key):
        self._ops.append(("delete", key))
        return self

    async def execute(self):
        for op in self._ops:
            kind = op[0]
            if kind == "hset":
                await self._redis.hset(op[1], op[2], op[3])
            elif kind == "expire":
                await self._redis.expire(op[1], op[2])
            elif kind == "delete":
                await self._redis.delete(op[1])


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, dict[str, str]] = {}

    def pipeline(self) -> _FakePipe:
        return _FakePipe(self)

    async def hset(self, key, field, value):
        self.store.setdefault(key, {})[field] = value

    async def hget(self, key, field):
        return self.store.get(key, {}).get(field)

    async def hgetall(self, key):
        return dict(self.store.get(key, {}))

    async def expire(self, key, ttl):
        # No-op for the fake; TTL semantics are covered by the integration suite.
        self.store.setdefault(key, {})

    async def delete(self, key):
        self.store.pop(key, None)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDataRepo(DataRepoClient):
    """Scriptable Data Repository client."""

    def __init__(
        self,
        *,
        versions_status: int = 404,
        versions_body: dict | None = None,
        other_status: int = 404,
    ):
        self.get = AsyncMock(side_effect=self._get)
        self.post = AsyncMock(return_value=(201, {"name": "x", "version": "v1"}))
        self._versions_status = versions_status
        self._versions_body = versions_body or {"versions": []}
        self._other_status = other_status

    async def _get(self, path: str):
        if path.endswith("/versions"):
            return self._versions_status, self._versions_body
        return self._other_status, {}


class FakeOci:
    """Scriptable OCI push client."""

    def __init__(self):
        self.push_blob = AsyncMock(return_value=("sha256:file-digest", 100))
        self.push_manifest = AsyncMock(return_value="sha256:manifest-digest")
        self.delete_tag = AsyncMock(return_value=None)


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    # The store binds redis_client at import time, so patch it in the
    # store's namespace (app.redis_state.uploads), not just the source.
    monkeypatch.setattr("app.redis_state.uploads.redis_client", lambda: redis)
    monkeypatch.setattr(settings, "harbor_url", "https://harbor.test")
    monkeypatch.setattr(settings, "harbor_username", "robot")
    monkeypatch.setattr(settings, "harbor_password", "secret")
    monkeypatch.setattr(settings, "upload_session_ttl_s", 86400)
    return redis


def _make_service(*, data_repo: FakeDataRepo | None = None, oci: FakeOci | None = None):
    return UploadService(
        store=UploadSessionStore(),
        oci=oci or FakeOci(),
        data_repo=data_repo or FakeDataRepo(),
    )


def _request(**overrides) -> CreateUploadRequest:
    defaults = {
        "category": "model",
        "name": "my-model",
        "version": "v1",
        "files": [
            UploadFileDeclaration(path="config.json", size=10),
            UploadFileDeclaration(path="model.gguf", size=1000),
        ],
        "metadata": {"description": "test"},
    }
    defaults.update(overrides)
    return CreateUploadRequest(**defaults)


async def _chunks(data: bytes = b"x" * 100, size: int = 32):
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


# ---------------------------------------------------------------------------
# create: validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_rejects_invalid_name(fake_redis):
    service = _make_service()
    with pytest.raises(HTTPException) as exc_info:
        await service.create(_request(name="BadName!"))
    assert exc_info.value.status_code == 422


@pytest.mark.anyio
async def test_create_rejects_reserved_latest_version(fake_redis):
    service = _make_service()
    with pytest.raises(HTTPException) as exc_info:
        await service.create(_request(version="latest"))
    assert exc_info.value.status_code == 422


@pytest.mark.anyio
async def test_create_rejects_traversal_path(fake_redis):
    service = _make_service()
    for bad_path in ("../x", "/abs", "a/../../b", "C:/win"):
        with pytest.raises(HTTPException) as exc_info:
            await service.create(
                _request(files=[UploadFileDeclaration(path=bad_path, size=1)])
            )
        assert exc_info.value.status_code == 422


@pytest.mark.anyio
async def test_create_rejects_duplicate_paths(fake_redis):
    service = _make_service()
    with pytest.raises(HTTPException) as exc_info:
        await service.create(
            _request(
                files=[
                    UploadFileDeclaration(path="a.bin", size=1),
                    UploadFileDeclaration(path="a.bin", size=2),
                ]
            )
        )
    assert exc_info.value.status_code == 422


@pytest.mark.anyio
async def test_create_rejects_empty_file_list(fake_redis):
    with pytest.raises(ValidationError):
        _request(files=[])


# ---------------------------------------------------------------------------
# create: pre-flight conflict checks
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_rejects_existing_version(fake_redis):
    data_repo = FakeDataRepo(
        versions_status=200,
        versions_body={"versions": [{"version": "v1"}, {"version": "v2"}]},
    )
    oci = FakeOci()
    service = _make_service(data_repo=data_repo, oci=oci)

    with pytest.raises(HTTPException) as exc_info:
        await service.create(_request(version="v2"))
    assert exc_info.value.status_code == 409
    # No Harbor call happens on a pre-flight conflict.
    oci.push_blob.assert_not_called()
    oci.push_manifest.assert_not_called()


@pytest.mark.anyio
async def test_create_rejects_category_mismatch(fake_redis):
    # The name exists as a dataset; uploading it as a model must be a 409.
    data_repo = FakeDataRepo(
        versions_status=404,
        other_status=200,  # GET /api/datasets/{name} succeeds
    )
    service = _make_service(data_repo=data_repo)

    with pytest.raises(HTTPException) as exc_info:
        await service.create(_request(category="model"))
    assert exc_info.value.status_code == 409
    assert "dataset" in exc_info.value.detail


@pytest.mark.anyio
async def test_create_assigns_next_version_when_omitted(fake_redis):
    data_repo = FakeDataRepo(
        versions_status=200,
        versions_body={"versions": [{"version": "v1"}, {"version": "v3"}]},
    )
    service = _make_service(data_repo=data_repo)

    result = await service.create(_request(version=None))
    assert result.version == "v4"
    assert result.harbor_ref.endswith("/supernova/my-model:v4")


@pytest.mark.anyio
async def test_create_assigns_v1_for_new_artifact(fake_redis):
    service = _make_service()
    result = await service.create(_request(version=None))
    assert result.version == "v1"


@pytest.mark.anyio
async def test_create_returns_session_and_expiry(fake_redis):
    service = _make_service()
    result = await service.create(_request())
    assert result.upload_id
    assert result.name == "my-model"
    assert result.version == "v1"
    assert result.harbor_ref == "harbor.test/supernova/my-model:v1"
    assert result.expires_at is not None


# ---------------------------------------------------------------------------
# put_file
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_put_file_unknown_session_404(fake_redis):
    service = _make_service()
    with pytest.raises(HTTPException) as exc_info:
        await service.put_file("nope", "a.bin", _chunks())
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_put_file_undeclared_path_422(fake_redis):
    service = _make_service()
    session = await service.create(_request())
    with pytest.raises(HTTPException) as exc_info:
        await service.put_file(session.upload_id, "not-declared.bin", _chunks())
    assert exc_info.value.status_code == 422


@pytest.mark.anyio
async def test_put_file_records_digest_and_refreshes(fake_redis):
    oci = FakeOci()
    service = _make_service(oci=oci)
    session = await service.create(_request())

    result = await service.put_file(session.upload_id, "config.json", _chunks())
    assert result.path == "config.json"
    assert result.digest == "sha256:file-digest"
    oci.push_blob.assert_awaited_once()
    assert await service._store.get_file(session.upload_id, "config.json") == {
        "digest": "sha256:file-digest",
        "size": 100,
    }


@pytest.mark.anyio
async def test_put_file_duplicate_upload_409(fake_redis):
    oci = FakeOci()
    service = _make_service(oci=oci)
    session = await service.create(_request())
    await service.put_file(session.upload_id, "config.json", _chunks())
    with pytest.raises(HTTPException) as exc_info:
        await service.put_file(session.upload_id, "config.json", _chunks())
    assert exc_info.value.status_code == 409


@pytest.mark.anyio
async def test_put_file_harbor_failure_502(fake_redis):
    oci = FakeOci()
    oci.push_blob.side_effect = OciPushError("boom", status_code=500)
    service = _make_service(oci=oci)
    session = await service.create(_request())
    with pytest.raises(HTTPException) as exc_info:
        await service.put_file(session.upload_id, "config.json", _chunks())
    assert exc_info.value.status_code == 502


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_status_reports_progress(fake_redis):
    service = _make_service()
    session = await service.create(_request())
    await service.put_file(session.upload_id, "config.json", _chunks())

    status = await service.get_status(session.upload_id)
    assert status.state == "uploading"
    assert status.bytes_total == 1010
    assert status.bytes_done == 100
    by_path = {f.path: f for f in status.files}
    assert by_path["config.json"].uploaded is True
    assert by_path["config.json"].digest == "sha256:file-digest"
    assert by_path["model.gguf"].uploaded is False


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


async def _complete_session(service: UploadService, *, with_all_files: bool = True):
    session = await service.create(_request())
    if with_all_files:
        for declared in _declared_paths():
            await service.put_file(session.upload_id, declared, _chunks())
    return session


def _declared_paths() -> list[str]:
    return ["config.json", "model.gguf"]


@pytest.mark.anyio
async def test_complete_requires_all_files_uploaded(fake_redis):
    oci = FakeOci()
    service = _make_service(oci=oci)
    session = await service.create(_request())
    await service.put_file(session.upload_id, "config.json", _chunks())

    with pytest.raises(HTTPException) as exc_info:
        await service.complete(session.upload_id)
    assert exc_info.value.status_code == 409
    assert "model.gguf" in exc_info.value.detail
    oci.push_manifest.assert_not_called()


@pytest.mark.anyio
async def test_complete_registers_with_summed_size_bytes(fake_redis):
    data_repo = FakeDataRepo()
    oci = FakeOci()
    service = _make_service(data_repo=data_repo, oci=oci)
    session = await _complete_session(service)

    result = await service.complete(session.upload_id)
    assert result.version == "v1"
    assert result.size_bytes == 1010  # 10 + 1000, the true artifact size

    payload = data_repo.post.await_args.kwargs["json"]
    assert payload["size_bytes"] == 1010
    assert payload["checksum"] == "sha256:manifest-digest"
    assert payload["harbor_ref"].endswith("/supernova/my-model:v1")
    assert payload["metadata"]["description"] == "test"

    # The manifest carried the layer digests from the session.
    manifest = oci.push_manifest.await_args.args[2]
    assert len(manifest["layers"]) == 2
    assert manifest["config"]["mediaType"] == (
        "application/vnd.supernova.model.config.v1+json"
    )


@pytest.mark.anyio
async def test_complete_rolls_back_harbor_tag_on_registration_failure(fake_redis):
    data_repo = FakeDataRepo()
    data_repo.post = AsyncMock(return_value=(409, {"detail": "duplicate"}))
    oci = FakeOci()
    service = _make_service(data_repo=data_repo, oci=oci)
    session = await _complete_session(service)

    with pytest.raises(HTTPException) as exc_info:
        await service.complete(session.upload_id)
    assert exc_info.value.status_code == 409
    assert "duplicate" in exc_info.value.detail

    oci.delete_tag.assert_awaited_once()
    assert oci.delete_tag.await_args.args == ("supernova/my-model", "v1")


@pytest.mark.anyio
async def test_complete_marks_failed_state_after_rollback(fake_redis):
    data_repo = FakeDataRepo()
    data_repo.post = AsyncMock(return_value=(500, "boom"))
    service = _make_service(data_repo=data_repo)
    session = await _complete_session(service)

    with pytest.raises(HTTPException) as exc_info:
        await service.complete(session.upload_id)
    assert exc_info.value.status_code == 502
    assert (await service.get_status(session.upload_id)).state == "failed"


# ---------------------------------------------------------------------------
# abort + replica survival
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_abort_marks_session_aborted(fake_redis):
    oci = FakeOci()
    service = _make_service(oci=oci)
    session = await service.create(_request())

    await service.abort(session.upload_id)

    with pytest.raises(HTTPException) as exc_info:
        await service.put_file(session.upload_id, "config.json", _chunks())
    assert exc_info.value.status_code == 409
    assert (await service.get_status(session.upload_id)).state == "aborted"


@pytest.mark.anyio
async def test_abort_unknown_session_404(fake_redis):
    service = _make_service()
    with pytest.raises(HTTPException) as exc_info:
        await service.abort("nope")
    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_session_survives_replica_change(fake_redis):
    # Two service instances over the same Redis: state lives in the store,
    # not in any one process.
    service_a = _make_service()
    session = await service_a.create(_request())
    await service_a.put_file(session.upload_id, "config.json", _chunks())

    service_b = _make_service()
    status = await service_b.get_status(session.upload_id)
    assert status.upload_id == session.upload_id
    assert status.bytes_done == 100
    assert status.files[0].uploaded is True
