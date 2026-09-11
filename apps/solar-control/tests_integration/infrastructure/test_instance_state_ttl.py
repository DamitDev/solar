"""infrastructure: per-instance runtime state TTL expiry (marker: infrastructure).

The store is exercised against the live session Redis (``stack.db_env``), not a
mocked client, so TTL expiry is proven against a real store: a write with a
short TTL round-trips, and after the TTL passes the entry is gone while
non-expired sibling entries survive.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.infrastructure


async def test_instance_state_real_write_and_ttl_expiry(stack, clean_state):
    """A real Redis write round-trips through the store and expires after TTL."""
    from app.redis_state.instance_states import InstanceStatesStore

    r = await _redis(stack)
    await r.flushall()
    store = InstanceStatesStore()

    try:
        ttl_s = 1
        entry_a = {
            "host_id": "host-a",
            "host_name": "host A",
            "instance_id": "inst-a",
            "timestamp": "2026-09-02T10:00:00+00:00",
            "data": {"load": 0.5},
        }
        entry_b = {
            "host_id": "host-a",
            "host_name": "host A",
            "instance_id": "inst-b",
            "timestamp": "2026-09-02T10:00:01+00:00",
            "data": {"load": 0.2},
        }

        # Round-trip a real write.
        await store.set(entry_a["host_id"], entry_a["instance_id"], entry_a, ttl=ttl_s)
        await store.set(entry_b["host_id"], entry_b["instance_id"], entry_b, ttl=300)

        loaded = await store.get("host-a", "inst-a")
        assert loaded == entry_a
        assert loaded["data"]["load"] == 0.5

        # Wait for only the short-TTL entry to expire.
        deadline = time.time() + ttl_s + 2.0
        while time.time() < deadline:
            if await store.get("host-a", "inst-a") is None:
                break
            await asyncio.sleep(0.1)

        # Short-TTL entry expired; the long-TTL sibling is still alive.
        assert await store.get("host-a", "inst-a") is None
        assert await store.get("host-a", "inst-b") == entry_b
    finally:
        await r.delete("solar:istate:host-a:inst-a")
        await r.delete("solar:istate:host-a:inst-b")
        await r.aclose()


async def _redis(stack):
    import redis.asyncio as aioredis

    return aioredis.from_url(stack.db_env["redis"], decode_responses=True)
