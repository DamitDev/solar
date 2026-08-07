"""GET /models, POST /models/pull, and DELETE /models/{name} routes.

GET /models — lists models recorded in MODELS_DIR/manifest.json.
Per S-009, the manifest is the single source of truth. This endpoint does not
scan the filesystem for models; only entries present in the manifest are
returned. Missing or invalid manifest yields an empty list (see read_manifest).
Each entry carries a per-file inventory (relative name + size) so consumers
can offer filter-aware deletion of shared directories.

POST /models/pull — pulls a model from Harbor (ORAS) or HuggingFace Hub and
records it in the manifest. Returns the local path and whether it was a cache
hit. Per S-015 / spec Section 3.6.

DELETE /models/{name} — removes a model from disk and the manifest. Rejects
the request with 409 if any active instance references the model. Per S-017.
An optional ``filters`` body restricts the deletion to matching files (same
fnmatch semantics as HuggingFace allow_patterns): a shared directory holding
several quants of one repository can then be pruned per quant without taking
the other quants' files with it.
"""

import asyncio
import concurrent.futures
import fnmatch
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from solar_host import models_manager
from solar_host.config import config_manager
from solar_host.models.base import InstanceStatus
from solar_host.models_manager import ModelPullError, read_manifest
from solar_host.ws_client import broadcast_pull_progress

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])


# ---------------------------------------------------------------------------
# GET /models — list
# ---------------------------------------------------------------------------


class ModelFile(BaseModel):
    """One file inside a stored model directory."""

    name: str  # path relative to the model directory (repo-relative)
    size_bytes: int


class ModelEntry(BaseModel):
    """A single model entry returned by GET /models.

    ``category``, ``model_name``, ``version`` and ``metadata`` are populated
    from the manifest when the model was pulled with Data Repository
    metadata (D-016); they are omitted on older entries.
    """

    model_config = {"protected_namespaces": ()}

    name: str
    path: str
    size_bytes: int
    source_uri: str | None = None
    checksum: str | None = None
    downloaded_at: str | None = None
    category: str | None = None
    model_name: str | None = None
    version: str | None = None
    metadata: dict | None = None
    file_filters: list[str] | None = None
    files: list[ModelFile] = Field(default_factory=list)


def _list_model_files(path: str) -> list[ModelFile]:
    """Inventory the files under *path* (recursive, repo-relative names).

    An empty list means the path is not a directory (single-file models).
    """
    base = Path(path)
    if not base.is_dir():
        return []
    files: list[ModelFile] = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            try:
                files.append(
                    ModelFile(
                        name=str(p.relative_to(base)),
                        size_bytes=p.stat().st_size,
                    )
                )
            except OSError:
                continue
    return files


def _manifest_to_entries() -> list[ModelEntry]:
    manifest = read_manifest()
    return [
        ModelEntry(
            name=e.slug,
            path=e.path,
            size_bytes=e.size_bytes,
            source_uri=e.source_uri,
            checksum=e.checksum or e.digest,
            downloaded_at=e.downloaded_at,
            category=e.category,
            model_name=e.name,
            version=e.version,
            metadata=e.metadata,
            file_filters=e.file_filters,
            files=_list_model_files(e.path),
        )
        for e in manifest.models
    ]


@router.get("", response_model=list[ModelEntry], summary="List managed models")
async def list_models() -> list[ModelEntry]:
    """Return all models listed in the managed models manifest.

    Data comes only from ``manifest.json`` under ``MODELS_DIR`` (see
    ``read_manifest``). No directory scanning is performed.

    Returns an empty list when the manifest is missing, empty, or unreadable.
    """
    return await asyncio.to_thread(_manifest_to_entries)


# ---------------------------------------------------------------------------
# POST /models/pull
# ---------------------------------------------------------------------------


