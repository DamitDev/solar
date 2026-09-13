"""infrastructure: the gateway maintains the per-request active registry
(marker: infrastructure).

Every routed request flows through ``_broadcast_routing_event``, the single
choke point feeding the ``solar:active-req:*`` registry that the routing
snapshot projects. The event-to-store wiring has no unit coverage; here a
real classify through the live gateway must leave the registry empty after
the terminal event — the invariant that keeps the WebUI's routing view free
of stuck 'processing' rows (delete on success/error, TTL on crash).
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.constants import (
    BACKEND_CLASSIFICATION,
    MODEL_ALIAS,
    MODEL_SOURCE_URI,
)
from fixtures.helpers import wait_for

pytestmark = pytest.mark.infrastructure


def _instance_payload() -> dict:
    return {
        "config": {
            "backend_type": BACKEND_CLASSIFICATION["backend_type"],
            "alias": MODEL_ALIAS,
            "model_source": MODEL_SOURCE_URI,
            "device": "cpu",
            "dtype": "float32",
            "max_length": 128,
            "labels": ["LABEL_0", "LABEL_1", "LABEL_2", "LABEL_3", "LABEL_4"],
        },
        "priority": "staging",
    }


async def _host_a(http_control) -> dict:
    hosts = (await http_control.get("/api/hosts")).json()
    return next(h for h in hosts if h["name"] == "host-a")


async def _instance_running(http_control, host_id: str, instance_id: str) -> bool:
    resp = await http_control.get(f"/api/hosts/{host_id}/instances")
    if resp.status_code != 200:
        return False
    for inst in resp.json():
        if inst.get("id") == instance_id and inst.get("status") == "running":
            return True
    return False


async def _registry_has_alias(http_control, alias: str) -> bool:
    resp = await http_control.get("/v1/models")
    if resp.status_code != 200:
        return False
    body = resp.json()
    names = {m.get("name") for m in body.get("models", [])} | {
        m.get("id") for m in body.get("data", [])
    }
    return alias in names


async def test_classify_leaves_request_registry_empty(stack, http_control, clean_state):
    """A successful classify creates, routes and then removes its registry
    entry through the real gateway path — no stuck 'processing' rows."""
    from app.redis_state import close_redis, init_redis
    from app.redis_state.routing import RoutingStore

    host = await _host_a(http_control)
    resp = await http_control.post(
        f"/api/hosts/{host['id']}/instances", json=_instance_payload()
    )
    assert resp.status_code == 200, resp.text
    instance_id = resp.json()["instance"]["id"]

    resp = await http_control.post(
        f"/api/hosts/{host['id']}/instances/{instance_id}/start"
    )
    assert resp.status_code == 200, resp.text

    await wait_for(
        lambda: _instance_running(http_control, host["id"], instance_id),
        timeout=90.0,
        interval=0.5,
        description=f"instance {instance_id} running",
    )
    await wait_for(
        lambda: _registry_has_alias(http_control, MODEL_ALIAS),
        timeout=30.0,
        interval=0.5,
        description=f"gateway routes to {MODEL_ALIAS}",
    )

    resp = await http_control.post(
        "/v1/classify",
        json={"model": MODEL_ALIAS, "input": f"hello registry {uuid.uuid4().hex[:8]}"},
        headers={"X-API-Key": stack.secrets["management"]},
    )
    assert resp.status_code == 200, resp.text

    # See test_instance_state_ttl: init the shared client for this process
    # only — the pytest process never runs the app lifespan.
    await init_redis(stack.db_env["redis"])
    try:
        store = RoutingStore()

        # request_success is awaited inline before the classify response
        # returns, so the registry converges immediately; the bounded wait
        # only absorbs scheduling jitter, never a missing delete.
        await wait_for(
            lambda: _registry_empty(store),
            timeout=5.0,
            interval=0.1,
            description="active request registry empty after terminal event",
        )
    finally:
        await close_redis()


async def _registry_empty(store) -> bool:
    return await store.list_requests() == []
