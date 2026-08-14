"""Fleet-aware intent validation (C3).

Pure rules (field ownership, device contract, gpu_type vocabulary) live in
``app.validation`` and are synchronous. This module adds the checks that
need the host roster (DB) and resource snapshots (Redis WS cache via
``_fetch_host_resource_snapshot``), and separates *hard* 422s (durable,
static facts) from *advisory warnings* (momentary fleet state).

The hard/warning split is the crux: a temporarily offline host must never
make a production intent uneditable, so anything derived from dynamic fleet
state degrades to a warning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.validation import normalize_gpu_type

logger = logging.getLogger(__name__)


def _backend_type(data: dict[str, Any]) -> str | None:
    """The payload's ``backend.backend_type``, or None when it is not set."""
    backend = data.get("backend")
    if not isinstance(backend, dict):
        return None
    backend_type = backend.get("backend_type")
    return backend_type if isinstance(backend_type, str) and backend_type else None


async def _fleet_state(
    data: dict[str, Any],
    hosts: list[Any],
) -> tuple[list[Any], dict[str, Any], list[Any]]:
    """Return (candidates, snapshots, durable) for the intent payload.

    ``candidates`` comes from the real placement chain (``find_candidates``)
    so validation and placement cannot disagree; ``durable`` is the shared
    durable-filter subset (roles/gpu_type/allow/deny, via
    ``placement.filter_durable_hosts``) used for the capacity and
    drain/unreachable warnings.

    The roster is passed in rather than re-read: the caller already needed it
    for the hard half, and a second query could see a different fleet than the
    one the hard errors were computed against.
    """
    from app.routes.management.resources import _fetch_host_resource_snapshot
    from app.services.placement import filter_durable_hosts, find_candidates

    placement = data.get("placement") or {}
    resources = data.get("resources") or {}
    gpu_type = normalize_gpu_type(placement.get("gpu_type")) or placement.get(
        "gpu_type"
    )
    roles = list(placement.get("roles") or ["inference"])
    host_allow = list(placement.get("host_allow") or []) or None
    host_deny = list(placement.get("host_deny") or []) or None
    backend_type = _backend_type(data)

    snapshots = {
        s.host_id: s
        for s in await asyncio.gather(
            *[_fetch_host_resource_snapshot(h) for h in hosts]
        )
    }
    durable = filter_durable_hosts(
        hosts,
        roles=roles,
        gpu_type=gpu_type,
        host_allow=host_allow,
        host_deny=host_deny,
        backend_type=backend_type,
    )
    candidates = await find_candidates(
        hosts,
        snapshots,
        roles=roles,
        gpu_type=gpu_type,
        host_allow=host_allow,
        host_deny=host_deny,
        backend_type=backend_type,
        vram_gb=float(resources.get("vram_gb") or 0),
        ram_gb=(
            float(resources["ram_gb"]) if resources.get("ram_gb") is not None else None
        ),
        disk_gb=None,
        exclude_alias=None,
    )
    return candidates, snapshots, durable


def validate_intent_fleet_hard(
    data: dict[str, Any], hosts: list[Any]
) -> list[dict[str, str]]:
    """The hard (422) half of fleet validation, against an existing roster.

    Hard: ``host_allow``/``host_deny`` referencing unknown host ids; a
    ``device`` requiring an accelerator that no host in a non-empty
    ``host_allow`` provides. Both are durable, static facts about the roster,
    so no resource snapshots are needed — which is what lets the reconciler
    reuse its already-fetched hosts instead of re-reading the fleet.
    """
    hard_errors: list[dict[str, str]] = []

    placement = data.get("placement") or {}
    backend = data.get("backend") or {}
    host_allow = list(placement.get("host_allow") or [])
    host_deny = list(placement.get("host_deny") or [])
    host_by_id = {h.id: h for h in hosts}

    # ── Hard: allow/deny must reference known hosts ─────────────
    for hid in host_allow:
        if hid not in host_by_id:
            hard_errors.append(
                {
                    "field": "placement.host_allow",
                    "message": f"host_allow references unknown host id '{hid}'",
                }
            )
    for hid in host_deny:
        if hid not in host_by_id:
            hard_errors.append(
                {
                    "field": "placement.host_deny",
                    "message": f"host_deny references unknown host id '{hid}'",
                }
            )

    # ── Hard: device vs explicit allow-list accelerators ────────
    device = backend.get("device") if isinstance(backend, dict) else None
    if device in ("cuda", "mps") and host_allow:
        required = "nvidia_cuda" if device == "cuda" else "apple_mps"
        if not any(
            host_by_id[hid].gpu_type == required
            for hid in host_allow
            if hid in host_by_id
        ):
            hard_errors.append(
                {
                    "field": "backend.device",
                    "message": (
                        f"device '{device}' requires gpu_type '{required}', "
                        "but none of the hosts in placement.host_allow "
                        "provides it"
                    ),
                }
            )

    return hard_errors


