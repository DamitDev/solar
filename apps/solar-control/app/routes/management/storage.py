"""Host storage management API routes (under /api/storage).

Aggregates the per-host local model inventories (solar-host ``GET
/models`` manifest entries) into a cluster view, joins ``in_use_by`` from
the Redis instance cache, and proxies deletions. A bulk delete fans out
concurrently and always returns per-item results — an unreachable host
never 502s the whole request.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import aiohttp
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.database.hosts import host_db
from app.models import Host, HostStatus
from app.redis_state import host_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage", tags=["storage"])

# Statuses that count as "in use" — mirrors solar-host's active statuses
# for the delete guard (running/starting/stopping).
_IN_USE_STATUSES: frozenset[str] = frozenset({"running", "starting", "stopping"})


class InstanceRef(BaseModel):
    """An instance currently using a stored model."""

    instance_id: str
    alias: str
    status: str


class StoredModel(BaseModel):
    """One model stored on one host (manifest entry + usage)."""

    slug: str  # host manifest name — the DELETE key
    model_name: str | None = None
    version: str | None = None
    category: str | None = None
    source_uri: str | None = None
    origin: Literal["repository", "huggingface", "local", "unknown"] = "unknown"
    harbor_ref: str | None = None  # from manifest metadata when present
    path: str
    size_bytes: int
    downloaded_at: str | None = None
    in_use_by: list[InstanceRef] = Field(default_factory=list)


class HostStorage(BaseModel):
    """Storage inventory for one host."""

    host_id: str
    host_name: str
    reachable: bool
    error: str | None = None
    disk_total_gb: float | None = None
    disk_used_gb: float | None = None
    disk_available_gb: float | None = None
    total_size_bytes: int
    models: list[StoredModel] = Field(default_factory=list)


class StorageResponse(BaseModel):
    """Cluster-wide storage inventory."""

    hosts: list[HostStorage]
    unreachable_hosts: list[str]
    generated_at: str


def _derive_origin(
    source_uri: str | None,
) -> Literal["repository", "huggingface", "local", "unknown"]:
    """Derive the origin badge from the source URI scheme."""
    if not source_uri:
        return "unknown"
    if source_uri.startswith("repo://"):
        return "repository"
    if source_uri.startswith("huggingface://"):
        return "huggingface"
    if source_uri.startswith("local://"):
        return "local"
    return "unknown"


def _harbor_ref(metadata: dict | None) -> str | None:
    """Surface ``harbor_ref`` from manifest metadata when present."""
    if not metadata:
        return None
    ref = metadata.get("harbor_ref")
    return ref if isinstance(ref, str) and ref else None


async def _fetch_host_models(host: Host) -> tuple[list[dict], str | None]:
    """Fetch manifest entries from *host*.

    Returns ``(models, None)`` on success and ``([], error_message)`` when
    the host is unreachable or answers with an error status.
    """
    url = f"{host.url.rstrip('/')}/models"
    headers = {"X-API-Key": host.api_key}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
            if resp.status == 200:
                return await resp.json(), None
            return [], f"Host answered HTTP {resp.status}"
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ) as exc:
        return [], f"Host unreachable: {exc}"
    except Exception as exc:  # noqa: BLE001
        return [], f"Cannot reach host: {exc}"


def _unreachable(host: Host, error: str) -> HostStorage:
    return HostStorage(
        host_id=host.id,
        host_name=host.name,
        reachable=False,
        error=error,
        total_size_bytes=0,
        models=[],
    )


async def _build_host_storage(host: Host) -> HostStorage:
    """Build one host's storage view (manifest + Redis usage join)."""
    if host.status in (HostStatus.OFFLINE, HostStatus.ERROR):
        return _unreachable(host, f"Host status is {host.status.value}")

    models_raw, error = await _fetch_host_models(host)
    if error is not None:
        return _unreachable(host, error)

    instances = await host_store.get_host_instances(host.id)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        source = inst.get("model_source")
        if source:
            by_source.setdefault(source, []).append(inst)

    models: list[StoredModel] = []
    for raw in models_raw:
        source_uri = raw.get("source_uri")
        in_use_by: list[InstanceRef] = []
        for inst in by_source.get(str(source_uri), []):
            if inst.get("status") in _IN_USE_STATUSES:
                in_use_by.append(
                    InstanceRef(
                        instance_id=inst.get("id") or inst.get("instance_id") or "",
                        alias=inst.get("alias") or "",
                        status=inst.get("status") or "",
                    )
                )
        models.append(
            StoredModel(
                slug=raw.get("name") or raw.get("slug") or "",
                model_name=raw.get("model_name"),
                version=raw.get("version"),
                category=raw.get("category"),
                source_uri=source_uri,
                origin=_derive_origin(source_uri),
                harbor_ref=_harbor_ref(raw.get("metadata")),
                path=raw.get("path") or "",
                size_bytes=int(raw.get("size_bytes") or 0),
                downloaded_at=raw.get("downloaded_at"),
                in_use_by=in_use_by,
            )
        )

    return HostStorage(
        host_id=host.id,
        host_name=host.name,
        reachable=True,
        disk_total_gb=host.disk_total_gb,
        disk_used_gb=host.disk_used_gb,
        disk_available_gb=host.disk_available_gb,
        total_size_bytes=sum(m.size_bytes for m in models),
        models=models,
    )


