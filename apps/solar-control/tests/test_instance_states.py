"""Tests for app.redis_state.instance_states — per-instance runtime state.

The store runs against a dict-based fake Redis (the store itself is the real
``InstanceStatesStore``). The fake records every ``expire`` issued so tests can
assert a TTL is applied on write; real TTL expiry semantics are covered by the
integration suite against a live Redis.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.redis_state.instance_states import (
    ISTATE_PREFIX,
    ISTATE_TTL_S,
    InstanceStatesStore,
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
    monkeypatch.setattr("app.redis_state.instance_states.redis_client", lambda: redis)
    return redis


def _entry(
    *, host_id: str = "host-1", instance_id: str = "inst-1", **overrides
) -> dict[str, Any]:
    entry = {
        "host_id": host_id,
        "host_name": "host-one",
        "instance_id": instance_id,
        "timestamp": "2026-09-02T10:00:00+00:00",
        "data": {"load": 0.6},
    }
    entry.update(overrides)
    return entry


@pytest.mark.anyio
async def test_set_persists_entry_under_host_instance(fake_redis):
    store = InstanceStatesStore()
    entry = _entry()
    await store.set(entry["host_id"], entry["instance_id"], entry)

    assert fake_redis.store["solar:istate:host-1:inst-1"] is not None


@pytest.mark.anyio
async def test_get_returns_stored_entry(fake_redis):
    store = InstanceStatesStore()
    entry = _entry(data={"load": 0.9, "memory_gb": 4})
    await store.set(entry["host_id"], entry["instance_id"], entry)

    loaded = await store.get("host-1", "inst-1")
    assert loaded == entry
    assert loaded["data"]["load"] == 0.9


@pytest.mark.anyio
async def test_get_missing_returns_none(fake_redis):
    store = InstanceStatesStore()
    assert await store.get("host-1", "nope") is None


@pytest.mark.anyio
async def test_delete_removes_entry(fake_redis):
    store = InstanceStatesStore()
    entry = _entry()
    await store.set(entry["host_id"], entry["instance_id"], entry)
    await store.delete("host-1", "inst-1")

    assert await store.get("host-1", "inst-1") is None


@pytest.mark.anyio
async def test_write_issues_ttl(fake_redis):
    store = InstanceStatesStore()
    entry = _entry()
    await store.set(entry["host_id"], entry["instance_id"], entry)

    assert (f"{ISTATE_PREFIX}host-1:inst-1", ISTATE_TTL_S) in fake_redis.expire_calls


@pytest.mark.anyio
async def test_write_honors_custom_ttl(fake_redis):
    store = InstanceStatesStore()
    entry = _entry()
    await store.set(entry["host_id"], entry["instance_id"], entry, ttl=60)

    assert (f"{ISTATE_PREFIX}host-1:inst-1", 60) in fake_redis.expire_calls


@pytest.mark.anyio
async def test_get_all_returns_live_entries(fake_redis):
    store = InstanceStatesStore()
    await store.set("host-1", "inst-1", _entry(instance_id="inst-1"))
    await store.set("host-2", "inst-2", _entry(host_id="host-2", instance_id="inst-2"))

    all_entries = await store.get_all()
    assert {e["instance_id"] for e in all_entries} == {"inst-1", "inst-2"}
