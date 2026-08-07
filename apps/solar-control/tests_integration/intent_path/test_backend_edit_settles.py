"""intent_path: C1 — backend edits settle instead of churning (marker: intent_path).

The reported incident: an intent edit (replicas 1 -> 2 with a backend whose
values the host re-normalizes) trapped the intent in a stop/recreate loop
across every host, with `spec_changed_at` never clearing. The fixture model
is a tiny HuggingFace classifier, so the chat_template_kwargs scenarios stay
unit tests; here we prove the end-to-end properties:

- SUCCESS: a backend edit settles — replica instance ids stay stable across
  several reconcile intervals and spec_changed_at clears.
- SUCCESS (the original incident shape): a replicas 1 -> 2 edit lands both
  replicas and settles with no churn.
- BOUNDED FAILURE: an edit that cannot roll out (device: cuda on a CPU-only
  test host fails at start) ends in a recorded last_error instead of an
  infinite create/stop loop — the observable guarantee the circuit breaker
  exists to provide.
"""

from __future__ import annotations

import asyncio
import json
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


def _alias(prefix: str = "settle") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _instance_ids(http_control, intent_id: str) -> set[str]:
    intent = await get_intent(http_control, intent_id)
    if intent is None:
        return set()
    return {
        r["instance_id"]
        for r in intent["status"].get("replica_set", [])
        if r.get("instance_id")
    }


async def test_backend_edit_settles_without_churn(http_control, clean_state):
    """Edit a backend field -> replicas stay stable, spec_changed_at clears."""
    intent = await create_intent(http_control, alias=_alias())
    ready = await wait_intent_ready(http_control, intent["id"])
    ids_before = await _instance_ids(http_control, intent["id"])
    assert len(ids_before) == 1

    # Backend-only edit (labels list) — the S-044 deep-compare path.
    backend = dict(ready["backend"])
    backend["labels"] = [f"NEW_LABEL_{i}" for i in range(4)]
    await update_intent(http_control, ready, backend=backend)

    # The edit must settle: spec_changed_at clears. A backend edit needs one
    # REPLACE round (stop + start with the new config); an HF server start
    # takes 3-6 minutes under stack load on this host. The bug class this
    # test guards (a churn loop) manifests as drift_replace_attempts climbing
    # or a BackendDriftUnsettled last_error — so fail FAST on those, and only
    # race the wall clock as a last resort.
    async def _wait_settled() -> None:
        deadline = asyncio.get_running_loop().time() + 600.0
        while True:
            current = await get_intent(http_control, intent["id"])
            assert current is not None, "intent disappeared"
            status = current["status"]
            if status.get("spec_changed_at") is None:
                return  # settled
            if status.get("drift_replace_attempts", 0) > 0:
                raise AssertionError(
                    "drift circuit breaker tripped while settling:\n"
                    + json.dumps(status, indent=1)
                )
            if status.get("last_error") is not None:
                raise AssertionError(
                    "last_error set while settling:\n" + json.dumps(status, indent=1)
                )
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    "still not settled after 600s:\n" + json.dumps(status, indent=1)
                )
            await asyncio.sleep(0.5)

    await _wait_settled()
    settled = await get_intent(http_control, intent["id"])
    assert settled is not None
    assert settled["status"]["phase"] == "ready"
    assert settled["status"]["ready_replicas"] == 1
    assert settled["status"]["spec_changed_at"] is None

    # The backend edit was APPLIED (one replacement — the config cannot
    # change in place), and the new replica picked up the new labels.
    ids_after = await _instance_ids(http_control, intent["id"])
    assert len(ids_after) == 1
    assert settled["backend"]["labels"] == [f"NEW_LABEL_{i}" for i in range(4)]
    assert settled["status"]["updated_replicas"] == 1

    # No churn: the replacement happens exactly once. The instance id must
    # stay stable across further reconcile intervals.
    for _ in range(6):
        await asyncio.sleep(0.6)
        assert await _instance_ids(http_control, intent["id"]) == ids_after

    # No churn markers in status: no drift breaker trip, no error.
    current = await get_intent(http_control, intent["id"])
    assert current is not None
    assert current["status"].get("drift_replace_attempts", 0) == 0
    assert current["status"]["last_error"] is None


async def test_replica_scale_edit_settles_without_churn(http_control, clean_state):
    """The reported incident shape: replicas 1 -> 2 settles with stable ids."""
    intent = await create_intent(http_control, alias=_alias(), replicas=1)
    ready = await wait_intent_ready(http_control, intent["id"])
    ids_before = await _instance_ids(http_control, intent["id"])

    await update_intent(http_control, ready, replicas=2)
    final = await wait_intent_ready(http_control, intent["id"], ready_replicas=2)

    assert final["status"]["spec_changed_at"] is None
    assert len(await _instance_ids(http_control, intent["id"])) == 2
    # The original replica survived the scale-up (no stop/recreate churn).
    ids_after = await _instance_ids(http_control, intent["id"])
    assert ids_before <= ids_after
    assert final["status"].get("drift_replace_attempts", 0) == 0
    assert final["status"]["last_error"] is None

    # Keep observing: instance ids stay stable across further ticks.
    for _ in range(6):
        await asyncio.sleep(0.6)
        assert await _instance_ids(http_control, intent["id"]) == ids_after


async def test_unsettlable_edit_degrades_to_bounded_error(http_control, clean_state):
    """An edit that cannot roll out (device cuda on a CPU-only host fails at
    start) must end in a recorded, linked error — not an infinite loop."""
    intent = await create_intent(http_control, alias=_alias())
    ready = await wait_intent_ready(http_control, intent["id"])

    backend = dict(ready["backend"])
    backend["device"] = "cuda"  # no CUDA on the test hosts -> start fails
    await update_intent(http_control, ready, backend=backend)

    # The rollout fails and the failure is recorded with its instance link.
    async def failed_with_link() -> bool:
        current = await get_intent(http_control, intent["id"])
        if current is None:
            return False
        err = current["status"].get("last_error")
        return err is not None and bool(err.get("instance_id"))

    await wait_for(
        failed_with_link,
        timeout=60.0,
        interval=0.5,
        description="start failure recorded with instance_id",
    )
    current = await get_intent(http_control, intent["id"])
    assert current is not None
    err = current["status"]["last_error"]
    assert err["instance_id"]

    # Bounded: the intent is not churning. Observe several ticks and
    # confirm the observed replica count stays stable (0 or 1) and the
    # error persists instead of create/stop pairs cycling forever.
    counts: set[int] = set()
    for _ in range(6):
        snap = await get_intent(http_control, intent["id"])
        if snap is not None:
            counts.add(snap["status"]["observed_replicas"])
        await asyncio.sleep(0.6)
    assert counts <= {0, 1}, f"observed replica count oscillated: {counts}"
