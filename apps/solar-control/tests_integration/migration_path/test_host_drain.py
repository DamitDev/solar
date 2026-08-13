"""migration_path: host draining (marker: migration_path).

S-043 end to end: the preflight refuses a host with a running manual
instance, a drain evacuates the intent-managed replica to the other host
and completes, a draining host accepts no new instances, and resuming
returns it to service — including for a second drain that moves the
replica back, which only works if the first drain left nothing behind.

Specification: training-platform-project/docs/specs/host-draining.md
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fixtures.constants import BACKEND_CLASSIFICATION, MODEL_SOURCE_URI
from fixtures.helpers import wait_for
from fixtures.intents import create_intent, get_intent, wait_intent_ready

pytestmark = pytest.mark.migration_path


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


async def _drain_status(http_control, host_id: str) -> dict:
    resp = await http_control.get(f"/api/hosts/{host_id}/drain")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _resume(http_control, host_id: str) -> dict:
    resp = await http_control.delete(f"/api/hosts/{host_id}/drain")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _other_host_id(http_control, host_id: str) -> str:
    hosts = (await http_control.get("/api/hosts")).json()
    return next(h["id"] for h in hosts if h["id"] != host_id)


async def _sample_alias_running(
    http_control, host_ids: list[str], alias: str, stop: asyncio.Event
) -> list[int]:
    """Record how many instances serve *alias* across *host_ids* until
    *stop* is set; return the per-sample running counts.

    S-057 continuity check: a drain must never reduce serving capacity,
    so no sample may read zero running instances.
    """
    counts: list[int] = []
    while not stop.is_set():
        running = 0
        for host_id in host_ids:
            resp = await http_control.get(f"/api/hosts/{host_id}/instances")
            if resp.status_code == 200:
                for i in resp.json():
                    cfg = i.get("config") or {}
                    inst_alias = cfg.get("alias") or i.get("alias")
                    if inst_alias == alias and i.get("status") == "running":
                        running += 1
        counts.append(running)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass
    return counts


async def test_drain_evacuates_replica_and_completes(http_control, stack, clean_state):
    """Drain -> replica migrated to the other host, host reaches drained.

    The replica is the intent's only one, so this also proves the drain
    does not cost serving capacity: the alias keeps at least one running
    instance at every sampled point of the evacuation (S-057
    create-then-stop), and the intent is ready again on the target.
    """
    alias = f"drain-{uuid.uuid4().hex[:8]}"
    intent = await create_intent(http_control, alias=alias)
    ready = await wait_intent_ready(http_control, intent["id"])
    source_host_id = ready["status"]["replica_set"][0]["host_id"]
    source_instance_id = ready["status"]["replica_set"][0]["instance_id"]
    target_host_id = await _other_host_id(http_control, source_host_id)

    # S-057: sample the alias's running count across the fleet while the
    # drain runs. A drain must never reduce serving capacity — the alias
    # has to keep at least one running instance at every sample point.
    # (The pre-fix implementation stopped the source before the target
    # was up; the gap is exactly what these samples catch.)
    stop_sampling = asyncio.Event()
    sampler = asyncio.create_task(
        _sample_alias_running(
            http_control, [source_host_id, target_host_id], alias, stop_sampling
        )
    )

    resp = await http_control.post(f"/api/hosts/{source_host_id}/drain")
    assert resp.status_code == 202, resp.text
    accepted = resp.json()
    assert accepted["drain_state"] == "draining"
    assert accepted["managed_remaining"] == 1
    assert accepted["blockers"] == []

    # Evacuation is a migration: new instance on the target, intent ready.
    async def evacuated() -> bool:
        state = await get_intent(http_control, intent["id"])
        if state is None:
            return False
        replicas = state["status"].get("replica_set", [])
        return bool(replicas) and all(
            r.get("host_id") == target_host_id for r in replicas
        )

    try:
        await wait_for(
            evacuated,
            timeout=120.0,
            interval=0.5,
            description="managed replica evacuated to the other host",
        )
    except AssertionError as exc:
        status = await _drain_status(http_control, source_host_id)
        state = await get_intent(http_control, intent["id"])
        raise AssertionError(
            f"replica never left the draining host; drain={status} "
            f"intent={state}\n{stack.tail()}"
        ) from exc
    finally:
        stop_sampling.set()
        continuity = await sampler

    # The alias kept serving at every sampled point: the drain never cost
    # serving capacity (S-057 — create-then-stop evacuation).
    assert all(
        c >= 1 for c in continuity
    ), f"alias '{alias}' had zero running instances during the drain: {continuity}"

    # The sweep promotes the emptied host once nothing managed remains.
    await wait_for(
        lambda: _drained(http_control, source_host_id),
        timeout=60.0,
        interval=0.5,
        description="host promoted to drained",
    )
    status = await _drain_status(http_control, source_host_id)
    assert status["managed_remaining"] == 0
    assert status["stalled"] is False

    # The replacement replica on the target went through a cold start —
    # log-gated readiness means the load time counts against this budget.
    final = await wait_intent_ready(http_control, intent["id"], timeout=180.0)
    replica = final["status"]["replica_set"][0]
    assert replica["host_id"] == target_host_id
    assert replica["instance_id"] != source_instance_id
    assert replica["state"] == "running"

    # The evacuated source is gone, not merely stopped: a leftover would keep
    # serving the alias on this host, so placement would exclude it for the
    # intent and the replica could never come back.
    resp = await http_control.get(f"/api/hosts/{source_host_id}/instances")
    assert resp.status_code == 200, resp.text
    left_behind = [
        i
        for i in resp.json()
        if ((i.get("config") or {}).get("alias") or i.get("alias")) == alias
    ]
    assert left_behind == [], f"drain left instances behind: {left_behind}"

    # Back to service: the drain state clears and placement may use it again.
    resumed = await _resume(http_control, source_host_id)
    assert resumed["drain_state"] is None

    # Proof the host is reusable: draining the target moves the replica back.
    resp = await http_control.post(f"/api/hosts/{target_host_id}/drain")
    assert resp.status_code == 202, resp.text
    try:
        await wait_for(
            lambda: _replica_on(http_control, intent["id"], source_host_id),
            timeout=120.0,
            interval=0.5,
            description="replica evacuated back to the resumed host",
        )
    except AssertionError as exc:
        status = await _drain_status(http_control, target_host_id)
        raise AssertionError(
            f"replica could not return to the resumed host; drain={status}\n"
            f"{stack.tail()}"
        ) from exc
    finally:
        await _resume(http_control, target_host_id)


async def _replica_on(http_control, intent_id: str, host_id: str) -> bool:
    state = await get_intent(http_control, intent_id)
    if state is None:
        return False
    replicas = state["status"].get("replica_set", [])
    return bool(replicas) and all(r.get("host_id") == host_id for r in replicas)


async def _drained(http_control, host_id: str) -> bool:
    return (await _drain_status(http_control, host_id))["drain_state"] == "drained"


async def test_drain_blocked_by_running_manual_instance(
    http_control, stack, clean_state
):
    """Draining never moves manual instances, so a running one blocks it."""
    hosts = (await http_control.get("/api/hosts")).json()
    host_id = hosts[0]["id"]
    alias = f"manual-{uuid.uuid4().hex[:8]}"

    resp = await http_control.post(
        f"/api/hosts/{host_id}/instances", json=_instance_payload(alias)
    )
    assert resp.status_code == 200, resp.text
    instance_id = resp.json()["instance"]["id"]

    resp = await http_control.post(
        f"/api/hosts/{host_id}/instances/{instance_id}/start"
    )
    assert resp.status_code == 200, resp.text
    await wait_for(
        lambda: _instance_running(http_control, host_id, instance_id),
        timeout=90.0,
        interval=0.5,
        description="manual instance running",
    )

    resp = await http_control.post(f"/api/hosts/{host_id}/drain")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    blockers = detail["blockers"]
    assert [b["kind"] for b in blockers] == ["manual_instance"]
    assert blockers[0]["id"] == instance_id

    # Refused, not half-applied: the host is still in service.
    assert (await _drain_status(http_control, host_id))["drain_state"] is None

    # Stopping it clears the blocker and the drain is accepted.
    resp = await http_control.post(f"/api/hosts/{host_id}/instances/{instance_id}/stop")
    assert resp.status_code == 200, resp.text
    await wait_for(
        lambda: _drain_preflight_clear(http_control, host_id),
        timeout=30.0,
        interval=0.5,
        description="manual instance no longer blocks the drain",
    )

    resp = await http_control.post(f"/api/hosts/{host_id}/drain")
    assert resp.status_code == 202, resp.text
    assert resp.json()["drain_state"] == "draining"

    await _resume(http_control, host_id)


async def _instance_running(http_control, host_id: str, instance_id: str) -> bool:
    resp = await http_control.get(f"/api/hosts/{host_id}/instances")
    if resp.status_code != 200:
        return False
    return any(
        i.get("id") == instance_id and i.get("status") == "running" for i in resp.json()
    )


async def _drain_preflight_clear(http_control, host_id: str) -> bool:
    return not (await _drain_status(http_control, host_id))["blockers"]


async def test_draining_host_accepts_no_new_instances(http_control, clean_state):
    """Manual creation bypasses placement, so the route has to refuse it.

    Both out-of-service states refuse. With nothing to evacuate, the drain
    POST's ``reconciler.wake()`` promotes ``draining`` to ``drained`` within
    milliseconds (measured: 14ms), so pinning the message to one of the two
    made this a coin flip — the refusal is what the route owes us here.
    """
    hosts = (await http_control.get("/api/hosts")).json()
    host_id = hosts[0]["id"]

    resp = await http_control.post(f"/api/hosts/{host_id}/drain")
    assert resp.status_code == 202, resp.text

    try:
        resp = await http_control.post(
            f"/api/hosts/{host_id}/instances",
            json=_instance_payload(f"rejected-{uuid.uuid4().hex[:6]}"),
        )
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert "does not accept new instances" in detail, detail
        assert any(state in detail for state in ("draining", "drained")), detail

        # Drain is idempotent while it is in progress.
        resp = await http_control.post(f"/api/hosts/{host_id}/drain")
        assert resp.status_code == 202, resp.text
        assert resp.json()["drain_state"] in ("draining", "drained")
    finally:
        await _resume(http_control, host_id)

    resp = await http_control.post(
        f"/api/hosts/{host_id}/instances",
        json=_instance_payload(f"accepted-{uuid.uuid4().hex[:6]}"),
    )
    assert resp.status_code == 200, resp.text
