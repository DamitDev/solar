"""Artifact upload service: validation, orchestration, registration (S-047).

Validates the session request before a single byte moves, checks version
conflicts against the Data Repository up front (so the operator fails in
the form rather than after a long upload), streams each declared file into
Harbor via the OCI push client, and on ``complete`` pushes the manifest and
registers the version. If registration fails, the just-pushed Harbor tag is
deleted so a retry is not blocked by a half-created version.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from fastapi import HTTPException

from app.config import settings
from app.harbor.oci_push import (
    OciPushClient,
    OciPushError,
    assemble_manifest,
)
from app.models.uploads import (
    CompleteUploadResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadFileResult,
    UploadFileStatus,
    UploadStatusResponse,
)
from app.redis_state.uploads import UploadSessionStore

logger = logging.getLogger(__name__)

# Mirrors _NAME_RE in the Data Repository (apps/data-repository
# app/services/models.py). The relay must accept exactly what the
# repository accepts, no more.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# OCI config media types per category (spec §2.1).
_CONFIG_MEDIA_TYPES = {
    "model": "application/vnd.supernova.model.config.v1+json",
    "dataset": "application/vnd.supernova.dataset.config.v1+json",
}

# Harbor project that holds SuperNova artifacts.
HARBOR_PROJECT = "supernova"

# States in which a session may still accept file uploads.
_UPLOADABLE_STATES = ("pending", "uploading")


class DataRepoClient:
    """Minimal Data Repository HTTP client used by the upload service."""

    async def get(self, path: str) -> tuple[int, Any]:
        raise NotImplementedError

    async def post(self, path: str, json: Any) -> tuple[int, Any]:
        raise NotImplementedError


class AioHttpDataRepo(DataRepoClient):
    """aiohttp implementation wired to ``settings.data_repository_*``."""

    async def _request(
        self, method: str, path: str, json: Any = None
    ) -> tuple[int, Any]:
        if not settings.data_repository_url:
            raise HTTPException(
                status_code=500,
                detail="DATA_REPOSITORY_URL is not configured",
            )
        url = f"{settings.data_repository_url.rstrip('/')}{path}"
        headers = {"Content-Type": "application/json"}
        if settings.data_repository_api_key:
            headers["X-API-Key"] = settings.data_repository_api_key

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.request(
                    method,
                    url,
                    json=json,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(
                        total=settings.data_repository_timeout_s
                    ),
                ) as response,
            ):
                try:
                    body = await response.json()
                except Exception:  # noqa: BLE001
                    body = await response.text()
                return response.status, body
        except (aiohttp.ClientConnectionError, aiohttp.ClientConnectorError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Data Repository is unreachable: {exc}",
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Data Repository request timed out: {exc}",
            )

    async def get(self, path: str) -> tuple[int, Any]:
        return await self._request("GET", path)

    async def post(self, path: str, json: Any) -> tuple[int, Any]:
        return await self._request("POST", path, json=json)


def _validate_file_path(path: str) -> None:
    """Reject paths violating the artifact layout contract (spec §2.3)."""
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise HTTPException(
            status_code=422,
            detail=f"File path {path!r} must be a relative POSIX path",
        )
    if any(segment in (".", "..") for segment in path.split("/")):
        raise HTTPException(
            status_code=422,
            detail=f"File path {path!r} must not contain '.' or '..' segments",
        )


def _validate_upload_request(request: CreateUploadRequest) -> None:
    """Validate name, version, and file declarations (spec §4.2)."""
    if not _NAME_RE.match(request.name):
        raise HTTPException(
            status_code=422,
            detail=(
                "Artifact name must be 1-255 characters and contain only "
                "lowercase alphanumeric characters, hyphens, underscores, or dots."
            ),
        )
    if request.version is not None:
        if request.version.lower() == "latest":
            raise HTTPException(
                status_code=422,
                detail="'latest' is a reserved alias and cannot be used as a version tag.",
            )
        if not _VERSION_RE.match(request.version):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Version must be 1-128 characters containing only "
                    "alphanumeric characters, hyphens, underscores, or dots."
                ),
            )
    seen: set[str] = set()
    for file in request.files:
        _validate_file_path(file.path)
        if file.path in seen:
            raise HTTPException(
                status_code=422,
                detail=f"Duplicate file path in upload: {file.path!r}",
            )
        seen.add(file.path)


def _next_version(existing_versions: list[str]) -> str:
    """Resolve the next ``v{n}`` the same way the Data Repository would."""
    nums = [int(v[1:]) for v in existing_versions if re.fullmatch(r"v\d+", v)]
    return f"v{max(nums) + 1}" if nums else "v1"


def _harbor_host() -> str:
    from urllib.parse import urlparse

    if not settings.harbor_url:
        raise HTTPException(status_code=500, detail="HARBOR_URL is not configured")
    host = urlparse(settings.harbor_url).netloc
    if not host:
        raise HTTPException(status_code=500, detail="HARBOR_URL is not configured")
    return host


class UploadService:
    """Validates, orchestrates, registers, and rolls back artifact uploads."""

    def __init__(
        self,
        *,
        store: UploadSessionStore | None = None,
        oci: OciPushClient | None = None,
        data_repo: DataRepoClient | None = None,
    ) -> None:
        self._store = store or UploadSessionStore()
        self._oci = oci
        self._data_repo = data_repo or AioHttpDataRepo()

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    async def create(self, request: CreateUploadRequest) -> CreateUploadResponse:
        """Validate + pre-flight conflict check, then persist the session."""
        _validate_upload_request(request)

        category = request.category
        name = request.name

        # Pre-flight against the Data Repository (spec §4.2): the operator
        # fails in the form, not after a 40-minute upload.
        versions_status, versions_body = await self._data_repo.get(
            f"/api/{category}s/{name}/versions"
        )
        if versions_status == 200:
            existing_versions = [
                item.get("version")
                for item in (versions_body or {}).get("versions", [])
                if item.get("version")
            ]
            if request.version is not None:
                if request.version in existing_versions:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Version {request.version!r} already exists for "
                            f"{category} {name!r}."
                        ),
                    )
                version = request.version
            else:
                version = _next_version(existing_versions)
        elif versions_status == 404:
            # Artifact does not exist (yet) in this category. If it exists
            # under the other category, that is a category mismatch.
            other = "dataset" if category == "model" else "model"
            other_status, _other_body = await self._data_repo.get(
                f"/api/{other}s/{name}"
            )
            if other_status == 200:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Artifact {name!r} already exists as a {other}; "
                        f"cannot upload it as a {category}."
                    ),
                )
            version = request.version or "v1"
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Data Repository pre-flight check failed [{versions_status}]",
            )

        host = _harbor_host()
        repo = f"{HARBOR_PROJECT}/{name}"
        harbor_ref = f"{host}/{repo}:{version}"

        upload_id = await self._store.create(
            category=category,
            name=name,
            version=version,
            repo=repo,
            harbor_ref=harbor_ref,
            metadata=request.metadata,
            files=[{"path": f.path, "size": f.size} for f in request.files],
        )
        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.upload_session_ttl_s
        )
        logger.info(
            "Created upload session %s for %s:%s (%d files)",
            upload_id,
            name,
            version,
            len(request.files),
        )
        return CreateUploadResponse(
            upload_id=upload_id,
            harbor_ref=harbor_ref,
            name=name,
            version=version,
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------
    # file upload
    # ------------------------------------------------------------------

    async def put_file(
        self,
        upload_id: str,
        path: str,
        chunks: AsyncIterator[bytes],
    ) -> UploadFileResult:
        """Stream one declared file into Harbor and record its digest."""
        _validate_file_path(path)
        session = await self._store.get(upload_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Unknown upload {upload_id!r}")
        if session["state"] not in _UPLOADABLE_STATES:
            raise HTTPException(
                status_code=409,
                detail=f"Upload session {upload_id!r} is {session['state']}",
            )

        declared = {f["path"]: f for f in session["files"]}
        if path not in declared:
            raise HTTPException(
                status_code=422,
                detail=f"Path {path!r} was not declared for upload {upload_id!r}",
            )
        if await self._store.get_file(upload_id, path) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"File {path!r} was already uploaded",
            )

        if self._oci is None:
            raise HTTPException(status_code=500, detail="HARBOR_URL is not configured")

        await self._store.set_state(upload_id, "uploading")
        try:
            digest, size = await self._oci.push_blob(session["repo"], chunks)
        except OciPushError as exc:
            logger.error("Blob upload failed for %s (%s): %s", upload_id, path, exc)
            raise HTTPException(
                status_code=502,
                detail=f"Harbor blob upload failed for {path!r}: {exc}",
            ) from exc

        await self._store.record_file(upload_id, path, digest=digest, size=size)
        logger.info(
            "Uploaded %s (%d bytes, %s) to session %s", path, size, digest, upload_id
        )
        return UploadFileResult(path=path, digest=digest, size=size)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    async def get_status(self, upload_id: str) -> UploadStatusResponse:
        session = await self._store.get(upload_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Unknown upload {upload_id!r}")

        uploaded = await self._store.list_files(upload_id)
        files: list[UploadFileStatus] = []
        bytes_total = 0
        bytes_done = 0
        for declared in session["files"]:
            path = declared["path"]
            record = uploaded.get(path)
            files.append(
                UploadFileStatus(
                    path=path,
                    size=declared["size"],
                    digest=record["digest"] if record else None,
                    uploaded=record is not None,
                )
            )
            bytes_total += declared["size"]
            if record:
                bytes_done += record["size"]

        return UploadStatusResponse(
            upload_id=upload_id,
            state=session["state"],
            files=files,
            bytes_total=bytes_total,
            bytes_done=bytes_done,
        )

    # ------------------------------------------------------------------
    # complete
    # ------------------------------------------------------------------

    async def complete(self, upload_id: str) -> CompleteUploadResponse:
        session = await self._store.get(upload_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Unknown upload {upload_id!r}")
        if session["state"] in ("aborted", "failed"):
            raise HTTPException(
                status_code=409,
                detail=f"Upload session {upload_id!r} is {session['state']}",
            )
        if session["state"] in ("completing", "completed"):
            raise HTTPException(
                status_code=409,
                detail=f"Upload session {upload_id!r} is already {session['state']}",
            )

        uploaded = await self._store.list_files(upload_id)
        missing = [f["path"] for f in session["files"] if f["path"] not in uploaded]
        if missing:
            raise HTTPException(
                status_code=409,
                detail=f"Not all files uploaded yet; missing: {', '.join(missing)}",
            )

        if self._oci is None:
            raise HTTPException(status_code=500, detail="HARBOR_URL is not configured")

        await self._store.set_state(upload_id, "completing")
        name = session["name"]
        version = session["version"]
        repo = session["repo"]
        category = session["category"]

        # Push the config blob and the manifest (spec §4.5).
        config = {
            "artifact_type": category,
            "name": name,
            "version": version,
            "metadata": session["metadata"],
        }
        config_bytes = json.dumps(config).encode()
        layers = [
            {
                "path": f["path"],
                "digest": uploaded[f["path"]]["digest"],
                "size": f["size"],
            }
            for f in session["files"]
        ]
        manifest = assemble_manifest(
            config_media_type=_CONFIG_MEDIA_TYPES[category],
            config_bytes=config_bytes,
            layers=layers,
        )
        try:
            manifest_digest = await self._oci.push_manifest(
                repo, version, manifest, config_bytes
            )
        except OciPushError as exc:
            await self._store.set_state(upload_id, "failed")
            raise HTTPException(
                status_code=502,
                detail=f"Harbor manifest push failed: {exc}",
            ) from exc

        # Register with the Data Repository. size_bytes must be sent
        # explicitly: the fallback derives it from the manifest HEAD, which
        # reports the manifest JSON size, not the artifact size (§4.5).
        size_bytes = sum(f["size"] for f in session["files"])
        try:
            status, body = await self._data_repo.post(
                f"/api/{category}s/{name}/versions",
                json={
                    "harbor_ref": session["harbor_ref"],
                    "version": version,
                    "checksum": manifest_digest,
                    "size_bytes": size_bytes,
                    "metadata": session["metadata"],
                },
            )
        except HTTPException:
            await self._store.set_state(upload_id, "failed")
            raise

        if status not in (200, 201):
            # Roll back: delete the just-pushed tag so a retry is not
            # blocked (spec §4.5 step 3).
            logger.error(
                "Registration failed for %s:%s [%d], deleting Harbor tag %s",
                name,
                version,
                status,
                f"{repo}:{version}",
            )
            try:
                await self._oci.delete_tag(repo, version)
            except OciPushError as rollback_exc:
                logger.error(
                    "Rollback delete of %s:%s also failed: %s",
                    repo,
                    version,
                    rollback_exc,
                )
            await self._store.set_state(upload_id, "failed")
            detail = body.get("detail") if isinstance(body, dict) else body
            raise HTTPException(
                status_code=status if 400 <= status < 500 else 502,
                detail=detail or f"Data Repository registration failed [{status}]",
            )

        await self._store.set_state(upload_id, "completed")
        logger.info("Registered %s:%s (manifest %s)", name, version, manifest_digest)
        return CompleteUploadResponse(
            name=name,
            version=version,
            category=category,
            harbor_ref=session["harbor_ref"],
            size_bytes=size_bytes,
            registration=body if isinstance(body, dict) else {},
        )

    # ------------------------------------------------------------------
    # abort
    # ------------------------------------------------------------------

    async def abort(self, upload_id: str) -> None:
        """Mark the session aborted; subsequent uploads are rejected (409)."""
        session = await self._store.get(upload_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Unknown upload {upload_id!r}")
        await self._store.set_state(upload_id, "aborted")
        logger.info("Aborted upload session %s", upload_id)


# ---------------------------------------------------------------------------
# Service factory (routes use this; tests build UploadService directly)
# ---------------------------------------------------------------------------

_oci: OciPushClient | None = None


def get_oci_client() -> OciPushClient:
    """Lazily created singleton OCI client (keeps the token cache warm)."""
    global _oci
    if _oci is None:
        if not settings.harbor_url:
            raise HTTPException(status_code=500, detail="HARBOR_URL is not configured")
        _oci = OciPushClient(
            settings.harbor_url,
            settings.harbor_username,
            settings.harbor_password,
            chunk_size_bytes=settings.upload_chunk_size_bytes,
        )
    return _oci


async def close_oci_client() -> None:
    """Close the singleton OCI client (app shutdown)."""
    global _oci
    if _oci is not None:
        await _oci.close()
        _oci = None


def build_upload_service() -> UploadService:
    return UploadService(
        oci=get_oci_client(),
        data_repo=AioHttpDataRepo(),
    )
