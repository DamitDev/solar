"""Shared placement policy helper (S-038 / S-041).

Implements the placement algorithm from docs/specs/deployment-intent.md §8.4.
Both the reservation coordinator and the intent reconciler use this module
so there is ONE placement policy, not two.
"""

import logging
from typing import Any

from app.models import Host, HostResourceSnapshot
from app.redis_state import host_store

logger = logging.getLogger(__name__)

# Priority ordering for displacement (deployment-intent.md §4.3)
PRIORITY_ORDER: dict[str, int] = {
    "ephemeral": 0,
    "staging": 1,
    "production": 2,
}


def _has_roles(host: Host, required_roles: list[str]) -> bool:
    """Check that *host* has all required roles."""
    host_roles = host.roles or []
    return all(r in host_roles for r in required_roles)


def supports_backend(host: Host, backend_type: str | None) -> bool:
    """Whether *host* can run *backend_type*.

    A host that has not advertised its backends (empty list — it predates
    advertisement, or has not reconnected since) is treated as having no
    opinion, so the fleet keeps working exactly as before while the roster
    catches up. Only a host that did advertise can be excluded.
    """
    if backend_type is None:
        return True
    advertised = host.supported_backends or []
    return not advertised or backend_type in advertised


def filter_durable_hosts(
    hosts: list[Host],
    *,
    roles: list[str],
    gpu_type: str | None = None,
    host_allow: list[str] | None = None,
    host_deny: list[str] | None = None,
    backend_type: str | None = None,
) -> list[Host]:
    """Hosts passing the durable placement filters.

    Durable means roles, gpu_type, allow/deny and backend support: facts
    about how a host is built and provisioned. Draining, reachability and
    resource fit are deliberately NOT part of this filter — they are dynamic
    fleet state that intent validation
    (``app.services.intent_validation``) reports as advisory warnings, never
    as hard errors. ``find_candidates`` applies them afterwards, so both
    paths share one filter chain.
    """
    host_allow_set = set(host_allow) if host_allow else None
    host_deny_set = set(host_deny) if host_deny else None

    result: list[Host] = []
    for host in hosts:
        # Role filter
        if not _has_roles(host, roles):
            continue

        # GPU type filter
        if gpu_type is not None and host.gpu_type != gpu_type:
            continue

        # Backend support (e.g. SGLang is a separate install, not everywhere)
        if not supports_backend(host, backend_type):
            continue

        # Allow/deny lists
        if host_allow_set is not None and host.id not in host_allow_set:
            continue
        if host_deny_set is not None and host.id in host_deny_set:
            continue

        result.append(host)
    return result


def gpu_headroom_by_device(
    snapshot: HostResourceSnapshot,
) -> tuple[dict[int, float], float]:
    """Per-device S-034 reservation headroom, plus the un-attributed remainder (L7).

    For each reservation with a resolved device set the per-device charge is
    ``max(vram_gb − actual_share, 0)`` with ``actual_share = actual_vram_gb /
    len(gpu_ids)`` — the same formula the host's manager derives from its own
    ledger (L1), so the two sides agree. Reservations without ``gpu_ids``
    (legacy callers / old agents) contribute to the L7 remainder, which is
    charged pessimistically to every device by the caller.
    """
    per_device: dict[int, float] = {}
    unattributed = 0.0
    for res in snapshot.reservations:
        ids = res.gpu_ids or []
        if ids:
            actual_share = (res.actual_vram_gb or 0.0) / len(ids)
            charge = max(res.vram_gb - actual_share, 0.0)
            for idx in ids:
                per_device[idx] = per_device.get(idx, 0.0) + charge
        else:
            actual = res.actual_vram_gb or 0.0
            unattributed += max(res.vram_gb * max(res.gpu_count, 1) - actual, 0.0)
    return per_device, unattributed


def _gpu_free_map(
    snapshot: HostResourceSnapshot,
    claims: dict[int, float] | None = None,
) -> dict[int, float]:
    """Live per-device free memory after headroom and claims (S-058).

    ``free = gpu.available_gb − headroom(device) − unattributed (L7) −
    claims(device)``. Shared by :func:`find_gpu_assignment` and the
    ranking key in :func:`find_candidates` so the fit check and the rank
    cannot disagree.
    """
    per_device, unattributed = gpu_headroom_by_device(snapshot)
    claims = claims or {}
    return {
        gpu.index: max(
            0.0,
            gpu.available_gb
            - per_device.get(gpu.index, 0.0)
            - unattributed
            - claims.get(gpu.index, 0.0),
        )
        for gpu in snapshot.gpus
    }


