"""Host draining (S-043).

Draining is a durable host state, not an orchestrated job: the API marks
a host ``draining``, placement stops choosing it (see
``app.services.placement``), and the reconciler evacuates its managed
replicas one action per tick. This module owns the parts that sit outside
the reconciler:

- classifying a host's instances as intent-managed or manual,
- the preflight that decides whether a drain may start at all,
- the drain status view the API and WebUI read,
- the stall reasons the reconciler records when a replica cannot move,
- the ``draining`` → ``drained`` promotion.

Specification: docs/specs/host-draining.md
"""

import logging
from typing import Any

from app.models import DrainBlocker, DrainReplica, DrainState, Host, HostDrainStatus
from app.redis_state.connection import redis_client

logger = logging.getLogger(__name__)

# Per-replica stall reasons. Written by the reconciler when an evacuation
# has no target, read by the drain status endpoint. TTL'd so a reason can
# never outlive its relevance (a moved replica leaves nothing behind).
_STALL_PREFIX = "solar:drain:stall:"
_STALL_TTL = 300

# Guards the draining → drained promotion so concurrent Solar Control
# replicas do not both broadcast it.
_SWEEP_LOCK = "solar:drain:sweep"
_SWEEP_LOCK_TTL = 10

# Instance states in which nothing is executing on the host. Anything else
# (running, starting, …) counts as occupying the host.
_INACTIVE_STATES = {"stopped", "failed", "error", "exited", "created", "pending"}


# ── Instance helpers ───────────────────────────────────────────


def _config(instance: dict[str, Any]) -> dict[str, Any]:
    """Return the instance config, tolerating flat and nested cache shapes."""
    cfg = instance.get("config", instance)
    return cfg if isinstance(cfg, dict) else instance


def instance_id(instance: dict[str, Any]) -> str | None:
    return instance.get("instance_id") or instance.get("id")


def instance_alias(instance: dict[str, Any]) -> str | None:
    cfg = _config(instance)
    return cfg.get("alias") or instance.get("alias")


def instance_status(instance: dict[str, Any]) -> str | None:
    return instance.get("status") or instance.get("state")


def owning_intent_id(instance: dict[str, Any]) -> str | None:
    """Return the owning intent id, or None for a manual instance.

    Mirrors the reconciler's ownership test (deployment-intent.md §5.4):
    an instance belongs to an intent only when it carries both
    ``managed_by == "intent"`` and an ``intent_id``.
    """
    cfg = _config(instance)
    managed_by = cfg.get("managed_by") or instance.get("managed_by")
    intent_id = cfg.get("intent_id") or instance.get("intent_id")
    if managed_by == "intent" and intent_id:
        return str(intent_id)
    return None


def is_managed(instance: dict[str, Any]) -> bool:
    return owning_intent_id(instance) is not None


def is_active(instance: dict[str, Any]) -> bool:
    """True when the instance still occupies the host."""
    status = (instance_status(instance) or "").lower()
    return status not in _INACTIVE_STATES


# ── Stall reasons ──────────────────────────────────────────────


def _stall_key(host_id: str, inst_id: str) -> str:
    return f"{_STALL_PREFIX}{host_id}:{inst_id}"


async def record_stall(host_id: str, inst_id: str, reason: str) -> None:
    """Record why *inst_id* cannot be evacuated from *host_id*."""
    try:
        await redis_client().set(_stall_key(host_id, inst_id), reason, ex=_STALL_TTL)
    except Exception:
        logger.debug("Could not record drain stall for %s", inst_id, exc_info=True)


async def clear_stall(host_id: str, inst_id: str) -> None:
    """Drop a previously recorded stall reason."""
    try:
        await redis_client().delete(_stall_key(host_id, inst_id))
    except Exception:
        logger.debug("Could not clear drain stall for %s", inst_id, exc_info=True)


async def get_stalls(host_id: str, instance_ids: list[str]) -> dict[str, str]:
    """Return ``{instance_id: reason}`` for the given instances."""
    if not instance_ids:
        return {}
    try:
        values = await redis_client().mget(
            [_stall_key(host_id, i) for i in instance_ids]
        )
    except Exception:
        logger.debug("Could not read drain stalls for %s", host_id, exc_info=True)
        return {}
    return {
        inst_id: value for inst_id, value in zip(instance_ids, values or []) if value
    }


# ── Preflight and status ───────────────────────────────────────


