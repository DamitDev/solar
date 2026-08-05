"""Model catalog API routes (under /api/catalog) — D-018.

``GET /api/catalog/models`` proxies Data Repository's D-013 model list
(``GET /api/models``) and enriches each model with Solar runtime context:

* **deployed hosts** — hosts where the model files are present, using the
  same per-host ``GET /models`` mechanism as S-020
  (``GET /api/models/availability``). Entries are joined on the
  authoritative ``model_name`` recorded in the host manifest since D-016,
  falling back to the manifest ``name`` for legacy entries.
* **running instances** — instances currently serving the model, joined
  through their ``model_source`` (e.g. ``repo://name:version`` or
  ``huggingface://org/model``) against the host instance cache.

Data Repository remains the authority for catalog metadata and versions;
Solar Control only adds runtime context. Enrichment is best-effort: a
failed host poll never fails the whole request — it degrades
``meta.enrichment`` and the per-model ``solar.status`` derivation so the
WebUI never sees a misleading "unavailable" when the availability source
itself is down.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Literal

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.config import settings
from app.database.hosts import host_db
from app.models import Host
from app.services import catalog_delete

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalog", tags=["catalog"])


# ── Response models ───────────────────────────────────────────


class DeployedHostInfo(BaseModel):
    """A host where the model's files are present (S-020 availability)."""

    host_id: str
    host_name: str
    size_bytes: int = 0
    path: str = ""


class RunningInstanceInfo(BaseModel):
    """A running instance of the model (host instance state)."""

    host_id: str
    host_name: str
    instance_id: str


class SolarRuntimeInfo(BaseModel):
    """Solar runtime context added by Solar Control (D-018).

    ``status`` is derived per model:
    * ``available`` — at least one running instance exists.
    * ``deployed`` — on at least one host, but no instance running.
    * ``unavailable`` — not on any host and no instance running.
    * ``unknown`` — no deployment evidence and the availability source
      itself could not be reached, so absence cannot be proven.
    """

    status: Literal["available", "deployed", "unavailable", "unknown"]
    running_instances: int
    deployed_hosts: list[DeployedHostInfo] = Field(default_factory=list)
    instances: list[RunningInstanceInfo] = Field(default_factory=list)


class CatalogModelItem(BaseModel):
    """One catalog entry: Data Repository metadata + Solar runtime context."""

    name: str
    category: str
    description: str | None = None
    versions_count: int
    latest_version: str | None = None
    created_at: datetime
    solar: SolarRuntimeInfo


class CatalogMeta(BaseModel):
    """Health of the Solar-side enrichment sources for this response.

    ``ok`` — every host answered; ``partial`` — some hosts failed;
    ``unavailable`` — no host answered (enrichment is degraded, per-model
    statuses fall back to ``unknown`` unless instance evidence exists).
    """

    enrichment: Literal["ok", "partial", "unavailable"]


class CatalogResponse(BaseModel):
    total: int
    items: list[CatalogModelItem]
    meta: CatalogMeta


class VersionSolarRuntimeInfo(BaseModel):
    """Per-version runtime context (S-048) — what blocks a version delete."""

    running_instances: int
    deployed_hosts: list[DeployedHostInfo] = Field(default_factory=list)


class CatalogModelVersionItem(BaseModel):
    """One catalog version: Data Repository metadata + per-version Solar block."""

    version: str
    harbor_ref: str
    created_at: datetime
    size_bytes: int | None = None
    checksum: str | None = None
    solar: VersionSolarRuntimeInfo


class CatalogModelVersionsResponse(BaseModel):
    versions: list[CatalogModelVersionItem]


# ── Data Repository proxy (D-013 / S-048) ─────────────────────


