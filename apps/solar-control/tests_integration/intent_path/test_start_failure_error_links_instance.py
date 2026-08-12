"""intent_path: C2 — start failures are linked to their instance logs
(marker: intent_path).

The reported symptom: a `502 ... Process exited unexpectedly (exit code: 1)`
reached the webui with no way to read the process log. The fixture host is
CPU-only, so a `device: cuda` backend is a deterministic start failure: the
HF server process exits at startup and the host answers with the structured
failure body.

- FAILURE: the intent's last_error carries instance_id and a log_tail.
- FAILURE: the process logs remain readable via the proxied logs endpoint
  after the failure (retained buffer / instance-addressable file).
- SUCCESS: a healthy instance's logs endpoint returns its output too.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.helpers import wait_for
from fixtures.intents import (
    create_intent,
    get_intent,
    update_intent,
    wait_intent_ready,
)

pytestmark = pytest.mark.intent_path


def _alias(prefix: str = "fail") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _host_of_replica(http_control, intent_id: str) -> str | None:
    intent = await get_intent(http_control, intent_id)
    if intent is None:
        return None
    for replica in intent["status"].get("replica_set", []):
        if replica.get("host_id"):
            return replica["host_id"]
    return None


async def test_start_failure_links_instance_and_logs(http_control, clean_state):
    """device: cuda on a CPU-only host -> last_error.instance_id set and the
    process logs stay readable after the failure."""
    intent = await create_intent(http_control, alias=_alias())
    ready = await wait_intent_ready(http_control, intent["id"])
    host_id = await _host_of_replica(http_control, intent["id"])
    assert host_id is not None

    backend = dict(ready["backend"])
    backend["device"] = "cuda"
    await update_intent(http_control, ready, backend=backend)

    async def failed_with_link() -> bool:
        current = await get_intent(http_control, intent["id"])
        if current is None:
            return False
        err = current["status"].get("last_error")
        if not err:
            return False
        # The C2 fields: instance_id set, and either a log_tail from the
        # host's structured body or a readable logs endpoint.
        return bool(err.get("instance_id"))

    try:
        await wait_for(
            failed_with_link,
            timeout=60.0,
            interval=0.5,
            description="start failure recorded with instance link",
        )
    except AssertionError as exc:
        state = await get_intent(http_control, intent["id"])
        raise AssertionError(
            "no start failure was recorded. A ready phase with no last_error "
            "means the cuda start SUCCEEDED — this test needs a CPU-only "
            f"host (see CUDA_VISIBLE_DEVICES in conftest).\nintent={state}"
        ) from exc
    current = await get_intent(http_control, intent["id"])
    assert current is not None
    err = current["status"]["last_error"]
    instance_id = err["instance_id"]
    assert instance_id

    # The recorded message names the exit (not a bare timeout).
    assert "Process exited" in err["message"] or "exit code" in err["message"]

    # Logs are still readable through control's proxied endpoint — the
    # retained buffer and/or the instance-addressable log file survive the
    # failure (C2).
    resp = await http_control.get(f"/api/hosts/{host_id}/instances/{instance_id}/logs")
    assert resp.status_code == 200, resp.text
    lines = resp.json()
    assert isinstance(lines, list) and len(lines) > 0
    joined = "\n".join(m.get("line", "") for m in lines).lower()
    assert "cuda" in joined or "error" in joined or "traceback" in joined


async def test_healthy_instance_logs_endpoint_success(http_control, clean_state):
    """SUCCESS state: a running replica's logs endpoint returns its output."""
    intent = await create_intent(http_control, alias=_alias())
    ready = await wait_intent_ready(http_control, intent["id"])
    host_id = await _host_of_replica(http_control, intent["id"])
    assert host_id is not None
    replica = ready["status"]["replica_set"][0]
    instance_id = replica["instance_id"]

    resp = await http_control.get(f"/api/hosts/{host_id}/instances/{instance_id}/logs")
    assert resp.status_code == 200, resp.text
    lines = resp.json()
    assert isinstance(lines, list)
    # A started server prints at least something (uvicorn banner etc.);
    # the exact content varies, so assert the shape only.
    assert all({"seq", "timestamp", "line"} <= set(m) for m in lines)