async def collect_blockers(host: Host) -> list[DrainBlocker]:
    """Return everything that prevents *host* from being drained (§3).

    Running manual instances block because they have no desired state to
    reconcile — Solar Control will not move a workload nobody asked it to
    manage. Active job steps block because ``execute_migration`` refuses a
    source host that has them, so every evacuation would fail.
    """
    from app.redis_state import host_store
    from app.services.migration import active_job_ids_on_host

    blockers: list[DrainBlocker] = []

    try:
        instances = await host_store.get_host_instances(host.id)
    except Exception:
        logger.warning(
            "Could not read instances for host %s during drain preflight",
            host.id,
            exc_info=True,
        )
        instances = []

    for inst in instances:
        inst_id = instance_id(inst)
        if not inst_id or is_managed(inst) or not is_active(inst):
            continue
        alias = instance_alias(inst)
        blockers.append(
            DrainBlocker(
                kind="manual_instance",
                id=inst_id,
                name=alias,
                detail=(
                    f"Manually created instance{f' {alias!r}' if alias else ''} "
                    f"is {instance_status(inst) or 'active'}. Draining never "
                    f"moves manual instances — stop it before draining."
                ),
            )
        )

    for job_id in await active_job_ids_on_host(host):
        blockers.append(
            DrainBlocker(
                kind="active_job",
                id=job_id,
                name=None,
                detail=(
                    "Job step is still active. Instances cannot be migrated "
                    "off a host that is running a job step."
                ),
            )
        )

    return blockers


async def build_drain_status(
    host: Host, *, blockers: list[DrainBlocker] | None = None
) -> HostDrainStatus:
    """Build the drain status view for *host* (§5.4)."""
    from app.redis_state import host_store

    if blockers is None:
        blockers = await collect_blockers(host)

    try:
        instances = await host_store.get_host_instances(host.id)
    except Exception:
        logger.warning(
            "Could not read instances for host %s drain status",
            host.id,
            exc_info=True,
        )
        instances = []

    managed = [i for i in instances if is_managed(i) and instance_id(i)]
    manual_running = sum(
        1 for i in instances if not is_managed(i) and is_active(i) and instance_id(i)
    )

    stalls = await get_stalls(host.id, [str(instance_id(i)) for i in managed])

    replicas = [
        DrainReplica(
            instance_id=str(instance_id(i)),
            alias=instance_alias(i),
            intent_id=owning_intent_id(i),
            status=instance_status(i),
            blocked_reason=stalls.get(str(instance_id(i))),
        )
        for i in managed
    ]

    stalled = (
        host.drain_state == DrainState.DRAINING
        and bool(replicas)
        and all(r.blocked_reason for r in replicas)
    )

    return HostDrainStatus(
        host_id=host.id,
        host_name=host.name,
        drain_state=host.drain_state,
        drain_requested_at=host.drain_requested_at,
        stalled=stalled,
        managed_remaining=len(replicas),
        manual_running=manual_running,
        replicas=replicas,
        blockers=blockers,
    )


# ── Completion sweep ───────────────────────────────────────────


async def is_host_empty(host_id: str) -> bool:
    """True when nothing managed and nothing running remains on the host.

    Stopped manual instances are allowed to remain: the preflight requires
    them to be stopped, not deleted, and a stopped process holds no GPU.
    """
    from app.redis_state import host_store

    try:
        instances = await host_store.get_host_instances(host_id)
    except Exception:
        logger.warning(
            "Could not read instances for host %s drain sweep",
            host_id,
            exc_info=True,
        )
        return False

    return not any(is_managed(i) or is_active(i) for i in instances)


async def sweep_drained_hosts() -> None:
    """Promote fully evacuated ``draining`` hosts to ``drained`` (§4.4).

    Called once per reconciler tick. Idempotent, and guarded by a short
    Redis lock so only one Solar Control replica broadcasts the change.
    """
    from app.database.hosts import host_db

    try:
        draining = [
            h
            for h in await host_db.list_draining_hosts()
            if h.drain_state == DrainState.DRAINING
        ]
    except Exception:
        logger.warning("Drain sweep could not list hosts", exc_info=True)
        return

    if not draining:
        return

    try:
        acquired = await redis_client().set(
            _SWEEP_LOCK, "1", nx=True, ex=_SWEEP_LOCK_TTL
        )
    except Exception:
        logger.debug("Drain sweep lock unavailable", exc_info=True)
        return
    if not acquired:
        return

    try:
        for host in draining:
            if not await is_host_empty(host.id):
                continue
            updated = await host_db.set_drain_state(host.id, DrainState.DRAINED)
            logger.info("Host '%s' is drained", host.name)
            await broadcast_drain_state(updated or host)
    finally:
        try:
            await redis_client().delete(_SWEEP_LOCK)
        except Exception:
            logger.debug("Could not release drain sweep lock", exc_info=True)


async def broadcast_drain_state(host: Host) -> None:
    """Emit ``host_status`` so WebUI clients see the drain state change.

    Drain state travels on the existing event rather than a new one:
    clients replace their whole host entry when a ``host_status`` arrives.
    """
    from app.redis_state import host_store
    from app.services.host_status import build_host_status_payload
    from app.socketio_app.server import sio

    try:
        connected = await host_store.is_host_connected(host.id)
        payload = await build_host_status_payload(host, connected=connected)
        await sio.emit("host_status", payload.model_dump(), namespace="/webui")
    except Exception:
        logger.debug(
            "Could not broadcast drain state for host %s", host.id, exc_info=True
        )
