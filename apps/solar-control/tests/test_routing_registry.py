"""Tests for the per-request active registry on ``RoutingStore``.

The store runs against a dict-based fake Redis (the store itself is the real
``RoutingStore``). The fake records every ``expire`` issued so tests can assert
a TTL is applied on write and that a terminal delete actually removes the entry
regardless of who created it (cross-pod delete semantics).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.redis_state.routing import (
    ACTIVE_TTL_S,
    REQ_PREFIX,
    RoutingStore,
)


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple[Any, ...]] = []

    def set(self, key, value):
        self._ops.append(("set", key, value))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        for op in self._ops:
            kind = op[0]
            if kind == "set":
                await self._redis.set(op[1], op[2])
            elif kind == "expire":
                await self._redis.expire(op[1], op[2])


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expire_calls: list[tuple[str, int]] = []

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def set(self, key, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def expire(self, key, ttl):
        self.expire_calls.append((key, ttl))

    async def delete(self, key):
        self.store.pop(key, None)

    async def mget(self, *keys):
        return [self.store.get(k) for k in keys]

    async def scan_iter(self, match=None):
        import fnmatch

        for key in list(self.store):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr("app.redis_state.routing.redis_client", lambda: redis)
    return redis


@pytest.mark.anyio
async def test_create_request_persists_entry_and_issues_ttl(fake_redis):
    store = RoutingStore()
    await store.create_request(
        "req-1",
        model="model-x",
        endpoint="/v1/chat/completions",
        client_ip="1.2.3.4",
        timestamp="2026-09-02T10:00:00+00:00",
    )

    entry = await store.get_request("req-1")
    assert entry is not None
    assert entry["model"] == "model-x"
    assert entry["endpoint"] == "/v1/chat/completions"
    assert entry["client_ip"] == "1.2.3.4"
    assert entry["timestamp"] == "2026-09-02T10:00:00+00:00"
    assert (f"{REQ_PREFIX}req-1", ACTIVE_TTL_S) in fake_redis.expire_calls


@pytest.mark.anyio
async def test_create_request_persists_all_fields_unconditionally(fake_redis):
    """All required fields (including empty strings) are persisted as-is; the
    store never drops create fields, matching the update contract."""
    store = RoutingStore()
    await store.create_request(
        "req-1", model="", endpoint="/v1", client_ip="", timestamp="ts"
    )

    entry = await store.get_request("req-1")
    assert entry is not None
    assert entry["model"] == ""
    assert entry["client_ip"] == ""
    assert entry["endpoint"] == "/v1"


@pytest.mark.anyio
async def test_create_request_honors_custom_ttl(fake_redis):
    store = RoutingStore()
    await store.create_request(
        "req-1",
        model="model-x",
        endpoint="/v1",
        client_ip="1.2.3.4",
        timestamp="ts",
        ttl=60,
    )

    assert (f"{REQ_PREFIX}req-1", 60) in fake_redis.expire_calls


@pytest.mark.anyio
async def test_update_request_merges_routed_fields(fake_redis):
    store = RoutingStore()
    await store.create_request(
        "req-1", model="model-x", endpoint="/v1", client_ip="1.2.3.4", timestamp="ts"
    )
    updated = await store.update_request(
        "req-1",
        host_id="host-1",
        host_name="host-one",
        instance_id="inst-1",
        resolved_model="model-x-32b",
        attempt=1,
    )

    assert updated is not None
    assert updated["host_id"] == "host-1"
    assert updated["host_name"] == "host-one"
    assert updated["instance_id"] == "inst-1"
    assert updated["resolved_model"] == "model-x-32b"
    assert updated["attempt"] == 1
    assert updated["model"] == "model-x"


@pytest.mark.anyio
async def test_update_request_missing_returns_none(fake_redis):
    store = RoutingStore()
    assert await store.update_request("nope", host_id="host-1") is None


@pytest.mark.anyio
async def test_delete_request_removes_entry(fake_redis):
    store = RoutingStore()
    await store.create_request(
        "req-1", model="model-x", endpoint="/v1", client_ip="1.2.3.4", timestamp="ts"
    )
    await store.delete_request("req-1")

    assert await store.get_request("req-1") is None


@pytest.mark.anyio
async def test_cross_pod_delete_semantics(fake_redis):
    """A second store instance (simulating another replica) can delete an entry
    created by the first — the shared Redis key is removed regardless of which
    replica created it."""
    store_a = RoutingStore()
    store_b = RoutingStore()

    await store_a.create_request(
        "req-1",
        model="model-x",
        endpoint="/v1",
        client_ip="1.2.3.4",
        timestamp="ts",
    )
    assert await store_b.get_request("req-1") is not None

    await store_b.delete_request("req-1")

    assert await store_a.get_request("req-1") is None


@pytest.mark.anyio
async def test_list_requests_returns_live_entries_excluding_expired(fake_redis):
    store = RoutingStore()
    await store.create_request(
        "req-1", model="model-a", endpoint="/v1", client_ip="1.1.1.1", timestamp="ts1"
    )
    await store.create_request(
        "req-2", model="model-b", endpoint="/v2", client_ip="2.2.2.2", timestamp="ts2"
    )
    await store.create_request(
        "req-3", model="model-c", endpoint="/v3", client_ip="3.3.3.3", timestamp="ts3"
    )
    await store.delete_request("req-2")

    entries = await store.list_requests()
    assert {e["model"] for e in entries} == {"model-a", "model-c"}
