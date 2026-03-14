"""/hosts namespace - Solar hosts connect here to stream events.

Protocol:
1. Host connects with auth={'api_key': '...'} in handshake
2. Host emits events: registration, instance_state, log, host_health, instances_update
3. Server validates host API key against database
4. Events are forwarded to /webui namespace for all WebUI clients
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .server import sio
from app.database.hosts import host_db
from app.models import HostStatus

logger = logging.getLogger(__name__)

# Connected host state: sid -> host_id
_sid_to_host: Dict[str, str] = {}
# host_id -> list of instance dicts (cached from host events)
_host_instances: Dict[str, list] = {}


def get_host_instances(host_id: str) -> list:
    return _host_instances.get(host_id, [])


def is_host_connected(host_id: str) -> bool:
    return host_id in {v for v in _sid_to_host.values()}


def get_connected_host_ids() -> list:
    return list(set(_sid_to_host.values()))


@sio.on("connect", namespace="/hosts")
async def host_connect(sid: str, environ: dict, auth: Optional[dict] = None):
    """Authenticate host on connect via API key."""
    if not auth or "api_key" not in auth:
        logger.warning("Host %s rejected: no auth", sid)
        raise ConnectionRefusedError("Authentication required")

    host = await host_db.get_host_by_api_key(auth["api_key"])
    if not host:
        logger.warning("Host %s rejected: unknown API key", sid)
        raise ConnectionRefusedError("Unknown API key")

    _sid_to_host[sid] = host.id
    await host_db.update_host_status(host.id, HostStatus.ONLINE)

    logger.info("Host '%s' (%s) connected [sid=%s]", host.name, host.id, sid)

    # Send registration ack
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

    # Notify WebUI clients
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


@sio.on("disconnect", namespace="/hosts")
async def host_disconnect(sid: str):
    host_id = _sid_to_host.pop(sid, None)
    if not host_id:
        return

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


@sio.on("registration", namespace="/hosts")
async def host_registration(sid: str, data: dict):
    """Receive initial instance list from host."""
    host_id = _sid_to_host.get(sid)
    if not host_id:
        return

    instances = data.get("instances", [])
    _host_instances[host_id] = instances

    # Trigger registry refresh
    try:
        from app.gateway import gateway
        asyncio.create_task(gateway.refresh_model_registry())
    except Exception:
        pass


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

    # Update host memory in database
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

    # Trigger registry refresh
    try:
        from app.gateway import gateway
        asyncio.create_task(gateway.refresh_model_registry())
    except Exception:
        pass
