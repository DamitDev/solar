"""Aggregated resource query API (S-035).

GET /api/resources — cluster-wide view of host capacity, workloads, and reservations.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp
from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.database.hosts import host_db
from app.models import (
    AggregatedResourceResponse,
    Host,
    HostInstanceSummary,
    HostReservationSummary,
    HostResourceSnapshot,
)
from app.redis_state import host_store
from app.redis_state.freshness import entry_age_s
from app.services.host_status import get_host_active_jobs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/resources", tags=["resources"])

_RESOURCE_TIMEOUT = 5  # seconds to wait for a host's /resources response


def _merge_resource_payload(
    base: HostResourceSnapshot, data: dict[str, Any]
) -> HostResourceSnapshot:
    """Merge a host resource payload into *base* and return it.

    Pure and unit-testable. Both the WS health push and the HTTP fallback
    produce the same payload shape (vram/ram/disk dimensions, reservations),
    so a WS snapshot and the equivalent HTTP body merge identically (C5).

    The payload's ``memory_type`` is deliberately dropped: control models the
    dimensions explicitly (``vram_*``/``ram_*``), so which one the host calls
    primary has no field on ``HostResourceSnapshot`` to land in.
    """
    for dim_name in ("vram", "ram", "disk"):
        dim = data.get(dim_name)
        if dim is None:
            continue
        setattr(base, f"{dim_name}_total_gb", dim.get("total_gb"))
        setattr(base, f"{dim_name}_system_used_gb", dim.get("system_used_gb"))
        setattr(
            base, f"{dim_name}_reserved_headroom_gb", dim.get("reserved_headroom_gb")
        )
        setattr(base, f"{dim_name}_reported_used_gb", dim.get("reported_used_gb"))
        setattr(base, f"{dim_name}_available_gb", dim.get("available_gb"))

    # Merge reservation details + totals
    reservations = data.get("reservations", [])
    if not isinstance(reservations, list):
        reservations = []
    base.reservation_count = len(reservations)
    base.reservation_vram_total_gb = sum(
        float(r.get("vram_gb", 0)) for r in reservations
    )
    base.reservation_ram_total_gb = sum(float(r.get("ram_gb", 0)) for r in reservations)
    base.reservation_disk_total_gb = sum(
        float(r.get("disk_gb") or 0) for r in reservations
    )
    base.reservations = [
        HostReservationSummary(
            id=r["id"],
            job_id=str(r.get("job_id", "")),
            workload_type=str(r.get("workload_type", "training")),
            status=str(r.get("status", "pending")),
            vram_gb=float(r.get("vram_gb") or 0),
            ram_gb=float(r.get("ram_gb") or 0),
            disk_gb=float(r["disk_gb"]) if r.get("disk_gb") is not None else None,
            actual_vram_gb=(
                float(r["actual_vram_gb"])
                if r.get("actual_vram_gb") is not None
                else None
            ),
            actual_ram_gb=(
                float(r["actual_ram_gb"])
                if r.get("actual_ram_gb") is not None
                else None
            ),
            actual_disk_gb=(
                float(r["actual_disk_gb"])
                if r.get("actual_disk_gb") is not None
                else None
            ),
            expires_at=r.get("expires_at"),
        )
        for r in reservations
        if r.get("id")
    ]

    # Active training job-step consumption: Σ actual usage of running
    # reservations (pending reservations have no actuals yet — their full
    # requested amount is already captured in reserved_headroom).
    base.vram_training_used_gb = sum(
        float(r.get("actual_vram_gb") or 0) for r in reservations
    )
    base.ram_training_used_gb = sum(
        float(r.get("actual_ram_gb") or 0) for r in reservations
    )
    base.disk_training_used_gb = sum(
        float(r.get("actual_disk_gb") or 0) for r in reservations
    )

    return base


async def _read_fresh_ws_snapshot(
    host_id: str,
) -> tuple[dict[str, Any], str] | None:
    """Return (resources, at) when a fresh WS-pushed snapshot exists.

    The snapshot counts as fresh when the host is connected and the entry
    is younger than ``settings.host_snapshot_max_age_s`` (three health
    ticks). Returns None otherwise — the caller falls back to HTTP.
    """
    try:
        entry = await host_store.get_host_resource_snapshot(host_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(entry, dict):
        return None
    at = entry.get("at")
    resources = entry.get("resources")
    if not isinstance(resources, dict) or not isinstance(at, str):
        return None
    # Shared with the reconciler's pull-progress freshness check: an unusable
    # stamp reads as None there and must read as "not fresh" here, so the two
    # cannot drift into disagreeing about what a stale entry is.
    age = entry_age_s(at)
    if age is None or age > settings.host_snapshot_max_age_s:
        return None
    return resources, at


async def _fetch_host_resource_snapshot(
    host: Host,
) -> HostResourceSnapshot:
    """Fetch live resource data from a single solar-host.

    Cache-first (C5): when the host is connected over the WS channel and
    pushed a resource snapshot younger than ``settings.host_snapshot_max_age_s``,
    the Redis copy is used and no HTTP call is made. Otherwise proxies
    GET /resources from the host. On any error (connection, timeout,
    non-200), marks the host as unreachable and returns a degraded snapshot
    with DB-only data. ``snapshot_source`` reports which path was taken.
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # Base snapshot from local DB (always available)
    base = HostResourceSnapshot(
        host_id=host.id,
        host_name=host.name,
        url=host.url,
        status=host.status,
        drain_state=host.drain_state,
        roles=host.roles or [],
        gpu_type=host.gpu_type,
        version=host.version,
        reachable=False,
        snapshot_timestamp=now_iso,
        snapshot_source="none",
    )

    # Try to get instances from Redis
    try:
        instances = await host_store.get_host_instances(host.id)
        base.instance_count = len(instances)
        base.running_instance_count = sum(
            1 for i in instances if i.get("status") == "running"
        )
        base.instances = [
            HostInstanceSummary(
                id=i["id"],
                alias=i.get("alias"),
                status=i.get("status"),
                backend_type=i.get("backend_type"),
                port=i.get("port"),
                supported_endpoints=list(i.get("supported_endpoints") or []),
                # Ownership markers let consumers tell intent-managed
                # replicas from manual instances (S-043 §6).
                managed_by=i.get("managed_by"),
                intent_id=i.get("intent_id"),
            )
            for i in instances
            if i.get("id")
        ]
    except Exception:
        logger.warning(
            "Failed to fetch instances from Redis for host %s",
            host.id,
            exc_info=True,
        )

    # Aggregate active job workloads from the jobs table
    base.active_jobs = await get_host_active_jobs(host.id)

    # ── WS-first: serve the Redis snapshot when it is fresh (C5) ──
    try:
        connected = await host_store.is_host_connected(host.id)
    except Exception:  # noqa: BLE001
        connected = False
    if connected:
        cached = await _read_fresh_ws_snapshot(host.id)
        if cached is not None:
            payload, at = cached
            base = _merge_resource_payload(base, payload)
            base.reachable = True
            base.snapshot_timestamp = at
            base.snapshot_source = "ws"
            return base

    # ── HTTP fallback: proxy live resource data from the host ──
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url.rstrip('/')}/resources"
            headers = {"X-API-Key": host.api_key}
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_RESOURCE_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    base.error = (
                        f"Host {host.name} at {host.url} returned HTTP {resp.status}"
                    )
                    return base

                data = await resp.json()
    except (aiohttp.ClientConnectionError, aiohttp.ClientConnectorError):
        base.error = f"Host unreachable at {host.url}"
        return base
    except asyncio.TimeoutError:
        base.error = f"Host timed out ({_RESOURCE_TIMEOUT}s)"
        return base
    except Exception as exc:  # noqa: BLE001
        base.error = f"Failed to fetch resources: {exc}"
        return base

    # Merge live resource dimensions
    base.reachable = True
    base.snapshot_timestamp = now_iso
    base.snapshot_source = "http"
    return _merge_resource_payload(base, data)


