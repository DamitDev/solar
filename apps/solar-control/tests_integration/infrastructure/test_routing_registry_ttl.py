"""infrastructure: per-request active registry real write + TTL expiry
(marker: infrastructure).

The registry is exercised against the live session Redis (``stack.db_env``),
not a mocked client: a request entry round-trips through the store, a terminal
delete removes it, and a short-TTL entry expires on its own while a non-expired
sibling survives.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.infrastructure


async def test_request_registry_real_write_ttl_and_cross_pod_delete(stack, clean_state):
    """A real Redis round-trips a request entry, expires after TTL, and a
    terminal delete removes it (cross-pod semantics)."""
    from app.redis_state import close_redis, init_redis
    from app.redis_state.routing import RoutingStore

    # See test_instance_state_ttl: init the shared client for this process
    # only, and rely on clean_state's ``solar:active-req:*`` wipe instead of
    # a flushall (which would break the session's WS connection state).
    await init_redis(stack.db_env["redis"])
    store = RoutingStore()

    try:
        ttl_s = 1
        rid_short = "req-short"
        rid_long = "req-long"
        rid_terminated = "req-term"

        await store.create_request(
            rid_short,
            model="m-a",
            endpoint="/v1",
            client_ip="1.1.1.1",
            timestamp="ts",
            ttl=ttl_s,
        )
        await store.create_request(
            rid_long,
            model="m-b",
            endpoint="/v2",
            client_ip="2.2.2.2",
            timestamp="ts",
        )
        await store.create_request(
            rid_terminated,
            model="m-c",
            endpoint="/v3",
            client_ip="3.3.3.3",
            timestamp="ts",
        )

        # Round-trip a real write.
        entry = await store.get_request(rid_short)
        assert entry is not None
        assert entry["model"] == "m-a"

        # Terminal delete on the store (any replica) removes the entry.
        await store.delete_request(rid_terminated)
        assert await store.get_request(rid_terminated) is None

        # Wait for only the short-TTL entry to expire.
        deadline = time.time() + ttl_s + 2.0
        while time.time() < deadline:
            if await store.get_request(rid_short) is None:
                break
            await asyncio.sleep(0.1)

        # Short-TTL entry expired; the long-TTL sibling is still alive.
        assert await store.get_request(rid_short) is None
        assert await store.get_request(rid_long) is not None
    finally:
        for rid in (rid_short, rid_long, rid_terminated):
            await store.delete_request(rid)
        await close_redis()
