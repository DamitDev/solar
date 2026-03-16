"""Instance health state stored in Redis with TTL.

Each instance has a health entry tracking last successful probe
and optional cooldown period.
"""

import json
import time

from .connection import redis_client

HEALTH_PREFIX = "solar:health:"


def _key(host_id: str, instance_id: str) -> str:
    return f"{HEALTH_PREFIX}{host_id}:{instance_id}"


class HealthStore:
    """Instance health tracking in Redis."""

    async def mark_healthy(
        self, host_id: str, instance_id: str, *, ttl_s: float = 5.0
    ) -> None:
        r = redis_client()
        data = json.dumps({"last_ok": time.time(), "cooldown_until": 0})
        await r.set(_key(host_id, instance_id), data, ex=int(ttl_s + 2))

    async def mark_failed(
        self,
        host_id: str,
        instance_id: str,
        *,
        cooldown_s: float = 5.0,
        ttl_s: float = 10.0,
    ) -> None:
        r = redis_client()
        data = json.dumps(
            {
                "last_ok": 0,
                "cooldown_until": time.time() + cooldown_s,
            }
        )
        await r.set(_key(host_id, instance_id), data, ex=int(ttl_s + 2))

    async def is_healthy(
        self, host_id: str, instance_id: str, *, health_ttl_s: float = 3.0
    ) -> bool:
        """Check if an instance is considered healthy.

        Healthy means: has a recent probe success AND is not in cooldown.
        """
        r = redis_client()
        raw = await r.get(_key(host_id, instance_id))
        if raw is None:
            return False
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return False
        now = time.time()
        if data.get("cooldown_until", 0) > now:
            return False
        last_ok = data.get("last_ok", 0)
        if last_ok == 0:
            return False
        return (now - last_ok) < health_ttl_s

    async def clear(self, host_id: str, instance_id: str) -> None:
        r = redis_client()
        await r.delete(_key(host_id, instance_id))
