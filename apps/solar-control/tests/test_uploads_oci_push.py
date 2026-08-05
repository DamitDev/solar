"""Tests for app.harbor.oci_push — the streaming OCI push client (S-047).

Each of the four registry behaviours observed against real Harbor (spec
§4.4) is encoded here so a refactor cannot silently regress it:

1. no cookies replayed (403 CSRF),
2. the ``_state`` query preserved when appending ``digest`` (404
   BLOB_UPLOAD_INVALID otherwise),
3. 8 MiB chunks (above the 5 MiB object-storage driver minimum),
4. token refresh near expiry, including the closing PUT.

Harbor is mocked with ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from app.harbor.oci_push import (
    OciPushClient,
    OciPushError,
    assemble_manifest,
    parse_repo,
)

CHUNK = 8 * 1024 * 1024

MIIB = 1024 * 1024


async def _chunks(data: bytes, size: int = 64 * 1024):
    """Yield *data* in small pieces, like FastAPI's Request.stream()."""
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


class _HarborStub:
    """MockTransport handler implementing the OCI write path + traps."""

    def __init__(self, *, token_ttl: int = 300, patch_status: int = 202):
        self.requests: list[httpx.Request] = []
        self.token_count = 0
        self.token_ttl = token_ttl
        self.patch_status = patch_status
        self.upload_buffers: dict[str, bytearray] = {}
        self.manifests: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/service/token":
            self.token_count += 1
            return httpx.Response(
                200,
                json={"token": f"tok-{self.token_count}", "expires_in": self.token_ttl},
            )

        if request.method == "POST" and path.endswith("/blobs/uploads/"):
            response = httpx.Response(
                202,
                headers={"Location": "/v2/supernova/x/blobs/uploads/uuid1?_state=st1"},
            )
            # Behaviour 1 trap: Harbor sets a sid cookie on /v2/ responses.
            response.headers["Set-Cookie"] = "sid=abc123; Path=/"
            self.upload_buffers["uuid1"] = bytearray()
            return response

        if request.method == "PATCH":
            if self.patch_status != 202:
                return httpx.Response(
                    self.patch_status,
                    json={"errors": [{"code": "BLOB_UPLOAD_INVALID"}]},
                )
            self.upload_buffers["uuid1"].extend(request.content)
            return httpx.Response(
                202,
                headers={"Location": "/v2/supernova/x/blobs/uploads/uuid1?_state=st1"},
            )

        if request.method == "PUT" and path.endswith("/manifests/v1"):
            import json as _json

            self.manifests.append(_json.loads(request.content.decode()))
            return httpx.Response(
                201, headers={"Docker-Content-Digest": "sha256:manifest-digest"}
            )

        if request.method == "PUT":
            # Behaviour 2 trap: close without the _state query must fail.
            if "_state=st1" not in request.url.query.decode():
                return httpx.Response(
                    404,
                    json={"errors": [{"code": "BLOB_UPLOAD_INVALID"}]},
                )
            body = bytes(self.upload_buffers["uuid1"])
            digest_param = "digest=sha256:" + hashlib.sha256(body).hexdigest()
            if digest_param not in request.url.query.decode():
                return httpx.Response(
                    400, json={"errors": [{"code": "DIGEST_INVALID"}]}
                )
            return httpx.Response(201)

        return httpx.Response(404, json={"errors": [{"code": "UNSUPPORTED"}]})

    def make_client(self, chunk_size: int = CHUNK) -> OciPushClient:
        return OciPushClient(
            "https://harbor.test",
            "robot",
            "secret",
            chunk_size_bytes=chunk_size,
            transport=httpx.MockTransport(self.handler),
        )


async def _run(awaitable):
    return await awaitable


# ---------------------------------------------------------------------------
# blob upload
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_blob_upload_streams_in_chunks():
    stub = _HarborStub()
    client = stub.make_client()
    try:
        _digest, size = await client.push_blob(
            "supernova/x", _chunks(b"a" * (20 * MIIB))
        )
    finally:
        await client.close()

    assert size == 20 * MIIB
    patches = [r for r in stub.requests if r.method == "PATCH"]
    assert len(patches) == 3  # 8 MiB + 8 MiB + 4 MiB
    ranges = [r.headers["Content-Range"] for r in patches]
    assert ranges == [
        f"0-{8 * MIIB - 1}",
        f"{8 * MIIB}-{16 * MIIB - 1}",
        f"{16 * MIIB}-{20 * MIIB - 1}",
    ]
    assert sum(len(r.content) for r in patches) == 20 * MIIB


