"""Snapshot builder for the Routing view.

Composes hosts (status + drain + health), instances per host, instance states,
active requests with server-computed tallies, endpoints, and pending hosts into
one authoritative :class:`RoutingSnapshot`. The same payload is emitted on
WebUI connect and mirrored over REST.

The aggregate arithmetic (inFlight per instance/host/model/endpoint and the
queued/processing/errored totals) previously lived client-side in
``apps/solar-webui/src/components/routing/workload.ts`` / ``graph.ts``; it now
runs here so the Routing page reconciles correctly after a reconnect.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from app.database.endpoints import endpoint_db
from app.database.hosts import host_db
from app.models.host import Host
from app.models.routing_snapshot import (
    ActiveRequestEntry,
    InstanceStateEntry,
    RequestAggregates,
    RoutingHost,
    RoutingSnapshot,
)
from app.redis_state import (
    host_store,
    instance_states_store,
    routing_store,
)

logger = logging.getLogger(__name__)

# request registry entries carry no status; an entry with routing details
# (host/instance) is in flight, one without is still queued for placement.
_ACTIVE_HINT_STATUS = "processing"
_QUEUED_HINT_STATUS = "queued"


def _host_connected(h: Host, connected_ids: set[str]) -> bool:
    return h.id in connected_ids


async def _build_hosts() -> list[RoutingHost]:
    """Compose one :class:`RoutingHost` per DB host, with its instance cache."""
    hosts = await host_db.get_all_hosts()
    if not hosts:
        return []
    connected = set(await host_store.get_connected_host_ids())
    built: list[RoutingHost] = []
    for h in hosts:
        instances = await host_store.get_host_instances(h.id)
        built.append(
            RoutingHost(
                host_id=h.id,
                name=h.name,
                status=h.status.value,
                drain_state=h.drain_state.value if h.drain_state else None,
                connected=_host_connected(h, connected),
                url=h.url,
                last_seen=h.last_seen.isoformat() if h.last_seen else None,
                gpu_type=h.gpu_type,
                roles=h.roles,
                memory=h.memory.model_dump() if h.memory else None,
                disk_total_gb=h.disk_total_gb,
                disk_used_gb=h.disk_used_gb,
                disk_available_gb=h.disk_available_gb,
                memory_available_gb=h.memory_available_gb,
                version=h.version,
                instances=instances,
            )
        )
    return built


async def _build_instance_states() -> list[InstanceStateEntry]:
    """Compose the latest per-instance runtime state as a list of entries."""
    result: list[InstanceStateEntry] = []
    for e in await instance_states_store.get_all():
        result.append(
            InstanceStateEntry(
                host_id=e.get("host_id", ""),
                instance_id=e.get("instance_id", ""),
                timestamp=e.get("timestamp"),
                data=e.get("data", {}),
            )
        )
    return result


def _status_for(entry: dict[str, Any]) -> str:
    """Derive the WebUI-active-state hint for a registry entry.

    A request that has been routed (carries host/instance) is ``processing``;
    anything still awaiting placement is ``queued``. Terminal requests are not
    in the registry (deleted on success/error), so ``errored`` never derives
    from here and stays 0 in the snapshot.
    """
    if entry.get("host_id") or entry.get("instance_id"):
        return _ACTIVE_HINT_STATUS
    return _QUEUED_HINT_STATUS


async def _build_active_requests_and_aggregates() -> (
    tuple[list[ActiveRequestEntry], RequestAggregates]
):
    """Compose active requests and the server-side tallies over them.

    Aggregate keys mirror the WebUI's expectations:
    ``by_instance`` uses ``host:instance`` cell keys, ``by_host``/``by_model``/
    ``by_endpoint`` use their respective identifiers.
    """
    entries = await routing_store.list_requests()

    by_instance: dict[str, int] = {}
    by_host: dict[str, int] = {}
    by_model: dict[str, int] = {}
    by_endpoint: dict[str, int] = {}
    queued = 0
    processing = 0

    requests: list[ActiveRequestEntry] = []
    for e in entries:
        status = _status_for(e)
        if status == _ACTIVE_HINT_STATUS:
            processing += 1
        else:
            queued += 1

        host_id = e.get("host_id")
        instance_id = e.get("instance_id")
        if host_id and instance_id:
            key = f"{host_id}:{instance_id}"
            by_instance[key] = by_instance.get(key, 0) + 1
        if host_id:
            by_host[host_id] = by_host.get(host_id, 0) + 1

        model = e.get("resolved_model") or e.get("model")
        if model:
            by_model[model] = by_model.get(model, 0) + 1

        endpoint = e.get("endpoint")
        if endpoint:
            by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + 1

        requests.append(
            ActiveRequestEntry(
                request_id=e.get("request_id", ""),
                model=e.get("model"),
                resolved_model=e.get("resolved_model"),
                endpoint=e.get("endpoint"),
                host_id=host_id,
                host_name=e.get("host_name"),
                instance_id=instance_id,
                attempt=e.get("attempt"),
                timestamp=e.get("timestamp"),
                status=status,
            )
        )

    aggregates = RequestAggregates(
        by_instance=by_instance,
        by_host=by_host,
        by_model=by_model,
        by_endpoint=by_endpoint,
        queued=queued,
        processing=processing,
        errored=0,
    )
    return requests, aggregates


async def _build_endpoints() -> list[dict[str, Any]]:
    """Compose endpoint records in the ``endpoints_update`` shape."""
    try:
        endpoints = await endpoint_db.get_all_endpoints()
        return [ep.model_dump() for ep in endpoints]
    except Exception:
        logger.warning("Failed to load endpoints for snapshot", exc_info=True)
        return []


async def build_routing_snapshot() -> RoutingSnapshot:
    """Build a fresh, versioned snapshot of fleet runtime state.

    Never raises for a failing data source where it can degrade gracefully
    (endpoints); Redis/DB reads that fail upstream propagate so the caller can
    decide how to surface the outage.
    """
    hosts = await _build_hosts()
    instance_states = await _build_instance_states()
    active_requests, aggregates = await _build_active_requests_and_aggregates()
    endpoints = await _build_endpoints()
    pending_hosts = await host_store.get_all_pending()

    return RoutingSnapshot(
        generated_at=datetime.now(timezone.utc).isoformat(),
        hosts=hosts,
        instance_states=instance_states,
        active_requests=active_requests,
        aggregates=aggregates,
        endpoints=endpoints,
        pending_hosts=pending_hosts,
    )