class PullRequest(BaseModel):
    """Request body for POST /models/pull (spec Section 3.6).

    The ``category``, ``name``, ``version``, ``checksum`` and ``metadata``
    fields were added with D-016 so solar-control can forward the
    authoritative metadata returned by Data Repository. They are optional and
    stored on the manifest entry verbatim when present; the host does not
    consult them for pull behaviour.
    """

    model_config = {"protected_namespaces": ()}

    source: Literal["harbor", "huggingface"]
    source_uri: str
    harbor_ref: str | None = None
    model_id: str | None = None
    digest: str | None = None
    size_bytes: int | None = None
    category: str | None = None
    name: str | None = None
    version: str | None = None
    checksum: str | None = None
    metadata: dict | None = None
    # Declared by solar-control when the pull targets a specific backend
    # (e.g. "llamacpp" from an intent).  For harbor artifacts it enables
    # GGUF selection: the returned path resolves to the largest *.gguf
    # inside the artifact instead of the directory.  None = no selection.
    backend_type: str | None = None
    # HuggingFace allow_patterns (e.g. ["*UD-Q4_K_XL*", "mmproj-BF16.gguf"]) so
    # only the wanted files of a multi-quant repository are downloaded.
    # Ignored for harbor pulls — ORAS cannot filter an artifact.
    file_filters: list[str] | None = None


class PullResponse(BaseModel):
    """Response body for POST /models/pull (spec Section 3.6)."""

    path: str
    cached: bool
    source_uri: str


@router.post(
    "/pull",
    response_model=PullResponse,
    summary="Pull a model from source",
    responses={
        200: {
            "description": "Model available at returned path (cached or freshly downloaded)"
        },
        400: {
            "description": "Invalid request (bad source_uri scheme or malformed URI)"
        },
        401: {"description": "Source authentication failed"},
        404: {"description": "Model not found at source"},
        422: {"description": "Missing required field for the chosen source"},
        500: {"description": "Missing credentials or unexpected server error"},
        502: {"description": "Source registry/hub unreachable"},
        507: {"description": "Insufficient disk space"},
    },
)
async def pull_model(req: PullRequest) -> PullResponse | JSONResponse:
    """Pull a model from Harbor or HuggingFace Hub.

    Checks the manifest cache first. On a cache hit the stored path is returned
    immediately without re-downloading. On a cache miss the model is downloaded,
    the manifest is updated atomically, and the new path is returned.

    The caller blocks until the model is fully available.

    Contract for Distribution (S-019):
    Before issuing a pull, solar-control should query the target host's /health
    endpoint to check disk.available_gb. If the model size is known, it should
    be passed as size_bytes here for proactive validation. If available space
    drops below min_free_disk_gb during download, the pull will be aborted.
    """
    # Validate conditional required fields before doing any I/O.
    if req.source == "harbor" and not req.harbor_ref:
        raise HTTPException(
            status_code=422, detail="harbor_ref is required for harbor source"
        )
    if req.source == "huggingface" and not req.model_id:
        raise HTTPException(
            status_code=422, detail="model_id is required for huggingface source"
        )

    try:
        # C4: bridge pull_model's plain progress callback to the WebSocket.
        # pull_model runs in a worker thread (asyncio.to_thread, and the
        # actual download in a pebble subprocess); the callback lands back on
        # the event loop via run_coroutine_threadsafe — the same thread-to-
        # loop bridging pattern used for log events.
        loop = asyncio.get_running_loop()

        def _log_emit_failure(future: concurrent.futures.Future) -> None:
            # Without consuming the future, an exception raised inside
            # broadcast_pull_progress is swallowed and the progress stream
            # goes quiet with no trace of why.
            try:
                future.result()
            except Exception:
                logger.warning("Failed to emit pull progress event", exc_info=True)

        def _progress_cb(payload: dict) -> None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    broadcast_pull_progress(payload), loop
                )
            except Exception:
                logger.debug("Failed to bridge pull progress event", exc_info=True)
                return
            future.add_done_callback(_log_emit_failure)

        result = await asyncio.to_thread(
            models_manager.pull_model,
            source=req.source,
            source_uri=req.source_uri,
            harbor_ref=req.harbor_ref,
            model_id=req.model_id,
            digest=req.digest,
            size_bytes=req.size_bytes,
            category=req.category,
            name=req.name,
            version=req.version,
            checksum=req.checksum,
            metadata=req.metadata,
            backend_type=req.backend_type,
            file_filters=req.file_filters,
            progress_cb=_progress_cb,
        )
    except ModelPullError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.error,
                "detail": exc.detail,
                "source_uri": exc.source_uri,
                "status_code": exc.status_code,
            },
        )

    return PullResponse(**result)