async def validate_intent_fleet(
    data: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return (hard_errors, warnings) using the host roster and snapshots.

    Hard (422): see :func:`validate_intent_fleet_hard`.

    Advisory: ``replicas`` above the currently eligible host count;
    ``resources.vram_gb``/``ram_gb`` above the largest capacity among
    eligible hosts; every eligible host draining or unreachable; a valid
    ``gpu_type`` that no connected host currently reports.

    This is the write-path entry point. It reads resource snapshots for every
    host, so it belongs on create/update — not on a reconcile tick.
    """
    warnings: list[dict[str, str]] = []

    from app.database.hosts import host_db
    from app.redis_state import host_store

    placement = data.get("placement") or {}
    resources = data.get("resources") or {}
    replicas = data.get("replicas", 1)
    gpu_type = normalize_gpu_type(placement.get("gpu_type")) or placement.get(
        "gpu_type"
    )
    vram_gb = float(resources.get("vram_gb") or 0)
    ram_gb = resources.get("ram_gb")

    hosts = await host_db.get_all_hosts()
    hard_errors = validate_intent_fleet_hard(data, hosts)

    if hard_errors:
        # Hard violations short-circuit the advisory half: the spec is
        # rejected anyway, and the warnings would only add noise.
        return hard_errors, []

    # ── Advisory: dynamic fleet state never blocks an edit ──────
    try:
        candidates, snapshots, durable = await _fleet_state(data, hosts)
    except Exception:
        logger.warning("Fleet validation could not compute eligibility", exc_info=True)
        return hard_errors, warnings

    # replicas above the eligible host count
    if isinstance(replicas, int) and replicas > len(candidates):
        warnings.append(
            {
                "field": "replicas",
                "message": (
                    f"{replicas} replicas requested, but only {len(candidates)} "
                    "host(s) can currently accept this intent (after role, "
                    "gpu_type, allow/deny and drain filtering)"
                ),
            }
        )

    # resource request above the largest capacity among durably eligible hosts
    durable_with_snap = [(h, snapshots[h.id]) for h in durable if h.id in snapshots]
    if durable_with_snap:
        largest_vram = 0.0
        largest_ram = 0.0
        for _host, snap in durable_with_snap:
            # Unified-memory hosts have no VRAM dimension; a VRAM request
            # consumes unified RAM there (same rule as fits_resources).
            eff_vram = (
                snap.vram_available_gb
                if snap.vram_available_gb is not None
                else (snap.ram_available_gb or 0.0)
            )
            largest_vram = max(largest_vram, eff_vram or 0.0)
            largest_ram = max(largest_ram, snap.ram_available_gb or 0.0)
        if vram_gb > largest_vram:
            warnings.append(
                {
                    "field": "resources.vram_gb",
                    "message": (
                        f"requests {vram_gb:.1f} GB VRAM, but the largest "
                        f"available among eligible hosts is {largest_vram:.1f} GB"
                    ),
                }
            )
        if ram_gb is not None and float(ram_gb) > largest_ram:
            warnings.append(
                {
                    "field": "resources.ram_gb",
                    "message": (
                        f"requests {float(ram_gb):.1f} GB RAM, but the largest "
                        f"available among eligible hosts is {largest_ram:.1f} GB"
                    ),
                }
            )

    # every eligible host draining or unreachable
    if durable and not candidates:
        all_blocked = all(
            h.drain_state is not None
            or h.id not in snapshots
            or not snapshots[h.id].reachable
            for h in durable
        )
        if all_blocked:
            warnings.append(
                {
                    "field": "replicas",
                    "message": (
                        "every eligible host is currently draining, unreachable "
                        "or lacks a resource snapshot — the intent will stay "
                        "unplaced until fleet state recovers"
                    ),
                }
            )

    # a valid gpu_type that no connected host currently reports
    connected_ids: set[str] = set()
    if gpu_type or _backend_type(data):
        try:
            connected_ids = set(await host_store.get_connected_host_ids())
        except Exception:  # noqa: BLE001
            connected_ids = set()

    if gpu_type:
        reported = {h.gpu_type for h in hosts if h.id in connected_ids}
        if gpu_type not in reported:
            warnings.append(
                {
                    "field": "placement.gpu_type",
                    "message": (
                        f"gpu_type '{gpu_type}' is not reported by any "
                        "currently connected host"
                    ),
                }
            )

    # a backend no connected host advertises (SGLang is a separate install, so
    # this is the common way an otherwise valid intent stays unplaced). Hosts
    # that advertise nothing are skipped: their silence is not a denial.
    backend_type = _backend_type(data)
    if backend_type:
        advertised = {
            backend
            for h in hosts
            if h.id in connected_ids
            for backend in (h.supported_backends or [])
        }
        if advertised and backend_type not in advertised:
            warnings.append(
                {
                    "field": "backend.backend_type",
                    "message": (
                        f"backend '{backend_type}' is not supported by any "
                        "currently connected host"
                    ),
                }
            )

    return hard_errors, warnings