def find_gpu_assignment(
    snapshot: HostResourceSnapshot,
    gpu_count: int,
    vram_gb: float,
    *,
    claims: dict[int, float] | None = None,
) -> list[int] | None:
    """Pick *gpu_count* physical devices that each fit *vram_gb* (S-058).

    Per-device availability is spec §6 literally: live nvidia-smi free minus
    host-side reservation headroom (derived from ``snapshot.reservations[]``,
    L1) minus control-side cold-start claims. Devices qualify when
    ``free >= vram_gb``; among qualifiers the preference is most free first,
    lowest index as tie-break (L6) — ``CUDA_VISIBLE_DEVICES`` is emitted in
    that order, so ``main_gpu: 0`` means "the most-free chosen GPU" and D5's
    renumbering makes backend flags positions in the visible set.

    Returns None when fewer than *gpu_count* devices qualify, or for
    ``gpu_count <= 0`` / an empty ``gpus`` list — callers treat that as
    "use the aggregate path" (D6).
    """
    gpus = getattr(snapshot, "gpus", None)
    if not gpus or gpu_count <= 0:
        return None
    free_by_index = _gpu_free_map(snapshot, claims)
    qualifying = sorted(
        (g for g in gpus if free_by_index[g.index] >= vram_gb),
        key=lambda g: (-free_by_index[g.index], g.index),
    )
    picked = [g.index for g in qualifying[:gpu_count]]
    return picked if len(picked) == gpu_count else None


def max_gpu_count_fittable(
    snapshot: HostResourceSnapshot,
    vram_gb: float,
    *,
    claims: dict[int, float] | None = None,
) -> int:
    """Largest device set *snapshot* can supply, each fitting *vram_gb* (S-058).

    Equals ``gpu_count`` when :func:`find_gpu_assignment` returns a set for
    that count: the qualifying devices under the L6 preference, most free
    first. Used by shortfall reporting to say *how many* devices the best
    eligible host offers.
    """
    gpus = getattr(snapshot, "gpus", None)
    if not gpus or vram_gb <= 0:
        return 0
    free_by_index = _gpu_free_map(snapshot, claims)
    return sum(1 for g in gpus if free_by_index[g.index] >= vram_gb)


def fits_resources(
    snapshot: HostResourceSnapshot,
    vram_gb: float,
    ram_gb: float | None,
    disk_gb: float | None,
    *,
    gpu_count: int = 1,
    gpu_claims: dict[int, float] | None = None,
) -> bool:
    """Check if *snapshot* has sufficient available resources.

    Uses available = total - Σeffective semantics from S-034/S-035.

    S-058: hosts reporting a ``gpus`` list use the per-GPU path exclusively
    — ``find_gpu_assignment`` replaces both the aggregate VRAM comparison
    and the unified-memory fallback; RAM and disk checks are untouched.
    Without a ``gpus`` list the function is byte-for-byte today's behaviour
    (D6).
    """
    if not snapshot.reachable:
        return False

    gpus = getattr(snapshot, "gpus", None)
    if gpus:
        if find_gpu_assignment(snapshot, gpu_count, vram_gb, claims=gpu_claims) is None:
            return False
    else:
        # §8.4: the aggregate path charges vram_gb * gpu_count — a pre-S-058
        # host silently drops the gpu_count we send it (no such field on its
        # ReservationRequest), so this is the only gate for those hosts.
        if (
            snapshot.vram_available_gb is not None
            and snapshot.vram_available_gb < vram_gb * max(gpu_count, 1)
        ):
            return False

        # Unified-memory fallback: a host without a VRAM dimension (Mac, or
        # a CPU-only box) runs the model in system RAM. The VRAM estimate
        # consumes the same unified memory there, so check it against RAM
        # available.
        if (
            snapshot.vram_available_gb is None
            and snapshot.ram_available_gb is not None
            and snapshot.ram_available_gb < (ram_gb or 0.0) + vram_gb
        ):
            return False

    if (
        ram_gb is not None
        and snapshot.ram_available_gb is not None
        and snapshot.ram_available_gb < ram_gb
    ):
        return False

    return not (
        disk_gb is not None
        and snapshot.disk_available_gb is not None
        and snapshot.disk_available_gb < disk_gb
    )