async def _request_data_repository(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Data Repository and return its JSON body.

    Error mapping (mirrors ``app.model_resolvers.repo``):
      * 500 if ``DATA_REPOSITORY_URL`` is unset
      * 404/422 propagated verbatim from Data Repository
      * 502 for all other upstream errors and transport failures
    """
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
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=settings.data_repository_timeout_s),
            ) as response,
        ):
            if response.status == 200:
                return await response.json()

            try:
                err = await response.json()
                detail = err.get("detail") or err.get("error")
            except Exception:  # noqa: BLE001
                detail = await response.text()

            if response.status in {404, 422}:
                raise HTTPException(
                    status_code=response.status,
                    detail=detail or f"Data Repository {path} failed",
                )

            raise HTTPException(
                status_code=502,
                detail=(
                    f"Data Repository {path} failed " f"[{response.status}]: {detail}"
                ),
            )
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Data Repository is unreachable: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Unexpected error during Data Repository {path}: {exc}",
        )


async def _list_data_repository_models(
    search: str | None, limit: int, offset: int
) -> dict[str, Any]:
    """Call Data Repository ``GET /api/models`` and return its body."""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if search:
        params["search"] = search
    return await _request_data_repository("GET", "/api/models", params=params)


async def _list_data_repository_model_versions(name: str) -> dict[str, Any]:
    """Call Data Repository ``GET /api/models/{name}/versions`` (S-048)."""
    return await _request_data_repository("GET", f"/api/models/{name}/versions")


# ── Solar enrichment ──────────────────────────────────────────


async def _fetch_models_from_host(host: Host) -> tuple[list[dict], bool]:
    """Fetch ``GET {host.url}/models`` like S-020, reporting reachability.

    Returns ``(models, ok)``; ``ok`` is False when the host did not
    answer with 200 (unreachable, timeout, or error status).
    """
    url = f"{host.url.rstrip('/')}/models"
    headers = {"X-API-Key": host.api_key}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp,
        ):
            if resp.status == 200:
                return await resp.json(), True
            logger.warning(
                "Host %s (%s) returned %d for GET /models",
                host.id,
                host.url,
                resp.status,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to fetch models from host %s: %s", host.id, e)
    return [], False


async def _collect_availability() -> tuple[
    dict[str, list[DeployedHostInfo]],
    dict[str, list[DeployedHostInfo]],
    Literal["ok", "partial", "unavailable"],
]:
    """Aggregate host-level model availability (same mechanism as S-020).

    Returns ``(model_name -> [DeployedHostInfo], name:version -> [DeployedHostInfo],
    enrichment_status)``. Host entries are keyed by the authoritative
    ``model_name`` recorded at pull time (D-016), falling back to the
    manifest ``name`` for legacy entries. The version-level map (S-048)
    keys on ``name:version`` from the manifest ``version`` field; legacy
    entries without a version contribute only to the model-level map.
    """
    hosts = await host_db.get_all_hosts()
    results = await asyncio.gather(*[_fetch_models_from_host(h) for h in hosts])

    by_name: dict[str, list[DeployedHostInfo]] = {}
    by_version: dict[str, list[DeployedHostInfo]] = {}
    failed = 0
    for host, (models, ok) in zip(hosts, results):
        if not ok:
            failed += 1
            continue
        for m in models:
            key = m.get("model_name") or m.get("name")
            if not key:
                continue
            info = DeployedHostInfo(
                host_id=host.id,
                host_name=host.name,
                size_bytes=m.get("size_bytes", 0),
                path=m.get("path", ""),
            )
            by_name.setdefault(key, []).append(info)
            version = m.get("version")
            if version:
                by_version.setdefault(f"{key}:{version}", []).append(info)

    status: Literal["ok", "partial", "unavailable"]
    if failed == 0:
        status = "ok"
    elif failed == len(hosts) and hosts:
        status = "unavailable"
    else:
        status = "partial"
    return by_name, by_version, status


def _model_name_from_source(source_uri: str | None) -> str | None:
    """Extract the Data Repository model name from a model source URI.

    ``repo://name:version/...`` -> ``name``; ``huggingface://org/model``
    -> ``org/model``; anything unparsable -> None.
    Shared implementation lives in the delete service module (S-048).
    """
    if not source_uri:
        return None
    return catalog_delete._model_name_from_source(source_uri)


async def _collect_running_instances() -> dict[str, list[RunningInstanceInfo]]:
    """Aggregate running instances from each host's cached instance state.

    Instances are joined to catalog models through their ``model_source``.
    The cache is best-effort: hosts that never connected contribute
    nothing, and a missing cache is indistinguishable from "no instances".
    """
    instances = await catalog_delete.collect_running_instances()
    by_name: dict[str, list[RunningInstanceInfo]] = {}
    for inst in instances:
        if inst.name is None:
            continue
        by_name.setdefault(inst.name, []).append(
            RunningInstanceInfo(
                host_id=inst.host_id,
                host_name=inst.host_name,
                instance_id=inst.instance_id,
            )
        )
    return by_name


def _derive_status(
    running: list[RunningInstanceInfo],
    deployed: list[DeployedHostInfo],
    availability_ok: bool,
) -> Literal["available", "deployed", "unavailable", "unknown"]:
    """Derive the WebUI-facing deployment status for a catalog model.

    Evidence-based, never misleading: running instances prove
    availability and deployed hosts prove deployment. When the
    availability source itself is down and no instance evidence exists,
    the status is ``unknown`` instead of a false ``unavailable``.
    """
    if running:
        return "available"
    if deployed:
        return "deployed"
    return "unavailable" if availability_ok else "unknown"


# ── Route ─────────────────────────────────────────────────────


@router.get("/models", response_model=CatalogResponse)
async def get_catalog_models(
    search: str | None = Query(
        None, description="Search string forwarded to Data Repository"
    ),
    limit: int = Query(50, ge=1, le=1000, description="Results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
) -> CatalogResponse:
    """List Data Repository models enriched with Solar deployment context.

    Pagination and ``search`` are forwarded verbatim to Data Repository
    (D-013); the response keeps its ``total``/``items`` shape. Each item
    carries a ``solar`` block with the derived deployment status, running
    instance count, deployed hosts, and running instances.
    """
    repo_listing = await _list_data_repository_models(search, limit, offset)
    raw_items = repo_listing.get("items", [])
    total = repo_listing.get("total", len(raw_items))

    # Enrichment is best-effort; failures degrade metadata, never the response.
    availability, _by_version, enrichment_status = await _collect_availability()
    running = await _collect_running_instances()
    availability_ok = enrichment_status == "ok"

    items: list[CatalogModelItem] = []
    for raw in raw_items:
        name = raw.get("name")
        if not name:
            continue
        deployed = availability.get(name, [])
        instances = running.get(name, [])
        items.append(
            CatalogModelItem(
                name=name,
                category=raw.get("category", "model"),
                description=raw.get("description"),
                versions_count=raw.get("versions_count", 0),
                latest_version=raw.get("latest_version"),
                created_at=raw.get("created_at"),
                solar=SolarRuntimeInfo(
                    status=_derive_status(instances, deployed, availability_ok),
                    running_instances=len(instances),
                    deployed_hosts=deployed,
                    instances=instances,
                ),
            )
        )

    return CatalogResponse(
        total=total,
        items=items,
        meta=CatalogMeta(enrichment=enrichment_status),
    )


# ── Version listing and deletion (S-048) ──────────────────────


@router.get("/models/{name}/versions", response_model=CatalogModelVersionsResponse)
async def get_catalog_model_versions(name: str) -> CatalogModelVersionsResponse:
    """List Data Repository versions enriched with per-version Solar context.

    Each version carries a ``solar`` block with the running instances and
    deployed hosts that would block its deletion, so the WebUI can show
    blockers before the user attempts a delete.
    """
    listing = await _list_data_repository_model_versions(name)
    raw_versions = listing.get("versions", [])

    _by_name, by_version, _status = await _collect_availability()
    running = await catalog_delete.collect_running_instances()
    newest = raw_versions[0].get("version") if raw_versions else None

    items: list[CatalogModelVersionItem] = []
    for raw in raw_versions:
        version = raw.get("version")
        if not version:
            continue
        blockers = [
            i
            for i in running
            if catalog_delete._instance_serves_version(i, name, version, newest)
        ]
        items.append(
            CatalogModelVersionItem(
                version=version,
                harbor_ref=raw.get("harbor_ref", ""),
                created_at=raw.get("created_at"),
                size_bytes=raw.get("size_bytes"),
                checksum=raw.get("checksum"),
                solar=VersionSolarRuntimeInfo(
                    running_instances=len(blockers),
                    deployed_hosts=by_version.get(f"{name}:{version}", []),
                ),
            )
        )

    return CatalogModelVersionsResponse(versions=items)


@router.delete("/models/{name}/versions/{version}", status_code=204)
async def delete_catalog_model_version(
    name: str,
    version: str,
) -> Response:
    """Delete one model version: Harbor first, then unregister (S-048)."""
    await catalog_delete.build_catalog_delete_service().delete_version(name, version)
    return Response(status_code=204)


@router.delete("/models/{name}", response_model=catalog_delete.DeleteArtifactResult)
async def delete_catalog_model(name: str) -> catalog_delete.DeleteArtifactResult:
    """Delete every version of a model, then the artifact row (S-048).

    Returns per-version results: versions whose Harbor delete succeeded are
    unregistered; when every version is clean the artifact row is removed
    and a best-effort repository delete is attempted.
    """
    return await catalog_delete.build_catalog_delete_service().delete_artifact(name)