@pytest.mark.anyio
async def test_blob_upload_computes_digest_while_streaming():
    stub = _HarborStub()
    client = stub.make_client()
    data = b"stream-digest-check" * 1000
    try:
        digest, size = await client.push_blob("supernova/x", _chunks(data))
    finally:
        await client.close()

    assert digest == "sha256:" + hashlib.sha256(data).hexdigest()
    assert size == len(data)


@pytest.mark.anyio
async def test_blob_upload_never_sends_cookies():
    stub = _HarborStub()
    client = stub.make_client()
    try:
        await client.push_blob("supernova/x", _chunks(b"cookies" * 100))
    finally:
        await client.close()

    # Harbour sets sid on the open response (the stub does); the client must
    # never send a Cookie header on any subsequent request.
    assert any(r.method == "POST" for r in stub.requests)  # open happened
    for request in stub.requests:
        assert "cookie" not in {k.lower() for k in request.headers}


@pytest.mark.anyio
async def test_blob_close_preserves_state_query():
    stub = _HarborStub()
    client = stub.make_client()
    data = b"state-query" * 500
    try:
        await client.push_blob("supernova/x", _chunks(data))
    finally:
        await client.close()

    closes = [
        r
        for r in stub.requests
        if r.method == "PUT" and "digest=" in r.url.query.decode()
    ]
    assert len(closes) == 1
    query = closes[0].url.query.decode()
    assert "_state=st1" in query
    assert "digest=sha256:" in query


@pytest.mark.anyio
async def test_token_refreshed_when_near_expiry():
    # expires_in=1 with the 60 s safety margin means every request mints a
    # fresh token — the observable guarantee is that a token minted mid-
    # upload is accepted on the closing PUT.
    stub = _HarborStub(token_ttl=1)
    client = stub.make_client()
    data = b"x" * (3 * MIIB)
    try:
        await client.push_blob("supernova/x", _chunks(data, size=MIIB))
    finally:
        await client.close()

    assert stub.token_count >= 3  # open + at least one mid-session refresh
    closes = [
        r
        for r in stub.requests
        if r.method == "PUT" and "digest=" in r.url.query.decode()
    ]
    close_token = closes[0].headers.get("Authorization", "")
    assert close_token.startswith("Bearer tok-")
    assert close_token != "Bearer tok-1"  # not the token from the open


@pytest.mark.anyio
async def test_patch_failure_raises_and_aborts_session():
    stub = _HarborStub(patch_status=500)
    client = stub.make_client()
    try:
        with pytest.raises(OciPushError) as exc_info:
            await client.push_blob("supernova/x", _chunks(b"y" * MIIB))
    finally:
        await client.close()

    assert exc_info.value.status_code == 500
    # No closing PUT after a failed PATCH.
    assert not [r for r in stub.requests if r.method == "PUT"]


@pytest.mark.anyio
async def test_peak_memory_is_bounded():
    stub = _HarborStub()
    client = stub.make_client()
    data = b"z" * (20 * MIIB)
    try:
        await client.push_blob("supernova/x", _chunks(data, size=64 * 1024))
    finally:
        await client.close()

    # Every PATCH body is at most one full chunk — the client never buffers
    # more than one chunk plus the stream's own chunk.
    for request in stub.requests:
        if request.method == "PATCH":
            assert len(request.content) <= CHUNK


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_manifest_layers_use_flat_layout():
    config = b'{"artifact_type": "model", "name": "x", "version": "v1", "metadata": {}}'
    manifest = assemble_manifest(
        config_media_type="application/vnd.supernova.model.config.v1+json",
        config_bytes=config,
        layers=[
            {"path": "model.gguf", "digest": "sha256:aa", "size": 10},
            {"path": "sub/dir/file.bin", "digest": "sha256:bb", "size": 20},
        ],
    )

    assert manifest["schemaVersion"] == 2
    assert manifest["config"]["mediaType"] == (
        "application/vnd.supernova.model.config.v1+json"
    )
    assert (
        manifest["config"]["digest"] == "sha256:" + hashlib.sha256(config).hexdigest()
    )
    assert len(manifest["layers"]) == 2
    assert manifest["layers"][0]["annotations"]["org.opencontainers.image.title"] == (
        "model.gguf"
    )
    assert manifest["layers"][1]["annotations"]["org.opencontainers.image.title"] == (
        "sub/dir/file.bin"
    )
    assert manifest["layers"][0]["mediaType"] == (
        "application/vnd.oci.image.layer.v1.tar"
    )


