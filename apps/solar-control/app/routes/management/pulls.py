"""Model pull progress API (C4).

GET /api/pulls — latest pull progress per (host, model source URI), from
the Redis hash the host_health/pull_progress WS pipeline maintains. Lets a
late-joining webui client render an in-flight cold start without waiting
for the next ``pull_progress`` event.
"""

import json
import logging

from fastapi import APIRouter

from app.redis_state.connection import redis_client
from app.redis_state.hosts import PULLS_MAP

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pulls", tags=["pulls"])


@router.get("")
async def list_pulls() -> dict[str, dict]:
    """Return the latest pull progress per (host, source_uri).

    Shape: ``{"{host_id}|{source_uri}": {"at": <iso8601>, "data": {...}}}``
    where ``data`` carries the host's payload (``source_uri``, ``phase``,
    ``bytes_done``, ``bytes_total``, ``speed_bps``).
    """
    r = redis_client()
    raw = await r.hgetall(PULLS_MAP)
    result: dict[str, dict] = {}
    for field, value in raw.items():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            # decode_responses=True is guaranteed by init_redis; str() keeps
            # pyright happy with the bytes|str union.
            result[str(field)] = parsed
    return result
