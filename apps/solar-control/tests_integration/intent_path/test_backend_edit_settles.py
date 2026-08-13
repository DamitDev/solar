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
- BREAKER STATE SURVIVES A RELOAD: a counter already at its bound still
  reads as tripped on the next tick, so the loop actually ends.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from fixtures.helpers import wait_for
from fixtures.intents import (
    create_intent,
    get_intent,
    update_intent,
    wait_intent_ready,
)

pytestmark = pytest.mark.intent_path

# Any value >= settings.max_drift_replace_attempts (default 3) trips the
# breaker; 99 keeps this test correct if that default is ever raised.
_TRIPPED_ATTEMPTS = 99

# max_length is carried in the host's stored instance config, so changing it
# behind control's back is visible to the deep compare. device/dtype/labels
# would work equally well; max_length just has no side effect on the start.
_DRIFTED_MAX_LENGTH = 999


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

    try:
        await wait_for(
            failed_with_link,
            timeout=60.0,
            interval=0.5,
            description="start failure recorded with instance_id",
        )
    except AssertionError as exc:
        state = await get_intent(http_control, intent["id"])
        raise AssertionError(
            "the unsettlable edit settled. A ready phase with no last_error "
            "means the cuda start SUCCEEDED — this test needs a CPU-only "
            f"host (see CUDA_VISIBLE_DEVICES in conftest).\nintent={state}"
        ) from exc
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


async def _replica_configs(http_control, intent_id: str) -> list[dict]:
    """Each replica's stored config as its host reports it.

    This is the payload the deep compare runs against: control's
    ``/api/hosts/{id}/instances`` proxies straight to the host, which is where
    ``capture_instance_config`` reads from too. A dump whose config carries no
    ``max_length`` is the silent degradation this test used to die on — when
    ``capture_instance_config`` fails transiently, ``_detect_backend_drift``
    falls back to the flat WS cache, which does not carry the field, so no
    drift is seen and no error is ever written.
    """
    intent = await get_intent(http_control, intent_id)
    if intent is None:
        return [{"error": "intent disappeared"}]
    out: list[dict] = []
    for replica in intent["status"].get("replica_set", []):
        host_id = replica.get("host_id")
        instance_id = replica.get("instance_id")
        entry: dict = {"host_id": host_id, "instance_id": instance_id}
        if not host_id or not instance_id:
            out.append(entry)
            continue
        try:
            resp = await http_control.get(f"/api/hosts/{host_id}/instances")
            if resp.status_code != 200:
                entry["probe"] = f"HTTP {resp.status_code} {resp.text[:200]}"
            else:
                match = [i for i in resp.json() if i.get("id") == instance_id]
                entry["config"] = match[0].get("config") if match else "not on host"
        except Exception as exc:  # noqa: BLE001
            entry["probe"] = f"{type(exc).__name__}: {exc}"
        out.append(entry)
    return out


async def _wait_or_dump(
    http_control,
    stack,
    intent_id: str,
    condition,
    *,
    timeout: float,
    description: str,
) -> None:
    """``wait_for``, but a timeout carries the evidence needed to triage it.

    Without this the failure mode is a bare "timed out waiting for X", which
    cannot distinguish "the drift was never detected" from "the drift was
    detected but the config the host reports has no max_length to compare".
    """
    try:
        await wait_for(
            condition, timeout=timeout, interval=0.5, description=description
        )
    except AssertionError:
        current = await get_intent(http_control, intent_id)
        status = current["status"] if current else None
        configs = await _replica_configs(http_control, intent_id)
        raise AssertionError(
            f"timed out after {timeout}s waiting for {description}\n"
            f"--- intent status ---\n{json.dumps(status, indent=1, default=str)}\n"
            f"--- replica configs on the hosts ---\n"
            f"{json.dumps(configs, indent=1, default=str)}\n"
            f"--- service logs ---\n{stack.tail()}"
        ) from None


def _plant_unsettleable_drift(control_db: str, intent_id: str) -> None:
    """Plant a drift vector with the breaker already at its bound.

    Written straight to Postgres rather than through PUT /api/intents/{id}
    for two reasons: the update route resets the counter (that is its job),
    and a spec the API accepts is one the host can honour, which would
    settle on the first REPLACE. Here the spec and the running instance
    provably disagree and the breaker is already spent, which is the state
    an operator reaches after max_drift_replace_attempts real rounds.

    ``spec_changed_at`` is what opens the deep-compare window — without it
    the reconciler only sees the flat WS cache, which does not carry
    max_length, and the drift would be invisible.
    """
    import psycopg2

    conn = psycopg2.connect(control_db)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE intents SET backend = backend || %s::jsonb, "
                "status = status || %s::jsonb WHERE id = %s",
                (
                    json.dumps({"max_length": _DRIFTED_MAX_LENGTH}),
                    json.dumps(
                        {
                            "spec_changed_at": datetime.now(timezone.utc).isoformat(),
                            "drift_replace_attempts": _TRIPPED_ATTEMPTS,
                        }
                    ),
                    intent_id,
                ),
            )
    finally:
        conn.close()


