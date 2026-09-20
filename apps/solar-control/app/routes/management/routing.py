"""Routing view management routes (under /api/routing).

``GET /api/routing/state`` mirrors the authoritative fleet snapshot over REST so
the WebUI can fall back to server state when the Socket.IO connection is down.
The payload is identical to the ``routing_snapshot`` WS event, both produced by
:func:`app.services.routing_snapshot_builder.build_routing_snapshot`.
"""

from fastapi import APIRouter

from app.models.routing_snapshot import RoutingSnapshot
from app.services.routing_snapshot_builder import build_routing_snapshot

router = APIRouter(prefix="/routing", tags=["routing"])


@router.get("/state", response_model=RoutingSnapshot)
async def get_routing_state() -> RoutingSnapshot:
    """Return the current authoritative routing snapshot."""
    return await build_routing_snapshot()
