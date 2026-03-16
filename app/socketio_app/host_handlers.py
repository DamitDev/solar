"""/hosts namespace - Solar hosts connect here to stream events.

Two-phase connection:
1. Host connects with auth={'api_key': '...'}
2. If the API key matches a registered host -> immediate activation
3. If the API key is unknown -> connection is accepted but held in
   "pending approval" state. Events are silently ignored until an
   admin approves the host from the WebUI.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .server import sio
from app.database.hosts import host_db
from app.models import HostStatus

logger = logging.getLogger(__name__)

# ── Active hosts ──────────────────────────────────────────────

# sid -> host_id  (only for approved/registered hosts)
_sid_to_host: Dict[str, str] = {}
# host_id -> list of instance dicts (cached from host events)
_host_instances: Dict[str, list] = {}


def get_host_instances(host_id: str) -> list:
    return _host_instances.get(host_id, [])


def is_host_connected(host_id: str) -> bool:
    return host_id in set(_sid_to_host.values())


def get_connected_host_ids() -> list:
    return list(set(_sid_to_host.values()))


# ── Pending hosts ─────────────────────────────────────────────


@dataclass
class PendingHost:
    pending_id: str
    sid: str
    api_key: str
    host_name: str = ""
    instances: list = field(default_factory=list)
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# pending_id -> PendingHost
_pending_hosts: Dict[str, PendingHost] = {}
# sid -> pending_id  (quick lookup on events/disconnect)
_pending_sids: Dict[str, str] = {}


def get_pending_hosts() -> List[dict]:
    """Return serialisable list of all pending hosts."""
    return [
        {
            "pending_id": p.pending_id,
            "api_key_preview": (
                p.api_key[:8] + "..." if len(p.api_key) > 8 else p.api_key
            ),
            "host_name": p.host_name,
            "instance_count": len(p.instances),
            "connected_at": p.connected_at.isoformat(),
        }
        for p in _pending_hosts.values()
    ]


def get_pending_host(pending_id: str) -> Optional[PendingHost]:
    return _pending_hosts.get(pending_id)


def remove_pending(pending_id: str) -> Optional[PendingHost]:
    p = _pending_hosts.pop(pending_id, None)
    if p:
        _pending_sids.pop(p.sid, None)
    return p


async def approve_pending_host(pending_id: str, name: str, url: str) -> Optional[str]:
    """Approve a pending host: create DB record, promote the socket connection.

    Returns the new host_id, or None if the pending_id was not found.
    """
    from app.models import Host

    p = remove_pending(pending_id)
    if not p:
        return None

    host_id = str(uuid.uuid4())
    host = Host(
        id=host_id, name=name, url=url, api_key=p.api_key, status=HostStatus.ONLINE
    )
    await host_db.add_host(host)

    # Promote: move sid from pending into active
    _sid_to_host[p.sid] = host_id
    if p.instances:
        _host_instances[host_id] = p.instances

    # Tell the host it's been approved
    await sio.emit(
        "registration_ack",
        {
            "host_id": host_id,
            "host_name": name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        to=p.sid,
        namespace="/hosts",
    )

    # Tell WebUI: remove the pending entry, then announce the new host
    await sio.emit(
        "host_pending_removed", {"pending_id": pending_id}, namespace="/webui"
    )
    await sio.emit(
        "host_status",
        {
            "host_id": host_id,
            "name": name,
            "status": "online",
            "url": url,
            "memory": None,
            "connected": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        namespace="/webui",
    )

    # Send cached instances to WebUI
    if p.instances:
        await sio.emit(
            "instances_update",
            {"host_id": host_id, "instances": p.instances},
            namespace="/webui",
        )

    # Trigger registry refresh to pick up the new host's instances
    try:
        from app.gateway import gateway

        asyncio.create_task(gateway.refresh_model_registry())
    except Exception:
        pass

    logger.info("Pending host approved -> '%s' (%s)", name, host_id)
    return host_id


async def reject_pending_host(pending_id: str) -> bool:
    p = remove_pending(pending_id)
    if not p:
        return False

    # Tell the host it was rejected, then disconnect it
    try:
        await sio.emit(
            "rejected",
            {"reason": "Host registration rejected by admin"},
            to=p.sid,
            namespace="/hosts",
        )
        await sio.disconnect(p.sid, namespace="/hosts")
    except Exception:
        pass

    await sio.emit(
        "host_pending_removed", {"pending_id": pending_id}, namespace="/webui"
    )
    logger.info("Pending host rejected (pending_id=%s)", pending_id)
    return True


# ── Socket.IO event handlers ─────────────────────────────────


@sio.on("connect", namespace="/hosts")
async def host_connect(sid: str, environ: dict, auth: Optional[dict] = None):
    if not auth or "api_key" not in auth:
        logger.warning("Host %s rejected: no auth", sid)
        raise ConnectionRefusedError("Authentication required")

    api_key = auth["api_key"]

    # Try to match a registered host
    host = await host_db.get_host_by_api_key(api_key)

    if host:
        # Known host -> activate immediately
        _sid_to_host[sid] = host.id
        await host_db.update_host_status(host.id, HostStatus.ONLINE)
        logger.info("Host '%s' (%s) connected [sid=%s]", host.name, host.id, sid)

        await sio.emit(
            "registration_ack",
            {
                "host_id": host.id,
                "host_name": host.name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            to=sid,
            namespace="/hosts",
        )

        await sio.emit(
            "host_status",
            {
                "host_id": host.id,
                "name": host.name,
                "status": "online",
                "url": host.url,
                "memory": host.memory.model_dump() if host.memory else None,
                "connected": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            namespace="/webui",
        )
    else:
        # Unknown API key -> pending approval
        pending_id = str(uuid.uuid4())
        pending = PendingHost(
            pending_id=pending_id,
            sid=sid,
            api_key=api_key,
            host_name=auth.get("host_name", ""),
        )
        _pending_hosts[pending_id] = pending
        _pending_sids[sid] = pending_id

        logger.info(
            "Host %s connected with unknown key -> pending (id=%s)", sid, pending_id
        )

        # Tell the host it's pending
        await sio.emit(
            "pending",
            {
                "pending_id": pending_id,
                "message": "Waiting for admin approval",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            to=sid,
            namespace="/hosts",
        )

        # Tell WebUI about the pending host
        await sio.emit(
            "host_pending",
            {
                "pending_id": pending_id,
                "api_key_preview": api_key[:8] + "..." if len(api_key) > 8 else api_key,
                "host_name": pending.host_name,
                "connected_at": pending.connected_at.isoformat(),
            },
            namespace="/webui",
        )


@sio.on("disconnect", namespace="/hosts")
async def host_disconnect(sid: str):
    # Check if it was an active host
    host_id = _sid_to_host.pop(sid, None)
    if host_id:
        _host_instances.pop(host_id, None)
        await host_db.update_host_status(host_id, HostStatus.OFFLINE)

        host = await host_db.get_host(host_id)
        logger.info("Host '%s' (%s) disconnected", host.name if host else "?", host_id)

        await sio.emit(
            "host_status",
            {
                "host_id": host_id,
                "name": host.name if host else None,
                "status": "offline",
                "url": host.url if host else None,
                "memory": host.memory.model_dump() if host and host.memory else None,
                "connected": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            namespace="/webui",
        )
        return

    # Check if it was a pending host
    pending_id = _pending_sids.pop(sid, None)
    if pending_id:
        _pending_hosts.pop(pending_id, None)
        logger.info("Pending host disconnected (pending_id=%s)", pending_id)
        await sio.emit(
            "host_pending_removed", {"pending_id": pending_id}, namespace="/webui"
        )


@sio.on("registration", namespace="/hosts")
async def host_registration(sid: str, data: dict):
    """Receive initial instance list from host."""
    # Active host -> process normally
    host_id = _sid_to_host.get(sid)
    if host_id:
        instances = data.get("instances", [])
        _host_instances[host_id] = instances

        # Forward to WebUI so the dashboard gets the instance list
        await sio.emit(
            "instances_update",
            {"host_id": host_id, "instances": instances},
            namespace="/webui",
        )

        try:
            from app.gateway import gateway

            asyncio.create_task(gateway.refresh_model_registry())
        except Exception:
            pass
        return

    # Pending host -> stash the data for when it gets approved
    pending_id = _pending_sids.get(sid)
    if pending_id and pending_id in _pending_hosts:
        p = _pending_hosts[pending_id]
        p.host_name = data.get("host_name", p.host_name)
        p.instances = data.get("instances", [])

        # Update the WebUI with the richer info now that we have host_name
        await sio.emit(
            "host_pending",
            {
                "pending_id": pending_id,
                "api_key_preview": (
                    p.api_key[:8] + "..." if len(p.api_key) > 8 else p.api_key
                ),
                "host_name": p.host_name,
                "instance_count": len(p.instances),
                "connected_at": p.connected_at.isoformat(),
            },
            namespace="/webui",
        )


@sio.on("instance_state", namespace="/hosts")
async def host_instance_state(sid: str, data: dict):
    host_id = _sid_to_host.get(sid)
    if not host_id:
        return

    host = await host_db.get_host(host_id)
    await sio.emit(
        "instance_state",
        {
            "host_id": host_id,
            "host_name": host.name if host else None,
            "instance_id": data.get("instance_id"),
            "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "data": data.get("data", data),
        },
        namespace="/webui",
    )


@sio.on("log", namespace="/hosts")
async def host_log(sid: str, data: dict):
    host_id = _sid_to_host.get(sid)
    if not host_id:
        return

    host = await host_db.get_host(host_id)
    await sio.emit(
        "log",
        {
            "host_id": host_id,
            "host_name": host.name if host else None,
            "instance_id": data.get("instance_id"),
            "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "data": data.get("data", data),
        },
        namespace="/webui",
    )


@sio.on("host_health", namespace="/hosts")
async def host_health(sid: str, data: dict):
    host_id = _sid_to_host.get(sid)
    if not host_id:
        return

    health_data = data.get("data", data)
    memory = health_data.get("memory")
    if memory:
        await host_db.update_host_memory(host_id, memory)

    host = await host_db.get_host(host_id)
    await sio.emit(
        "host_health",
        {
            "host_id": host_id,
            "host_name": host.name if host else None,
            "timestamp": data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "data": health_data,
        },
        namespace="/webui",
    )


@sio.on("instances_update", namespace="/hosts")
async def host_instances_update(sid: str, data: dict):
    host_id = _sid_to_host.get(sid)
    if not host_id:
        return

    instances = data.get("data", {}).get("instances", data.get("instances", []))
    _host_instances[host_id] = instances

    # Forward to WebUI
    await sio.emit(
        "instances_update",
        {"host_id": host_id, "instances": instances},
        namespace="/webui",
    )

    try:
        from app.gateway import gateway

        asyncio.create_task(gateway.refresh_model_registry())
    except Exception:
        pass