# ---------------------------------------------------------------------------
# DELETE /models/{name}
# ---------------------------------------------------------------------------

# Statuses that mean an instance is actively using its model files.
_ACTIVE_STATUSES = {
    InstanceStatus.RUNNING,
    InstanceStatus.STARTING,
    InstanceStatus.STOPPING,
}


def _instance_uses_model(instance_config: object, model_dir: Path) -> bool:
    """Return True if *instance_config* references a path under *model_dir*.

    For LlamaCpp configs the ``model`` field is always a filesystem path to a
    GGUF file.  For HuggingFace configs the ``model_id`` may be a Hub ID (e.g.
    ``meta-llama/Llama-2-7b-hf``) or a local absolute path; only absolute
    paths are checked against the model directory.
    """
    backend_type: str = getattr(instance_config, "backend_type", "")

    if backend_type == "llamacpp":
        instance_model = getattr(instance_config, "model", None)
        if not instance_model:
            return False
        resolved = Path(instance_model).resolve()
        return resolved == model_dir or resolved.is_relative_to(model_dir)

    if backend_type.startswith("huggingface_"):
        model_id: str = getattr(instance_config, "model_id", None) or ""
        if not os.path.isabs(model_id):
            # Hub ID (e.g. "org/model") — not a local path, skip.
            return False
        resolved = Path(model_id).resolve()
        return resolved == model_dir or resolved.is_relative_to(model_dir)

    return False


def _active_instances_using_dir(model_dir: Path) -> list[Any]:
    """All active instances whose config references *model_dir*."""
    return [
        instance
        for instance in config_manager.get_all_instances()
        if instance.status in _ACTIVE_STATUSES
        and _instance_uses_model(instance.config, model_dir)
    ]


def _filters_overlap(instance_config: object, file_names: list[str]) -> bool:
    """True when *instance_config*'s download filters match any of *file_names*.

    Conservative by design: an instance without recorded filters (legacy
    config, or a full-repo pull) is treated as matching everything, because
    there is no way to prove it does not use the files.
    """
    filters = getattr(instance_config, "file_filters", None)
    if not filters:
        return True
    return any(
        any(fnmatch.fnmatch(name, pattern) for pattern in filters)
        for name in file_names
    )