@router.get("/hosts", response_model=StorageResponse)
async def list_host_storage() -> StorageResponse:
    """Aggregate storage across all hosts, unreachable ones included."""
    hosts = await host_db.get_all_hosts()
    results = await asyncio.gather(*(_build_host_storage(h) for h in hosts))
    unreachable = [r.host_name for r in results if not r.reachable]
    return StorageResponse(
        hosts=results,
        unreachable_hosts=unreachable,
        generated_at=datetime.now(UTC).isoformat(),
    )


@router.get("/hosts/{host_id}", response_model=HostStorage)
async def get_host_storage(host_id: str) -> HostStorage:
    """Fresh storage view for a single host."""
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return await _build_host_storage(host)


@router.delete("/hosts/{host_id}/models/{slug}")
async def delete_host_model(host_id: str, slug: str):
    """Proxy DELETE to the host, propagating 404/409 verbatim.

    404 means the model is already gone; 409 means an active instance is
    using it — the UI must be able to distinguish the two.
    """
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    if host.status in (HostStatus.OFFLINE, HostStatus.ERROR):
        raise HTTPException(
            status_code=502,
            detail=f"Host '{host.name}' is {host.status.value}",
        )
    url = f"{host.url.rstrip('/')}/models/{quote(slug, safe='')}"
    headers = {"X-API-Key": host.api_key}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.delete(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp,
        ):
            text = await resp.text()
            if resp.status in (200, 404, 409):
                body = json.loads(text) if text else {"detail": ""}
                return JSONResponse(status_code=resp.status, content=body)
            raise HTTPException(status_code=resp.status, detail=text)
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ) as exc:
        raise HTTPException(
            status_code=502, detail=f"Host '{host.name}' is unreachable: {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Cannot reach host '{host.name}': {exc}"
        )


class DeleteItem(BaseModel):
    """One host + model slug to delete in bulk."""

    host_id: str
    slug: str


class DeleteRequest(BaseModel):
    """Bulk delete body."""

    items: list[DeleteItem]


class DeleteResult(BaseModel):
    """Per-item outcome of a bulk delete."""

    host_id: str
    host_name: str
    slug: str
    status: Literal["deleted", "in_use", "not_found", "unreachable", "error"]
    detail: str | None = None
    freed_bytes: int = 0


@router.post("/delete", response_model=list[DeleteResult])
async def bulk_delete_models(req: DeleteRequest) -> list[DeleteResult]:
    """Delete models across hosts, reporting per-item outcomes.

    Always returns 200 with per-item results (partial-result convention,
    mirroring ``POST /api/models/distribute``). The host's own 409 remains
    the authoritative in-use guard — ``in_use_by`` is advisory only.
    """
    hosts = {h.id: h for h in await host_db.get_all_hosts()}

    # Pre-fetch manifest sizes so freed_bytes can be reported for deleted
    # items (the host's delete response does not carry the size).
    size_by_host_slug: dict[tuple[str, str], int] = {}
    for host in {h.id: h for h in hosts.values()}.values():
        if host.status in (HostStatus.OFFLINE, HostStatus.ERROR):
            continue
        models, _ = await _fetch_host_models(host)
        for m in models:
            slug = m.get("name") or m.get("slug")
            if slug:
                size_by_host_slug[(host.id, slug)] = int(m.get("size_bytes") or 0)

    async def _delete_one(item: DeleteItem) -> DeleteResult:
        host = hosts.get(item.host_id)
        if host is None:
            return DeleteResult(
                host_id=item.host_id,
                host_name="",
                slug=item.slug,
                status="not_found",
                detail="Host not found",
            )
        if host.status in (HostStatus.OFFLINE, HostStatus.ERROR):
            return DeleteResult(
                host_id=item.host_id,
                host_name=host.name,
                slug=item.slug,
                status="unreachable",
                detail=f"Host status is {host.status.value}",
            )
        url = f"{host.url.rstrip('/')}/models/{quote(item.slug, safe='')}"
        headers = {"X-API-Key": host.api_key}
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.delete(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp,
            ):
                text = await resp.text()
                if resp.status == 200:
                    return DeleteResult(
                        host_id=item.host_id,
                        host_name=host.name,
                        slug=item.slug,
                        status="deleted",
                        freed_bytes=size_by_host_slug.get((item.host_id, item.slug), 0),
                    )
                if resp.status == 404:
                    return DeleteResult(
                        host_id=item.host_id,
                        host_name=host.name,
                        slug=item.slug,
                        status="not_found",
                        detail=text,
                    )
                if resp.status == 409:
                    return DeleteResult(
                        host_id=item.host_id,
                        host_name=host.name,
                        slug=item.slug,
                        status="in_use",
                        detail=text,
                    )
                return DeleteResult(
                    host_id=item.host_id,
                    host_name=host.name,
                    slug=item.slug,
                    status="error",
                    detail=f"HTTP {resp.status}: {text[:200]}",
                )
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientConnectorError,
            asyncio.TimeoutError,
        ) as exc:
            return DeleteResult(
                host_id=item.host_id,
                host_name=host.name,
                slug=item.slug,
                status="unreachable",
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return DeleteResult(
                host_id=item.host_id,
                host_name=host.name,
                slug=item.slug,
                status="error",
                detail=str(exc),
            )

    return list(await asyncio.gather(*(_delete_one(i) for i in req.items)))
