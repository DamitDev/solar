"""Streaming OCI push client for Harbor (S-047).

Implements the OCI Distribution blob upload protocol against Harbor with
the four registry behaviours established empirically (spec §4.4). Each one
is encoded in a unit test so a refactor cannot silently regress it:

1. **No cookies.** Harbor sets a ``sid`` cookie on ``/v2/`` responses;
   replaying it on a write request fails with ``403 CSRF token invalid``.
   The client never persists cookies between requests.
2. **Preserve the ``_state`` query.** The upload ``Location`` carries a
   ``_state`` query parameter that must be returned verbatim. Replacing the
   query string (e.g. ``httpx`` ``params=``) fails with
   ``404 BLOB_UPLOAD_INVALID``. The digest is appended to the existing
   query, never rebuilt.
3. **8 MiB chunks.** Above the 5 MiB minimum that object-storage registry
   drivers impose, so the chunk size is safe regardless of Harbor's backend.
4. **Token refresh near expiry.** A freshly minted bearer token is accepted
   mid-session, including on the closing ``PUT`` — this is what removes the
   30-minute ceiling on upload duration.

The client streams from an async chunk iterator (e.g. FastAPI's
``Request.stream()``), computes the sha256 as bytes pass through, and never
holds more than one chunk in memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# Layer media type for plain files (oras default; spec §2.1).
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
TITLE_ANNOTATION = "org.opencontainers.image.title"

# Harbor's token endpoint reports TTL in seconds; refresh this much early so
# a token never expires between the check and the request it guards.
_TOKEN_SAFETY_MARGIN_S = 60


class OciPushError(Exception):
    """A Harbor write-path request failed (transport, auth, or status)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


def parse_repo(harbor_ref: str) -> str:
    """Extract the repository part of a Harbor reference.

    ``imgrepo.damit.hu/supernova/iris-osl:v4`` -> ``supernova/iris-osl``.
    """
    rest = harbor_ref
    if "://" in rest:
        rest = rest.split("://", 1)[1]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[0]
    else:
        rest = rest.rsplit(":", 1)[0]
    # Strip the host segment (first path component).
    parts = rest.split("/")
    return "/".join(parts[1:])


def split_project_repo(repo: str) -> tuple[str, str]:
    """Split ``project/repo`` into (project, repo_name) for Harbor's v2.0 API."""
    project, separator, repo_name = repo.partition("/")
    if not separator:
        raise OciPushError(f"Harbor repository {repo!r} must be project/repo")
    return project, repo_name