def _delete_filtered(
    entry: models_manager.ManifestEntry,
    model_dir: Path,
    target_filters: list[str],
) -> "DeleteResponse":
    """Delete only files matching *target_filters*; keep everything else.

    Returns a DeleteResponse describing what was removed. Raises 409 when
    an active instance may use any of the targeted files.
    """
    files = _list_model_files(str(model_dir))
    if not files:
        # Nothing on disk — drop the entry and report nothing freed.
        models_manager.remove_manifest_entry_by_slug(entry.slug)
        return DeleteResponse(detail="Model deleted", name=entry.slug)

    # Files matching the requested patterns. The caller's filters ARE the
    # intent (the webui sends exact file names; power users may send
    # patterns) — the manifest's filter union is cache metadata, not user
    # intent, so nothing is "kept" behind the caller's back. Protection for
    # running instances comes from the in-use guard below.
    deleted = [
        f for f in files if any(fnmatch.fnmatch(f.name, p) for p in target_filters)
    ]
    if not deleted:
        return DeleteResponse(
            detail="No files match the requested filters",
            name=entry.slug,
            removed=[],
            freed_bytes=0,
            remaining=len(files),
        )

    using = _active_instances_using_dir(model_dir)
    deleted_names = [f.name for f in deleted]
    if using and any(_filters_overlap(inst.config, deleted_names) for inst in using):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Model files are in use by instance {using[0].id}. "
                "Stop the instance first."
            ),
        )

    freed_bytes = 0
    for f in deleted:
        try:
            (model_dir / f.name).unlink()
            freed_bytes += f.size_bytes
        except FileNotFoundError:
            pass

    # Prune now-empty subdirectories (deepest first).
    for p in sorted((model_dir.rglob("*")), key=lambda x: len(x.parts), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass

    remaining = [f for f in files if f.name not in set(deleted_names)]
    if not remaining:
        models_manager.remove_manifest_entry_by_slug(entry.slug)
        try:
            shutil.rmtree(model_dir, ignore_errors=True)
        except OSError:
            pass
        return DeleteResponse(
            detail=f"Deleted {len(deleted)} file(s)",
            name=entry.slug,
            removed=deleted_names,
            freed_bytes=freed_bytes,
            remaining=0,
        )

    # Keep the entry, but make its filter set describe exactly the files
    # that remain — a later pull of a deleted quant then re-downloads the
    # missing files instead of trusting a stale cache hit.
    remaining_names = {f.name for f in remaining}
    updated = entry.model_copy(
        update={
            "file_filters": sorted(remaining_names),
            "size_bytes": sum(f.size_bytes for f in remaining),
            "file_digests": (
                {k: v for k, v in entry.file_digests.items() if k in remaining_names}
                if entry.file_digests
                else None
            ),
        }
    )
    models_manager.add_manifest_entry(updated)
    return DeleteResponse(
        detail=f"Deleted {len(deleted)} file(s), {len(remaining)} remaining",
        name=entry.slug,
        removed=deleted_names,
        freed_bytes=freed_bytes,
        remaining=len(remaining),
    )


class DeleteResponse(BaseModel):
    """Response body for DELETE /models/{name}."""

    detail: str
    name: str
    removed: list[str] = Field(default_factory=list)
    freed_bytes: int = 0
    remaining: int = 0


class DeleteModelRequest(BaseModel):
    """Optional body for DELETE /models/{name}: restrict to matching files."""

    filters: list[str] | None = None


@router.delete(
    "/{name}",
    response_model=DeleteResponse,
    summary="Delete a managed model",
    responses={
        200: {"description": "Model (or matching files) deleted successfully"},
        404: {"description": "Model not found in manifest"},
        409: {"description": "Model is in use by a running instance"},
    },
)
async def delete_model(
    name: str, req: DeleteModelRequest | None = None
) -> DeleteResponse | JSONResponse:
    """Delete a model from disk and remove it from the manifest.

    The ``name`` path parameter must match the slug returned by ``GET /models``.
    Returns 404 if the model is not in the manifest.  Returns 409 if any
    instance with status ``running``, ``starting``, or ``stopping`` references
    the model — stop the instance first.

    With an optional ``filters`` body only files matching those patterns are
    deleted (smart delete); files matching a pattern that is *not* listed are
    kept, and the manifest entry's filter set is narrowed to the remaining
    files. Without filters the whole model directory is removed.
    """

    def _delete() -> DeleteResponse:
        entry = models_manager.get_manifest_entry_by_slug(name)
        if entry is None:
            raise HTTPException(status_code=404, detail="Model not found")

        model_dir = Path(entry.path).resolve()

        if req is not None and req.filters:
            return _delete_filtered(entry, model_dir, req.filters)

        using = _active_instances_using_dir(model_dir)
        if using:
            raise HTTPException(
                status_code=409,
                detail=f"Model is in use by instance {using[0].id}. Stop the instance first.",
            )

        files = _list_model_files(str(model_dir))
        freed_bytes = sum(f.size_bytes for f in files)
        models_manager.delete_model_files(entry.path)
        models_manager.remove_manifest_entry_by_slug(name)
        return DeleteResponse(
            detail="Model deleted",
            name=name,
            removed=[f.name for f in files],
            freed_bytes=freed_bytes,
            remaining=0,
        )

    return await asyncio.to_thread(_delete)
