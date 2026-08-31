"""infrastructure: gateway registry alias lifecycle (marker: infrastructure)."""

from __future__ import annotations

import uuid

import pytest
from fixtures.constants import BACKEND_CLASSIFICATION, MODEL_SOURCE_URI
from fixtures.helpers import registry_entries_for_alias, wait_for

pytestmark = pytest.mark.infrastructure


def _instance_payload(alias: str) -> dict:
    return {
        "config": {
            "backend_type": BACKEND_CLASSIFICATION["backend_type"],
            "alias": alias,
            "model_source": MODEL_SOURCE_URI,
            "device": "cpu",
            "dtype": "float32",
            "max_length": 128,
            "labels": ["LABEL_0", "LABEL_1", "LABEL_2", "LABEL_3", "LABEL_4"],
        },
        "priority": "staging",
    }


async def _alias_visible(http_control, alias: str) -> bool:
    resp = await http_control.get("/v1/models")
    if resp.status_code != 200:
        return False
    body = resp.json()
    names = {m.get("name") for m in body.get("models", [])} | {
        m.get("id") for m in body.get("data", [])
    }
    return alias in names


async def _alias_gone(http_control, alias: str) -> bool:
    return not await _alias_visible(http_control, alias)


async def test_alias_lifecycle(http_control, clean_state):
    """Registry entry appears when the instance runs, disappears on stop."""
    hosts = {h["name"]: h for h in (await http_control.get("/api/hosts")).json()}
    host = hosts["host-a"]
    alias = f"life-{uuid.uuid4().hex[:8]}"

    resp = await http_control.post(
        f"/api/hosts/{host['id']}/instances", json=_instance_payload(alias)
    )
    assert resp.status_code == 200, resp.text
    instance_id = resp.json()["instance"]["id"]

    # Not visible while stopped.
    await wait_for(
        lambda: _alias_gone(http_control, alias),
        timeout=30.0,
        interval=0.5,
        description="alias absent while stopped",
    )

    # Visible once running (registry refresh picks it up).
    resp = await http_control.post(
        f"/api/hosts/{host['id']}/instances/{instance_id}/start"
    )
    assert resp.status_code == 200, resp.text
    await wait_for(
        lambda: _alias_visible(http_control, alias),
        timeout=30.0,
        interval=0.5,
        description="alias visible while running",
    )

    # Gone again after stop.
    resp = await http_control.post(
        f"/api/hosts/{host['id']}/instances/{instance_id}/stop"
    )
    assert resp.status_code == 200, resp.text
    await wait_for(
        lambda: _alias_gone(http_control, alias),
        timeout=60.0,
        interval=0.5,
        description="alias gone after stop",
    )


async def _flush_host_state_like_redis_restart(redis_url: str) -> None:
    """Wipe the connection/instance/registry maps — what a Redis restart
    does to solar-control's view. Unlike _flush_volatile_redis this DELETES
    solar:hosts:*: the incident is the connection map itself being lost.

    The disconnect/reconnect timestamp maps go too: a real restart wipes
    them, and leaving a recent disconnect_ts behind would push hosts into
    the grace path (disconnect_grace_period_s=15) with an empty instance
    cache — no HTTP poll, no rebuild, and a false test failure.
    """
    import redis.asyncio as aioredis

    r = aioredis.from_url(redis_url, decode_responses=True)
    try:
        for key in (
            "solar:hosts:sids",
            "solar:hosts:connected",
            "solar:hosts:instances",
            "solar:hosts:disconnect_ts",
            "solar:hosts:reconnect_req_ts",
            "solar:registry",
        ):
            await r.delete(key)
    finally:
        await r.aclose()


async def _cache_reseeded(redis_url: str, host_id: str, instance_id: str) -> bool:
    from fixtures.seed import redis_cache_instances

    return any(
        inst.get("id") == instance_id
        for inst in redis_cache_instances(redis_url, host_id)
    )


