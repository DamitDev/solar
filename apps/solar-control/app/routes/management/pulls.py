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

    Doubles as the pruner: the hash has no TTL, so without this an entry
    would be returned to every client forever. Two ages apply.

    * Terminal (``completed``/``failed``) entries live for
      ``pull_progress_terminal_grace_s``, long enough for a late-joining
      client to still see the outcome.
    * Non-terminal entries live for ``pull_progress_stale_after_s`` plus a
      margin of the same length. Past that the host has stopped reporting —
      it died mid-pull, or the pull wedged — and serving a frozen
      ``downloading`` row as live progress is worse than showing nothing.
      The margin keeps a host that merely missed a couple of emissions from
      having its live download erased.
    """
    from app.redis_state.freshness import entry_age_s

    stale_after = settings.pull_progress_stale_after_s * 2

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
        age = entry_age_s(parsed.get("at"))
        if age is not None:
            terminal = phase in ("completed", "failed")
            limit = settings.pull_progress_terminal_grace_s if terminal else stale_after
            if age > limit:
                expired.append(str(field))
                continue
        # decode_responses=True is guaranteed by init_redis; str() keeps
        # pyright happy with the bytes|str union.
        result[str(field)] = parsed

    if expired:
        try:
            await r.hdel(PULLS_MAP, *expired)
        except Exception:
            logger.warning("Failed to prune stale pull entries", exc_info=True)
    return result