async def find_candidates(
    hosts: list[Host],
    snapshots: dict[str, HostResourceSnapshot],
    *,
    roles: list[str],
    gpu_type: str | None = None,
    host_allow: list[str] | None = None,
    host_deny: list[str] | None = None,
    backend_type: str | None = None,
    vram_gb: float,
    ram_gb: float | None = None,
    disk_gb: float | None = None,
    exclude_alias: str | None = None,
    gpu_count: int = 1,
    gpu_claims: dict[str, dict[int, float]] | None = None,
) -> list[tuple[Host, HostResourceSnapshot]]:
    """Find candidate hosts matching placement constraints.

    Returns candidates ranked by: VRAM available to this request → most
    free disk → fewest instances → host id. The first ``(host, snapshot)``
    pair is the best choice.

    S-058: per-GPU hosts (a non-empty ``snapshot.gpus``) are fit-checked per
    device via ``find_gpu_assignment``, and their ranking key is the sum of
    free memory over the *chosen* device set — aggregate free is meaningless
    for fit on a multi-GPU box. Hosts without a ``gpus`` list keep today's
    aggregate key (D6). When ``gpu_claims`` is not supplied the control-side
    cold-start claim ledger is fetched lazily per host (``reservation``
    imports this module, so the import lives inside the function body).

    Implements deployment-intent.md §8.4 placement policy, including the
    draining-host exclusion from host-draining.md §4.1.
    """
    candidates: list[tuple[Host, HostResourceSnapshot]] = []

    # Durable filters first (shared with intent validation via
    # filter_durable_hosts), then the dynamic ones.
    durable = filter_durable_hosts(
        hosts,
        roles=roles,
        gpu_type=gpu_type,
        host_allow=host_allow,
        host_deny=host_deny,
        backend_type=backend_type,
    )

    # S-058: per-host control-side GPU claims (injected by tests/reconciler,
    # otherwise lazily fetched), and the per-host ranking key.
    claims_by_host: dict[str, dict[int, float]] = dict(gpu_claims or {})
    request_vram_key: dict[str, float] = {}

    for host in durable:
        # Draining hosts are being emptied — never place new work there,
        # and never make one the target of another host's evacuation
        # (host-draining.md §4.1). Applied here so intent reconciliation and
        # the S-038 reservation coordinator share the same rule.
        if host.drain_state is not None:
            continue

        # Need a resource snapshot
        snap = snapshots.get(host.id)
        if snap is None:
            continue

        # S-058: fetch the cold-start GPU claims for per-GPU hosts.
        host_claims = claims_by_host.get(host.id)
        if host_claims is None and getattr(snap, "gpus", None):
            from app.services.reservation import gpu_claims_by_device

            host_claims = await gpu_claims_by_device(host.id, snap)
            claims_by_host[host.id] = host_claims

        # Resource fit
        if not fits_resources(
            snap,
            vram_gb,
            ram_gb,
            disk_gb,
            gpu_count=gpu_count,
            gpu_claims=host_claims,
        ):
            continue

        # Per-GPU hosts: remember the VRAM available to *this request* for
        # the ranking key (computed once here, reused by the sort below).
        if getattr(snap, "gpus", None):
            free_map = _gpu_free_map(snap, host_claims)
            assignment = find_gpu_assignment(
                snap, gpu_count, vram_gb, claims=host_claims
            )
            if assignment is not None:
                request_vram_key[host.id] = sum(free_map[i] for i in assignment)

        # One-replica-per-host check (if alias provided)
        if exclude_alias is not None:
            instances = await host_store.get_host_instances(host.id)
            conflict = any(
                (i.get("config", i).get("alias") == exclude_alias) for i in instances
            )
            if conflict:
                continue

        candidates.append((host, snap))

    # Rank: VRAM available to this request → most free disk → fewest
    # running instances → host id (stable tiebreak) (§8.4). The primary key
    # is the per-GPU free sum for per-GPU hosts, the aggregate
    # vram_available_gb otherwise (unchanged for hosts without a gpus list).
    candidates.sort(
        key=lambda pair: (
            -request_vram_key.get(pair[0].id, pair[1].vram_available_gb or 0.0),
            -(pair[1].disk_available_gb or 0),
            pair[1].running_instance_count,
            pair[0].id,
        )
    )

    return candidates


def can_displace(
    candidate_priority: str,
    existing_priority: str,
) -> bool:
    """Check if *candidate_priority* may displace *existing_priority*.

    Displacement is allowed only toward strictly lower priority.
    production never displaced; equal priority never displaced.
    (deployment-intent.md §8.5)
    """
    candidate_order = PRIORITY_ORDER.get(candidate_priority)
    existing_order = PRIORITY_ORDER.get(existing_priority)

    if candidate_order is None or existing_order is None:
        return False

    return candidate_order > existing_order


async def find_displaceable_instances(
    host_id: str,
    request_priority: str,
    *,
    preserve_alias: str | None = None,
) -> list[dict[str, Any]]:
    """Find instances on *host_id* that could be displaced by *request_priority*.

    Returns instances eligible for migration, sorted lowest-priority first.
    Respects the one-replica-per-host rule: if *preserve_alias* is set,
    instances with that alias are only displaceable if more than one replica
    of that alias exists on this host.
    """
    instances = await host_store.get_host_instances(host_id)

    displaceable: list[dict[str, Any]] = []
    alias_counts: dict[str, int] = {}

    for inst in instances:
        cfg = inst.get("config", inst)
        alias = cfg.get("alias")
        if alias:
            alias_counts[alias] = alias_counts.get(alias, 0) + 1

    for inst in instances:
        cfg = inst.get("config", inst)
        priority = cfg.get("priority") or inst.get("priority", "production")
        alias = cfg.get("alias")

        # Check one-replica preservation
        if (
            preserve_alias
            and alias == preserve_alias
            and alias_counts.get(alias, 0) <= 1
        ):
            continue  # Must preserve at least one replica

        if can_displace(request_priority, priority):
            inst["_priority"] = priority
            displaceable.append(inst)

    # Sort by lowest priority first (ephemeral before staging)
    displaceable.sort(key=lambda i: PRIORITY_ORDER.get(i.get("_priority", ""), 99))

    return displaceable
