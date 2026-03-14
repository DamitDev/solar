"""/webui namespace - WebUI clients connect here to receive events.

Features:
- Authenticated via management API key
- Receives all host events (forwarded from /hosts namespace)
- Receives routing events (emitted by gateway)
- Can set filters for gateway_request events
- Receives initial status on connect
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from .server import sio
from app.config import settings
from app.database.hosts import host_db
from app.socketio_app.host_handlers import is_host_connected, get_pending_hosts

logger = logging.getLogger(__name__)


def _extract_key_from_environ(environ: dict) -> Optional[str]:
    """Extract API key from ASGI scope headers (set by reverse proxy)."""
    for name, value in environ.get("headers", []):
        header = name.decode("latin-1").lower() if isinstance(name, bytes) else name.lower()
        val = value.decode("latin-1") if isinstance(value, bytes) else value
        if header == "x-api-key":
            return val
        if header == "authorization" and val.startswith("Bearer "):
            return val[7:]
    return None


@sio.on("connect", namespace="/webui")
async def webui_connect(sid: str, environ: dict, auth: Optional[dict] = None):
    """Authenticate WebUI client and send initial state."""
    api_key = (auth or {}).get("api_key") or _extract_key_from_environ(environ)
    if api_key != settings.management_api_key:
        logger.warning("WebUI client %s rejected: bad auth", sid)
        raise ConnectionRefusedError("Invalid management API key")

    logger.info("WebUI client connected [sid=%s]", sid)

    # Send initial status of all hosts
    hosts = await host_db.get_all_hosts()
    await sio.emit(
        "initial_status",
        [
            {
                "host_id": h.id,
                "name": h.name,
                "status": h.status.value,
                "url": h.url,
                "last_seen": h.last_seen.isoformat() if h.last_seen else None,
                "memory": h.memory.model_dump() if h.memory else None,
                "connected": is_host_connected(h.id),
            }
            for h in hosts
        ],
        to=sid,
        namespace="/webui",
    )

    # Send current pending hosts (if any)
    pending = get_pending_hosts()
    for p in pending:
        await sio.emit("host_pending", p, to=sid, namespace="/webui")


@sio.on("disconnect", namespace="/webui")
async def webui_disconnect(sid: str):
    logger.info("WebUI client disconnected [sid=%s]", sid)


@sio.on("set_filter", namespace="/webui")
async def webui_set_filter(sid: str, filter_config: dict):
    """Update the client's event filter.

    With Socket.IO rooms, we could use room-based filtering for more efficiency,
    but for now we store the filter per-session and filter server-side on emit.
    The Redis adapter ensures all replicas can emit to this client.
    """
    # Store filter in session data
    async with sio.session(sid, namespace="/webui") as session:
        session["filter"] = filter_config

    await sio.emit(
        "filter_status",
        {"filter": filter_config},
        to=sid,
        namespace="/webui",
    )


async def broadcast_to_webui(event: str, data: dict):
    """Helper to emit events to all WebUI clients.

    The Redis adapter ensures this reaches clients on all replicas.
    """
    await sio.emit(event, data, namespace="/webui")


async def broadcast_gateway_request(summary_data: dict):
    """Broadcast a completed gateway request summary to WebUI clients."""
    await sio.emit("gateway_request", summary_data, namespace="/webui")
