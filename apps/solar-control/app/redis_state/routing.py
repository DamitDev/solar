"""Routing state in Redis: active request counts, host weights, round-robin.

All operations are atomic to ensure consistency across replicas.

Also hosts a per-request active registry (``REQ_PREFIX``), the server-authoritative
source for the WebUI's active-request view. Each in-flight request is stored keyed
by ``request_id`` and removed when a terminal event (success/error) arrives.
Because the registry lives in shared Redis, a terminal event on any replica
removes the entry regardless of which replica created it (cross-pod delete), and
the TTL self-heals state for replicas that crash mid-request.
"""

from typing import Any
import json

from .connection import redis_client

ACTIVE_PREFIX = "solar:active:"
WEIGHT_PREFIX = "solar:weight:"
RR_PREFIX = "solar:rr:"
REQ_PREFIX = "solar:active-req:"

# TTL for active counters - auto-cleanup if a replica crashes mid-request
ACTIVE_TTL_S = 300


class RoutingStore:
    """Atomic routing state shared across all solar-control replicas."""

    # --- Active request counts per instance ---

    async def increment_active(self, host_id: str, instance_id: str) -> int:
        r = redis_client()
        key = f"{ACTIVE_PREFIX}{host_id}:{instance_id}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ACTIVE_TTL_S)
        val, _ = await pipe.execute()
        return val

    async def decrement_active(self, host_id: str, instance_id: str) -> int:
        r = redis_client()
        key = f"{ACTIVE_PREFIX}{host_id}:{instance_id}"
        val = await r.decr(key)
        if val <= 0:
            await r.delete(key)
            return 0
        return val

    async def get_active(self, host_id: str, instance_id: str) -> int:
        r = redis_client()
        val = await r.get(f"{ACTIVE_PREFIX}{host_id}:{instance_id}")
        return int(val) if val else 0

    # --- Host active weight (sum of model sizes in B) ---

    async def add_weight(self, host_id: str, weight: float) -> float:
        r = redis_client()
        key = f"{WEIGHT_PREFIX}{host_id}"
        pipe = r.pipeline()
        pipe.incrbyfloat(key, weight)
        pipe.expire(key, ACTIVE_TTL_S)
        val, _ = await pipe.execute()
        return val

    async def remove_weight(self, host_id: str, weight: float) -> float:
        r = redis_client()
        key = f"{WEIGHT_PREFIX}{host_id}"
        val = await r.incrbyfloat(key, -weight)
        if val <= 0:
            await r.delete(key)
            return 0.0
        return val

    async def get_weight(self, host_id: str) -> float:
        r = redis_client()
        val = await r.get(f"{WEIGHT_PREFIX}{host_id}")
        return float(val) if val else 0.0

    # --- Host active count (total requests on a host) ---

    async def increment_host_active(self, host_id: str) -> int:
        r = redis_client()
        key = f"{ACTIVE_PREFIX}host:{host_id}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ACTIVE_TTL_S)
        val, _ = await pipe.execute()
        return val

    async def decrement_host_active(self, host_id: str) -> int:
        r = redis_client()
        key = f"{ACTIVE_PREFIX}host:{host_id}"
        val = await r.decr(key)
        if val <= 0:
            await r.delete(key)
            return 0
        return val

    async def get_host_active(self, host_id: str) -> int:
        r = redis_client()
        val = await r.get(f"{ACTIVE_PREFIX}host:{host_id}")
        return int(val) if val else 0

    # --- Round-robin per model ---

    RR_TTL_S = 3600

    async def next_rr_index(self, model: str) -> int:
        """Get and increment the round-robin index for a model."""
        r = redis_client()
        key = f"{RR_PREFIX}{model}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, self.RR_TTL_S)
        val, _ = await pipe.execute()
        return val

    # --- Per-request active registry (server-authoritative routing view) ---

    @staticmethod
    def _req_key(request_id: str) -> str:
        return f"{REQ_PREFIX}{request_id}"

    @staticmethod
    async def _req_write(
        request_id: str, data: dict[str, Any], *, ttl: int | None = None
    ) -> None:
        """Persist ``data`` for ``request_id`` and refresh its TTL.

        The TTL mirrors ``ACTIVE_TTL_S`` so a replica that crashes mid-request
        expires its entry instead of showing a stuck 'processing' request.
        """
        r = redis_client()
        key = RoutingStore._req_key(request_id)
        pipe = r.pipeline()
        pipe.set(key, json.dumps(data))
        pipe.expire(key, ttl or ACTIVE_TTL_S)
        await pipe.execute()

    async def create_request(
        self,
        request_id: str,
        *,
        model: str,
        endpoint: str,
        client_ip: str,
        timestamp: str,
        ttl: int | None = None,
    ) -> None:
        """Record an in-flight request when it starts (``request_start``)."""
        await self._req_write(
            request_id,
            {
                "model": model,
                "endpoint": endpoint,
                "client_ip": client_ip,
                "timestamp": timestamp,
            },
            ttl=ttl,
        )

    async def update_request(
        self,
        request_id: str,
        *,
        host_id: str | None = None,
        host_name: str | None = None,
        instance_id: str | None = None,
        resolved_model: str | None = None,
        attempt: int | None = None,
    ) -> dict[str, Any] | None:
        """Merge routing details (host/instance/resolved_model/attempt) into an
        in-flight request (``request_routed`` / ``request_reroute``)."""
        r = redis_client()
        key = self._req_key(request_id)
        raw = await r.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if host_id is not None:
            data["host_id"] = host_id
        if host_name is not None:
            data["host_name"] = host_name
        if instance_id is not None:
            data["instance_id"] = instance_id
        if resolved_model is not None:
            data["resolved_model"] = resolved_model
        if attempt is not None:
            data["attempt"] = attempt
        await self._req_write(request_id, data)
        return data

    async def delete_request(self, request_id: str) -> None:
        """Remove an in-flight request on a terminal event
        (``request_success`` / ``request_error``).

        Works across pods: the shared Redis key is removed regardless of which
        replica originally created it.
        """
        r = redis_client()
        await r.delete(self._req_key(request_id))

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        """Read the live registry entry for ``request_id``, or None."""
        r = redis_client()
        raw = await r.get(self._req_key(request_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def list_requests(self) -> list[dict[str, Any]]:
        """Return every live in-flight request entry (used by snapshotting)."""
        r = redis_client()
        keys = [key async for key in r.scan_iter(match=f"{REQ_PREFIX}*")]
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