def assemble_manifest(
    *,
    config_media_type: str,
    config_bytes: bytes,
    layers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a flat OCI image manifest (spec §2.1).

    ``layers`` is a list of ``{"path": ..., "digest": "sha256:...",
    "size": ...}`` — one entry per file. The digest must be the sha256 of
    the exact file bytes; the file name is carried in
    ``org.opencontainers.image.title``.
    """
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": config_media_type,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": LAYER_MEDIA_TYPE,
                "digest": layer["digest"],
                "size": layer["size"],
                "annotations": {TITLE_ANNOTATION: layer["path"]},
            }
            for layer in layers
        ],
        "annotations": {
            "org.opencontainers.image.created": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
        },
    }
    return manifest


class OciPushClient:
    """Streaming OCI push client with token refresh and cookie suppression."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        chunk_size_bytes: int = 8 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._chunk_size_bytes = chunk_size_bytes
        # No cookie jar persistence: Harbor's `sid` cookie must never be
        # replayed (403 CSRF). The jar is also cleared after every request
        # as a second line of defence.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0),
            follow_redirects=True,
            cookies={},
            transport=transport,
        )
        self._token_cache: dict[tuple[str, str], tuple[str, float]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # low-level request helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: httpx.BasicAuth | None = None,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        """Send one request; the cookie jar is cleared before returning."""
        try:
            response = await self._http.request(
                method,
                url,
                headers=headers,
                auth=auth,
                params=params,
                content=content,
            )
        except httpx.ConnectError as exc:
            raise OciPushError(f"Cannot reach Harbor at {url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise OciPushError(
                f"Harbor request timed out ({method} {url}): {exc}"
            ) from exc
        finally:
            # Behaviour 1: never carry Harbor's sid cookie to the next request.
            self._http.cookies.clear()
        return response

    async def _get_token(self, repo: str, actions: tuple[str, ...]) -> str:
        """Fetch (or reuse) a bearer token for *repo* and *actions*."""
        cache_key = (repo, ",".join(actions))
        cached = self._token_cache.get(cache_key)
        if cached:
            token, expires_at = cached
            if time.monotonic() < expires_at:
                return token

        url = f"{self._base_url}/service/token"
        params = {
            "service": "harbor-registry",
            "scope": f"repository:{repo}:{','.join(actions)}",
        }
        response = await self._request(
            "GET",
            url,
            params=params,
            auth=httpx.BasicAuth(self._username, self._password),
        )
        if response.status_code in (401, 403):
            raise OciPushError(
                "Harbor token endpoint rejected the credentials",
                status_code=response.status_code,
            )
        if response.status_code != 200:
            raise OciPushError(
                f"Harbor token endpoint returned {response.status_code}",
                status_code=response.status_code,
            )
        data = response.json()
        token = data["token"]
        expires_in = int(data.get("expires_in", 1800))
        expires_at = time.monotonic() + expires_in - _TOKEN_SAFETY_MARGIN_S
        self._token_cache[cache_key] = (token, expires_at)
        logger.debug("Obtained Harbor token for %s (ttl=%ds)", repo, expires_in)
        return token

    async def _ensure_token(self, repo: str, actions: tuple[str, ...]) -> str:
        """Behaviour 4: refresh the token when near expiry, mid-session."""
        return await self._get_token(repo, actions)

    def _resolve_location(self, location: str) -> str:
        """Resolve a possibly-relative upload Location against the base URL."""
        if location.startswith(("http://", "https://")):
            return location
        return urljoin(self._base_url + "/", location.lstrip("/"))

    @staticmethod
    def _append_digest(location: str, digest: str) -> str:
        """Behaviour 2: append ``digest=`` to the existing query verbatim."""
        separator = "&" if "?" in location else "?"
        return f"{location}{separator}digest={digest}"

    # ------------------------------------------------------------------
    # blob upload
    # ------------------------------------------------------------------

    async def push_blob(
        self,
        repo: str,
        chunks: AsyncIterator[bytes],
    ) -> tuple[str, int]:
        """Stream one blob into Harbor; returns ``(digest, size)``.

        ``chunks`` is an async iterator of raw byte chunks (the client
        request body). The body is coalesced into ``chunk_size_bytes``
        pieces and each piece is PATCHed to Harbor while the sha256 is
        computed incrementally; peak memory is one chunk plus the client's
        own stream chunk.
        """
        token = await self._ensure_token(repo, ("pull", "push"))

        # Open the upload session.
        headers = {"Authorization": f"Bearer {token}"}
        response = await self._request(
            "POST",
            f"{self._base_url}/v2/{repo}/blobs/uploads/",
            headers=headers,
        )
        if response.status_code != 202:
            raise OciPushError(
                f"Failed to open blob upload for {repo}: "
                f"{response.status_code} {response.text[:300]}",
                status_code=response.status_code,
            )
        location = self._resolve_location(response.headers.get("Location", ""))

        digest = hashlib.sha256()
        total = 0
        start = 0
        buffer = bytearray()
        chunk_size = self._chunk_size_bytes

        async def flush(force: bool) -> None:
            """PATCH the accumulated buffer once it reaches a full chunk.

            ``force`` flushes a partial trailing chunk (or a body smaller
            than one chunk). ``start``/``token``/``location``/``digest``/
            ``total`` are captured via ``nonlocal``.
            """
            nonlocal start, token, location, digest, total
            if not buffer:
                return
            if not force and len(buffer) < chunk_size:
                return
            data = bytes(buffer)
            buffer.clear()
            end = start + len(data) - 1
            token = await self._ensure_token(repo, ("pull", "push"))
            response = await self._request(
                "PATCH",
                location,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"{start}-{end}",
                },
                content=data,
            )
            if response.status_code != 202:
                raise OciPushError(
                    f"Chunk PATCH failed for {repo}: {response.status_code} "
                    f"{response.text[:300]}",
                    status_code=response.status_code,
                )
            location = self._resolve_location(
                response.headers.get("Location", location)
            )
            digest.update(data)
            total += len(data)
            start += len(data)

        async for chunk in chunks:
            if not chunk:
                continue
            buffer.extend(chunk)
            await flush(force=False)
        await flush(force=True)

        if total == 0:
            raise OciPushError(f"Refusing to push an empty blob for {repo}")

        # Close the session: PUT with the digest appended to _state.
        digest_hex = digest.hexdigest()
        token = await self._ensure_token(repo, ("pull", "push"))
        response = await self._request(
            "PUT",
            self._append_digest(location, f"sha256:{digest_hex}"),
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 201:
            raise OciPushError(
                f"Blob close failed for {repo}: {response.status_code} "
                f"{response.text[:300]}",
                status_code=response.status_code,
            )
        logger.info("Pushed blob %s (%d bytes) to %s", digest_hex[:16], total, repo)
        return f"sha256:{digest_hex}", total

    # ------------------------------------------------------------------
    # manifest push
    # ------------------------------------------------------------------

    async def push_manifest(
        self,
        repo: str,
        reference: str,
        manifest: dict[str, Any],
        config_bytes: bytes,
    ) -> str:
        """Upload the config blob and PUT the manifest; returns the manifest digest.

        The config blob travels the same chunked path as file blobs; the
        manifest PUT is the final ``201`` of the push.
        """
        # Config blob first (small; one chunk).
        config_digest, _ = await self.push_blob(
            repo, _iter_bytes(config_bytes, self._chunk_size_bytes)
        )
        if manifest["config"]["digest"] != config_digest:
            raise OciPushError(
                f"Config blob digest mismatch: manifest says "
                f"{manifest['config']['digest']}, Harbor stored {config_digest}"
            )

        token = await self._ensure_token(repo, ("pull", "push"))
        body = json.dumps(manifest).encode()
        response = await self._request(
            "PUT",
            f"{self._base_url}/v2/{repo}/manifests/{reference}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": OCI_MANIFEST_MEDIA_TYPE,
            },
            content=body,
        )
        if response.status_code != 201:
            raise OciPushError(
                f"Manifest push failed for {repo}:{reference}: "
                f"{response.status_code} {response.text[:300]}",
                status_code=response.status_code,
            )
        manifest_digest = response.headers.get("Docker-Content-Digest", "")
        if not manifest_digest:
            raise OciPushError(
                f"Harbor accepted the manifest for {repo}:{reference} but "
                "returned no Docker-Content-Digest"
            )
        logger.info("Pushed manifest %s -> %s", f"{repo}:{reference}", manifest_digest)
        return manifest_digest

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------

    async def delete_tag(self, repo: str, reference: str) -> None:
        """Delete one artifact tag via Harbor's v2.0 API (rollback path).

        Used when Data Repository registration fails after the manifest was
        pushed, so a retry is not blocked by a half-created version. The
        robot account may delete artifacts but not repositories.
        """
        project, repo_name = split_project_repo(repo)
        url = (
            f"{self._base_url}/api/v2.0/projects/{project}"
            f"/repositories/{repo_name}/artifacts/{reference}"
        )
        response = await self._request(
            "DELETE", url, auth=httpx.BasicAuth(self._username, self._password)
        )
        if response.status_code in (200, 202, 404):
            # 404 means the tag is already gone — nothing to roll back.
            logger.info(
                "Deleted Harbor tag %s:%s (status=%d)",
                repo,
                reference,
                response.status_code,
            )
            return
        raise OciPushError(
            f"Failed to delete Harbor tag {repo}:{reference}: "
            f"{response.status_code} {response.text[:300]}",
            status_code=response.status_code,
        )


async def _iter_bytes(data: bytes, chunk_size: int) -> AsyncIterator[bytes]:
    """Yield *data* in fixed-size chunks (for config blob uploads)."""
    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]
