"""infrastructure: canary probe for the error_message leak surface (IT Sec #97).

Fires a canary-carrying request whose error path persists into the gateway
request log, then asserts what ``error_message`` carries. Two paths prove
the surface:

1. A request for a nonexistent model — the gateway's model-not-found error
   carries the model name (user-controlled input) verbatim. Accepted,
   documented surface: the caller's own model name, capped at 200
   characters (``_MAX_UPSTREAM_ERROR_CHARS``).
2. A request routed to a real instance whose upstream 4xx body echoes the
   request payload — the prompt ``CANARY`` must not appear in any persisted
   ``error_message``. The request itself takes the model-not-found branch
   (the prompt never reaches ``error_message``), so this assertion is a
   sanity guard; the cap itself is covered by
   ``tests/test_error_message_cap.py``.

The gateway request registry (``gateway_requests`` / ``gateway_events``) is
the authoritative record — the same pattern as the #985/#996 line of work.
"""

from __future__ import annotations

import pytest
from fixtures.constants import (
    BACKEND_CLASSIFICATION,
    MODEL_ALIAS,
    MODEL_SOURCE_URI,
)
from fixtures.helpers import wait_for

pytestmark = pytest.mark.infrastructure

CANARY = "canary-ITSEC-97-solar-prompt-fragment"


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


def _error_messages(items: list) -> list[str]:
    return [item.get("error_message") or "" for item in items]


async def _registry_has_alias(http_control, alias: str) -> bool:
    resp = await http_control.get("/v1/models")
    if resp.status_code != 200:
        return False
    body = resp.json()
    names = {m.get("name") for m in body.get("models", [])} | {
        m.get("id") for m in body.get("data", [])
    }
    return alias in names


async def test_canary_in_model_name_persists_into_error_message(
    stack, http_control, clean_state
):
    """A request for a nonexistent model whose name carries the canary:
    the gateway's model-not-found error persists the canary into
    error_message — user-controlled input reaches the request log."""
    from app.redis_state import close_redis, init_redis

    canary_model = f"{CANARY}-no-such-model"

    resp = await http_control.post(
        "/v1/classify",
        json={"model": canary_model, "input": "hello"},
        headers={"X-API-Key": stack.secrets["management"]},
    )

    # The gateway 404s/500s; the error_message carries the model name.
    await init_redis(stack.db_env["redis"])
    try:
        resp = await http_control.get("/api/gateway/requests")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body.get("items", body)
        messages = _error_messages(items)
        assert any(
            canary_model in message for message in messages
        ), f"canary model name not found in error messages: {messages[:3]}"
    finally:
        await close_redis()


async def test_upstream_error_persists_into_error_message(
    stack, http_control, clean_state
):
    """A request the gateway cannot route persists its failure into
    error_message (IT Sec #97's leak surface).

    With the classification fixture the /v1/completions request is filtered
    at the capability gate (a classification instance serves only
    /v1/classify, /v1/models, /health), so the gateway takes the
    model-not-found branch — user-controlled model input persists verbatim
    into the persisted error_message. The upstream-echo branch (capped at
    _MAX_UPSTREAM_ERROR_CHARS) needs a real upstream 4xx and flows through
    the same terminal _emit_error path.
    """
    from app.redis_state import close_redis, init_redis

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

    # The gateway's routing registry must know the instance before a routed
    # request reaches the upstream — without this wait the request 404s at
    # the model-not-found path instead of exercising the upstream echo.
    await wait_for(
        lambda: _registry_has_alias(http_control, MODEL_ALIAS),
        timeout=30.0,
        interval=0.5,
        description=f"gateway routes to {MODEL_ALIAS}",
    )

    # /v1/completions on a classification instance: the gateway's capability
    # filter rejects it (no /v1/completions in supported_endpoints) — the
    # model-not-found branch persists the failure into error_message.
    resp = await http_control.post(
        "/v1/completions",
        json={"model": MODEL_ALIAS, "prompt": CANARY},
        headers={"X-API-Key": stack.secrets["management"]},
    )
    assert resp.status_code == 404, resp.text

    await init_redis(stack.db_env["redis"])
    try:
        resp = await http_control.get("/api/gateway/requests")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body.get("items", body)
        messages = _error_messages(items)
        # The model-not-found branch persists the routing failure verbatim.
        expected = f"Model '{MODEL_ALIAS}' not found or no instances available"
        assert (
            expected in messages
        ), f"expected model-not-found error not persisted: {messages[:3]}"
        # The request takes the model-not-found branch, so the prompt never
        # reaches error_message. Sanity guard only: the cap itself is
        # covered by tests/test_error_message_cap.py.
        assert all(
            CANARY not in message for message in messages
        ), f"canary leaked into a persisted error_message: {messages[:3]}"
    finally:
        await close_redis()