@router.get("", response_model=AggregatedResourceResponse)
async def get_resources(
    host_id: str | None = Query(None, description="Filter to a specific host"),
    role: str | None = Query(
        None, description="Filter by host role (e.g. 'training', 'inference')"
    ),
    gpu_type: str | None = Query(
        None, description="Filter by GPU type (e.g. 'nvidia_cuda')"
    ),
    min_available_vram_gb: float | None = Query(
        None, description="Minimum available VRAM in GB"
    ),
    min_available_ram_gb: float | None = Query(
        None, description="Minimum available RAM in GB"
    ),
) -> AggregatedResourceResponse:
    """Return aggregated cluster-wide resource view.

    Fetches live resource snapshots from every known host and merges
    with locally stored metadata.  Unreachable hosts are included in
    the response with ``reachable=False`` and an error string instead
    of failing the entire request.

    The resource availability formula follows S-034 semantics:
    ``available = total - (system_used + reserved_headroom)`` where
    ``reserved_headroom = Σ max(reserved − actual, 0)`` per reservation.
    This correctly implements ``effective = max(actual, requested)`` —
    real consumption is never double-counted.

    Per-host finer details (U-004): ``instances`` lists the inference
    workloads (with aliases) behind ``system_used``; ``reservations``
    carries per-reservation details (owner ``job_id``, requested vs
    actual per dimension); ``*_training_used_gb`` is the portion of
    ``system_used`` consumed by active training job steps (Σ actuals of
    running reservations).
    """
    if isinstance(host_id, str):
        host = await host_db.get_host(host_id)
        if not host:
            raise HTTPException(status_code=404, detail="Host not found")
        hosts = [host]
    else:
        hosts = await host_db.get_all_hosts(
            role=role if isinstance(role, str) else None
        )

    # Fetch all host snapshots concurrently
    snapshots: list[HostResourceSnapshot] = await asyncio.gather(
        *[_fetch_host_resource_snapshot(h) for h in hosts]
    )

    # Apply response-level filters
    if isinstance(gpu_type, str):
        snapshots = [
            s
            for s in snapshots
            if s.gpu_type and s.gpu_type.lower() == gpu_type.lower()
        ]

    if isinstance(min_available_vram_gb, (int, float)):
        snapshots = [
            s
            for s in snapshots
            if s.reachable
            and s.vram_available_gb is not None
            and s.vram_available_gb >= min_available_vram_gb
        ]

    if isinstance(min_available_ram_gb, (int, float)):
        snapshots = [
            s
            for s in snapshots
            if s.reachable
            and s.ram_available_gb is not None
            and s.ram_available_gb >= min_available_ram_gb
        ]

    reachable = sum(1 for s in snapshots if s.reachable)
    unreachable = sum(1 for s in snapshots if not s.reachable)

    return AggregatedResourceResponse(
        hosts=snapshots,
        total_hosts=len(snapshots),
        reachable_hosts=reachable,
        unreachable_hosts=unreachable,
    )
