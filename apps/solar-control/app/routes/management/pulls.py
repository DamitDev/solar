"""Model pull progress API (C4).

GET /api/pulls — latest pull progress per (host, model source URI), from
the Redis hash the host_health/pull_progress WS pipeline maintains. Lets a
late-joining webui client render an in-flight cold start without waiting
for the next ``pull_progress`` event.
"""

import json
import logging

from fastapi import APIRouter

from app.config import settings
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

    Doubles as the pruner for finished pulls: the hash has no TTL, so without
    this a completed pull would be returned to every client forever. Entries
    past ``pull_progress_terminal_grace_s`` are dropped, which is long enough
    for a late-joining client to still see the outcome.
    """
    from app.services.reconciliation import _entry_age_s

    r = redis_client()
    raw = await r.hgetall(PULLS_MAP)
    result: dict[str, dict] = {}
    expired: list[str] = []
    for field, value in raw.items():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            # Unparseable entries can never become parseable; drop them.
            expired.append(str(field))
            continue
        if not isinstance(parsed, dict):
            expired.append(str(field))
            continue
        data = parsed.get("data")
        phase = data.get("phase") if isinstance(data, dict) else None
        age = _entry_age_s(parsed.get("at"))
        if (
            phase in ("completed", "failed")
            and age is not None
            and age > settings.pull_progress_terminal_grace_s
        ):
            expired.append(str(field))
            continue
        # decode_responses=True is guaranteed by init_redis; str() keeps
        # pyright happy with the bytes|str union.
        result[str(field)] = parsed

    if expired:
        try:
            await r.hdel(PULLS_MAP, *expired)
        except Exception:
            logger.warning("Failed to prune finished pull entries", exc_info=True)
    return result