@pytest.mark.anyio
async def test_push_manifest_uploads_config_blob_and_manifest():
    stub = _HarborStub()
    client = stub.make_client()
    config = b'{"artifact_type": "model", "name": "x", "version": "v1", "metadata": {}}'
    manifest = assemble_manifest(
        config_media_type="application/vnd.supernova.model.config.v1+json",
        config_bytes=config,
        layers=[{"path": "model.gguf", "digest": "sha256:aa", "size": 10}],
    )
    try:
        digest = await client.push_manifest("supernova/x", "v1", manifest, config)
    finally:
        await client.close()

    assert digest == "sha256:manifest-digest"
    # The config blob went through the same upload path (open + close).
    assert len(stub.manifests) == 1
    assert stub.manifests[0]["config"]["digest"] == manifest["config"]["digest"]


def test_parse_repo():
    assert parse_repo("imgrepo.damit.hu/supernova/iris-osl:v4") == "supernova/iris-osl"
    assert parse_repo("imgrepo.damit.hu/supernova/a/b:latest") == "supernova/a/b"
    assert parse_repo("harbor.test/supernova/x@sha256:abc") == "supernova/x"


@pytest.mark.anyio
async def test_push_blob_rejects_empty_body():
    stub = _HarborStub()
    client = stub.make_client()
    try:
        with pytest.raises(OciPushError, match="empty blob"):
            await client.push_blob("supernova/x", _chunks(b""))
    finally:
        await client.close()


@pytest.mark.anyio
async def test_push_blob_small_body_single_chunk():
    stub = _HarborStub()
    client = stub.make_client()
    data = b"small" * 1000
    try:
        await client.push_blob("supernova/x", _chunks(data))
    finally:
        await client.close()

    patches = [r for r in stub.requests if r.method == "PATCH"]
    assert len(patches) == 1
    assert patches[0].headers["Content-Range"] == f"0-{len(data) - 1}"


# ---------------------------------------------------------------------------
# delete_repository (best-effort repository cleanup, S-048)
# ---------------------------------------------------------------------------


class _RepoDeleteStub:
    """MockTransport handler for the v2.0 repository DELETE endpoint."""

    def __init__(self, status: int):
        self.status = status
        self.requests: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={})

    def make_client(self) -> OciPushClient:
        return OciPushClient(
            "https://harbor.test",
            "robot",
            "secret",
            transport=httpx.MockTransport(self.handler),
        )


async def _delete_repo_with(status: int) -> tuple[bool, list[httpx.Request]]:
    stub = _RepoDeleteStub(status)
    client = stub.make_client()
    try:
        result = await client.delete_repository("supernova/iris-osl")
    finally:
        await client.close()
    return result, stub.requests


@pytest.mark.anyio
async def test_delete_repository_success_returns_true():
    result, requests = await _delete_repo_with(200)

    assert result is True
    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == (
        "/api/v2.0/projects/supernova/repositories/iris-osl"
    )
    assert "Authorization" in requests[0].headers


@pytest.mark.anyio
async def test_delete_repository_202_returns_true():
    result, _ = await _delete_repo_with(202)
    assert result is True


@pytest.mark.anyio
async def test_delete_repository_404_already_gone_returns_true():
    """Harbor auto-removes an empty repo — 404 means it is already gone."""
    result, _ = await _delete_repo_with(404)
    assert result is True


@pytest.mark.anyio
async def test_delete_repository_403_forbidden_returns_false():
    """Robot account lacks repository-delete permission — reported, no raise."""
    result, _ = await _delete_repo_with(403)
    assert result is False


@pytest.mark.anyio
async def test_delete_repository_500_returns_false():
    result, _ = await _delete_repo_with(500)
    assert result is False
