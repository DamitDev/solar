"""Per-instance runtime state stored in Redis.

Persists the latest runtime state each host reports per ``host:instance`` at
the relay choke points so instance load bars do not freeze when the WebUI
reconnects after sleeping or a socket drop. Entries carry a TTL so a host
that stops reporting expires its recorded state instead of going stale.
"""

import json
from typing import Any

from .connection import redis_client

ISTATE_PREFIX = "solar:istate:"

# TTL for instance state entries - auto-expire if a host dies mid-stream
ISTATE_TTL_S = 300


class InstanceStatesStore:
    """Per-instance runtime state in Redis, keyed by ``host:instance``."""

    def _key(self, host_id: str, instance_id: str) -> str:
        return f"{ISTATE_PREFIX}{host_id}:{instance_id}"

    async def set(
        self,
        host_id: str,
        instance_id: str,
        entry: dict[str, Any],
        *,
        ttl: int | None = None,
    ) -> None:
        """Store ``entry`` under ``host:instance`` and refresh its TTL.

        ``entry`` carries the ``InstanceStatePayload`` shape
        (``host_id``, ``host_name``, ``instance_id``, ``timestamp``,
        ``data``). The TTL is issued on every write so a host that keeps
        reporting stays alive while a silent host expires.
        """
        r = redis_client()
        key = self._key(host_id, instance_id)
        pipe = r.pipeline()
        pipe.set(key, json.dumps(entry))
        pipe.expire(key, ttl or ISTATE_TTL_S)
        await pipe.execute()

    async def get(self, host_id: str, instance_id: str) -> dict[str, Any] | None:
        """Read the latest stored state for ``host:instance``, or None."""
        r = redis_client()
        raw = await r.get(self._key(host_id, instance_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def delete(self, host_id: str, instance_id: str) -> None:
        """Drop the stored state for ``host:instance``."""
        r = redis_client()
        await r.delete(self._key(host_id, instance_id))

    async def get_all(self) -> list[dict[str, Any]]:
        """Return every non-expired instance state entry (used by snapshotting)."""
        r = redis_client()
        keys = [key async for key in r.scan_iter(match=f"{ISTATE_PREFIX}*")]
        values = await r.mget(*keys)
        entries: list[dict[str, Any]] = []
        for raw in values:
            if raw is None:
                continue
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                continue
            entries.append(parsed)
        return entries