async def test_tripped_breaker_survives_the_reload(http_control, stack, clean_state):
    """C1/F1: the breaker's counter has to survive the trip back through the
    database, or it can never reach its bound.

    The reconciler keeps no cross-tick state of its own, so every tick reads
    ``drift_replace_attempts`` back off the intent. When that read silently
    yielded the field default, the breaker reset to zero on every tick, kept
    planning REPLACEs, and the intent churned forever — the exact loop the
    breaker exists to stop.

    Seeding the counter at its bound makes the regression observable in one
    tick instead of waiting for real rounds to accumulate: with hydration the
    intent reports BackendDriftUnsettled and stops replacing; without it the
    counter loads as 0, a REPLACE is planned, and the replica is recreated
    with the drifted spec — so this test fails on the timeout rather than on
    a wrong value.
    """
    intent = await create_intent(http_control, alias=_alias("breaker"))
    await wait_intent_ready(http_control, intent["id"])
    ids_before = await _instance_ids(http_control, intent["id"])
    assert len(ids_before) == 1

    _plant_unsettleable_drift(stack.db_env["control_db"], intent["id"])

    # Staged waits. The planted UPDATE has to be readable before the breaker
    # can trip at all, so a timeout on this first stage is a different failure
    # (the SQL never landed, or the read model drops spec_changed_at) from the
    # drift never being detected — worth separating, because the second stage
    # is the slow one and its diagnostics only make sense once the window is
    # provably open.
    async def drift_window_open() -> bool:
        current = await get_intent(http_control, intent["id"])
        return (
            current is not None and current["status"].get("spec_changed_at") is not None
        )

    await _wait_or_dump(
        http_control,
        stack,
        intent["id"],
        drift_window_open,
        timeout=30.0,
        description="planted spec_changed_at readable on the intent",
    )

    # Either observable counts as tripped. drift_unsettled_keys is what the
    # diff path writes; last_error carries the message only when no host
    # action failed this tick (a failed action takes precedence), so keying
    # solely off the error code can time out on a breaker that did trip.
    async def breaker_tripped() -> bool:
        current = await get_intent(http_control, intent["id"])
        if current is None:
            return False
        status = current["status"]
        if status.get("drift_unsettled_keys"):
            return True
        err = status.get("last_error")
        return err is not None and err.get("code") == "BackendDriftUnsettled"

    # 120s, not 60s: reaching the breaker needs a deep compare, and
    # capture_instance_config falls back to an HTTP GET /instances (5s cap
    # per try) whenever the Redis entry has no full config. With both hosts
    # and their model servers on one machine that fallback is common enough
    # that 60s left no margin for the retry.
    await _wait_or_dump(
        http_control,
        stack,
        intent["id"],
        breaker_tripped,
        timeout=120.0,
        description=(
            "backend drift recorded as unsettled (a timeout here means the "
            "breaker never tripped — check that drift_replace_attempts is "
            "hydrated in _row_to_response, and that the dumped replica config "
            "carries max_length at all)"
        ),
    )

    current = await get_intent(http_control, intent["id"])
    assert current is not None
    status = current["status"]
    _dump = json.dumps(status, indent=1, default=str)

    # The persisted keys come first: they are what the diff path observed.
    # last_error can be occupied by an unrelated failed action, so asserting
    # on it first would report "no breaker" for a breaker that did trip.
    assert status["drift_unsettled_keys"] == ["max_length"], _dump
    assert status["last_error"] is not None, f"drift unsettled, no error:\n{_dump}"
    assert status["last_error"]["code"] == "BackendDriftUnsettled", _dump
    assert "max_length" in status["last_error"]["message"], _dump
    # Carried forward, not recounted from zero: no REPLACE ran this round.
    assert status["drift_replace_attempts"] >= _TRIPPED_ATTEMPTS, _dump

    degraded = [c for c in status.get("conditions", []) if c["type"] == "Degraded"]
    assert degraded, f"no Degraded condition:\n{_dump}"
    assert degraded[0]["reason"] == "DriftUnsettled", _dump

    # The point of the breaker: the replica is left alone instead of being
    # stopped and recreated on every tick.
    for _ in range(6):
        await asyncio.sleep(0.6)
        assert await _instance_ids(http_control, intent["id"]) == ids_before
