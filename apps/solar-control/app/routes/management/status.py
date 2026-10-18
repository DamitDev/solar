"""Reserved status endpoint for the Solar Status SPA.

GET /api/status — placeholder returning 501 Not Implemented. The payload
schema, aggregation logic, and realtime behavior are deferred feature
decisions; this only reserves the route so the SPA's API contract endpoint
exists without solar-control implementing any status model yet.
"""

from fastapi import APIRouter, Response

router = APIRouter(prefix="/status", tags=["status"])


@router.get("")
async def get_status() -> Response:
    """Return 501 Not Implemented until the status model is built."""
    return Response(status_code=501)
