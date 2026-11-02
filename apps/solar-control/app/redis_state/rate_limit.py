"""Per-key request rate limiting in Redis (fixed 60 s window).

The count is an atomic INCR on a window key, shared by every replica, so two
simultaneous requests cannot both pass on the last slot. The window is fixed, not
sliding: a client can reach at most 2x the limit across a window boundary, which
the edge's per-IP cap already absorbs. The window key expires on its own, so a
crashed replica leaves no permanent state.
"""

import time

from .connection import redis_client

RATE_PREFIX = "solar:ratelimit:"
WINDOW_S = 60


class RateLimitStore:
    async def hit(self, api_key_id: str, limit: int) -> tuple[bool, int]:
        """Count one request; return (allowed, retry_after_s)."""
        r = redis_client()
        now = int(time.time())
        window = now // WINDOW_S
        key = f"{RATE_PREFIX}{api_key_id}:{window}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, WINDOW_S * 2)
        count, _ = await pipe.execute()
        if count > limit:
            return False, WINDOW_S - (now % WINDOW_S)
        return True, 0


rate_limit_store = RateLimitStore()