async def test_registry_rebuilds_with_host_api_key_after_redis_flush(
    stack, http_control, clean_state
):
    """Redis restart: hosts look disconnected, the registry is rebuilt over
    HTTP polling — and entries must carry the host key, no control restart.

    No instance changes happen after the flush, so no instances_update can
    fire — a re-seeded instances cache is proof the HTTP poll ran (and the
    api_key assertion holds whichever path rebuilt the registry).
    """
    hosts = {h["name"]: h for h in (await http_control.get("/api/hosts")).json()}
    host = hosts["host-a"]
    alias = f"rekey-{uuid.uuid4().hex[:8]}"

    resp = await http_control.post(
        f"/api/hosts/{host['id']}/instances", json=_instance_payload(alias)
    )
    assert resp.status_code == 200, resp.text
    instance_id = resp.json()["instance"]["id"]

    resp = await http_control.post(
        f"/api/hosts/{host['id']}/instances/{instance_id}/start"
    )
    assert resp.status_code == 200, resp.text
    await wait_for(
        lambda: _alias_visible(http_control, alias),
        timeout=30.0,
        interval=0.5,
        description="alias visible before flush",
    )

    # Baseline: the pre-flush registry carries the host key.
    expected_key = stack.host_key("a")
    entries = await registry_entries_for_alias(stack.db_env["redis"], alias)
    assert entries and entries[0]["api_key"] == expected_key

    # Save the WS connection maps: the hosts' sockets survive the flush, so
    # control keeps ignoring their pushes while SID_MAP is empty — leaving
    # the flush in place would starve later tests of WS snapshots/pulls.
    import redis.asyncio as aioredis

    r = aioredis.from_url(stack.db_env["redis"], decode_responses=True)
    try:
        saved_sids = await r.hgetall("solar:hosts:sids")
        saved_connected = await r.hgetall("solar:hosts:connected")
    finally:
        await r.aclose()

    # Simulate the incident: control's Redis view of the world vanishes.
    await _flush_host_state_like_redis_restart(stack.db_env["redis"])

    try:
        # The registry must rebuild over HTTP polling within a few refresh
        # ticks (REGISTRY_REFRESH_INTERVAL_S=1.0 in the suite), WITHOUT a
        # control restart — and every entry carries the host key again.
        #
        # Sync on the rebuild state, not on either write alone: the poll
        # writes the instances cache a few ms before the registry inside the
        # same tick, so waiting on the cache and asserting the registry
        # could sample in the gap (and vice versa). The cache check also
        # proves the rebuild came from the HTTP poll rather than
        # carry-forward of the pre-flush registry.
        async def _recovered() -> bool:
            entries = await registry_entries_for_alias(stack.db_env["redis"], alias)
            return (
                bool(entries)
                and entries[0]["api_key"] == expected_key
                and await _cache_reseeded(
                    stack.db_env["redis"], host["id"], instance_id
                )
            )

        await wait_for(
            _recovered,
            timeout=30.0,
            interval=0.5,
            description="registry rebuilt with the host API key over HTTP poll",
        )
        entries = await registry_entries_for_alias(stack.db_env["redis"], alias)
        assert entries[0]["api_key"] == expected_key, entries[0]

        # The same control process still routes: classify succeeds.
        resp = await http_control.post(
            "/v1/classify",
            json={"model": alias, "input": "hello integration world"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["label"].startswith("LABEL_")
    finally:
        # Restore the connection maps so the rest of the session sees the hosts
        # connected again (their sockets were alive the whole time; the registry
        # and instances cache were re-seeded by the HTTP poll above). The
        # restore runs even when an assertion above fails: the maps are
        # session-scoped shared state with no other healing mechanism (control
        # never kicks unknown-sid sockets, so the hosts cannot re-register), and
        # a skipped restore poisons every later WS-dependent test — the
        # 2026-08-12 run-5 cascade (13 failures from one leaked mutation).
        r = aioredis.from_url(stack.db_env["redis"], decode_responses=True)
        try:
            if saved_sids:
                await r.hset("solar:hosts:sids", mapping=saved_sids)
            if saved_connected:
                await r.hset("solar:hosts:connected", mapping=saved_connected)
        finally:
            await r.aclose()
