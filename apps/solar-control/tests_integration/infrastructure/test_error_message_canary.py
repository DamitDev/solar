"""infrastructure: canary probe for the error_message leak surface (IT Sec #97).

Fires a canary-carrying request whose error path persists the canary into
the gateway request log, then asserts what ``error_message`` carries. Two
paths prove the surface:

1. A request for a nonexistent model — the gateway's model-not-found error
   carries the model name (user-controlled input) verbatim.
2. A request routed to a real instance whose upstream 4xx body echoes the
   request payload — the upstream echo path
   (``app/gateway.py``'s ``Request failed: {status} - {error_text}``).

Documents the leak surface; the trim is driven by these assertions. The
gateway request registry (``gateway_requests`` / ``gateway_events``) is the
authoritative record — the same pattern as the #985/#996 line of work.
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
        assert any(canary_model in message for message in messages), (
            f"canary model name not found in error messages: {messages[:3]}"
        )
    finally:
        await close_redis()


async def test_upstream_error_persists_into_error_message(
    stack, http_control, clean_state
):
    """A request routed to the real instance whose upstream 4xx echoes the
    payload: the upstream echo path persists the body text into
    error_message (IT Sec #97's trim target)."""
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

    # /v1/chat/completions on a classification instance: the upstream 400s
    # ("Chat completions only available for causal or vision models") — the
    # gateway persists that body text into error_message.
    resp = await http_control.post(
        "/v1/chat/completions",
        json={"model": MODEL_ALIAS, "messages": [{"role": "user", "content": CANARY}]},
        headers={"X-API-Key": stack.secrets["management"]},
    )
    # The gateway converts the upstream 4xx into an OpenAI-shaped 404 via
    # _raise_model_not_found (routes/openai.py) after emitting the error.
    assert resp.status_code == 404, resp.text

    await init_redis(stack.db_env["redis"])
    try:
        resp = await http_control.get("/api/gateway/requests")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body.get("items", body)
        messages = _error_messages(items)
        upstream_messages = [m for m in messages if m.startswith("Request failed: ")]
        assert upstream_messages, "error_message must carry the upstream error"
        # IT Sec #97 trim: the echoed upstream body is capped at
        # _MAX_UPSTREAM_ERROR_CHARS (200); the persisted message carries the
        # status prefix plus at most that many chars.
        assert all(
            len(m) <= len("Request failed: 400 - ") + 200 for m in upstream_messages
        ), f"uncapped upstream error persisted: {upstream_messages[:1]}"
    finally:
        await close_redis()
