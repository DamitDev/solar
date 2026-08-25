"""infrastructure: S-058 — extensive multi-GPU & GPU-pinning coverage.

The real fleet cannot be exercised for this feature (dev machine has no
GPU, CI has none), so the simulated environment carries the load. Every
test spawns real solar-host subprocesses faking NVIDIA devices through
``GPU_TELEMETRY_OVERRIDE`` and asserts through control's API **and** the
hosts' spawn records — the enforced ``CUDA_VISIBLE_DEVICES`` reaching the
child process is the ground truth that pinning works.

The harness can only *run* the HF fixture model, and HF is single-GPU by
validation, so multi-GPU device-set selection is exercised through the
S-038 reservation coordinator (no backend involved), while instance
pinning/lifecycle/migration are exercised with gpu_count=1 intents.

Coverage matrix:
- multi-GPU device set: gpu_count=2 via the coordinator picks the two
  most-free devices in preference order and holds them on the host
- instance pinning: an intent's replica is pinned to the chosen device
  and the child spawns with CUDA_VISIBLE_DEVICES=<that device>
- double-booking: while intent A cold-starts, intent B must not place
  anywhere; deleting A releases the device and B takes it over
- explicit migration (API path): devices re-derived on the target host
  (host-d has a *different* layout — picks its own free device)
- drain/evacuation (reconciler path): target re-assignment under drain
- host-side enforcement: a reservation pins a device; a competing
  reservation is rejected naming it; an instance relying on it fails at
  *spawn* re-verify naming the device (TOCTOU guard)
- shortfall: a footprint no device can serve → no replica, intent stuck
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from fixtures.constants import BACKEND_CLASSIFICATION, MODEL_SOURCE_URI
from fixtures.helpers import wait_for
from fixtures.intents import create_intent, get_intent, wait_intent_ready

pytestmark = pytest.mark.infrastructure

_ALIAS_PREFIX = "gpupin"


def _override(devices: list[tuple[int, float]]) -> str:
    """Build a GPU_TELEMETRY_OVERRIDE from (index, available_gb) pairs."""
    entries = []
    for index, available in devices:
        entries.append(
            {
                "index": index,
                "name": "RTX 4090",
                "total_gb": 24.0,
                "used_gb": round(24.0 - available, 2),
            }
        )
    return json.dumps(entries)


def _alias(tag: str = "t") -> str:
    """A unique alias under the module's prefix (the pin filter matches it)."""
    return f"{_ALIAS_PREFIX}-{tag}-{uuid.uuid4().hex[:8]}"


async def _host_id(http_control: Any, name: str) -> str:
    resp = await http_control.get("/api/hosts")
    assert resp.status_code == 200, resp.text
    return next(h["id"] for h in resp.json() if h["name"] == name)


async def _snapshot(http_control: Any, host_id: str) -> dict:
    resp = await http_control.get("/api/resources")
    assert resp.status_code == 200, resp.text
    hosts = {h["host_id"]: h for h in resp.json()["hosts"]}
    assert host_id in hosts, f"host {host_id} missing from /api/resources"
    return hosts[host_id]


async def _wait_instance_gpu_ids(
    http_control: Any,
    host_id: str,
    instance_id: str,
    expected: list[int],
    *,
    timeout: float = 60.0,
) -> None:
    """Wait until the host's instance summary reports the pinned devices."""

    async def seen() -> bool:
        snap = await _snapshot(http_control, host_id)
        return any(
            i.get("id") == instance_id and i.get("gpu_ids") == expected
            for i in snap.get("instances", [])
        )

    await wait_for(
        seen,
        timeout=timeout,
        interval=0.5,
        description=f"instance {instance_id} gpu_ids={expected}",
    )


async def _wait_pinned_replica(
    http_control: Any, host_id: str, *, exclude_id: str, expected: list[int]
) -> str:
    """Wait until *host_id* runs an S-058 test instance pinned to
    ``expected`` (other than ``exclude_id``); returns its instance id."""

    holder: dict[str, str] = {}

    async def poll() -> bool:
        snap = await _snapshot(http_control, host_id)
        for inst in snap.get("instances", []):
            if inst.get("id") == exclude_id:
                continue
            if not str(inst.get("alias", "")).startswith(_ALIAS_PREFIX):
                continue
            if inst.get("gpu_ids") == expected:
                holder["id"] = str(inst["id"])
                return True
        return False

    try:
        await wait_for(
            poll,
            timeout=180.0,
            interval=0.5,
            description=f"replica on {host_id} pinned to {expected}",
        )
    except AssertionError:
        snap = await _snapshot(http_control, host_id)
        raise AssertionError(
            f"no replica on {host_id} pinned to {expected}; live snapshot: "
            f"{json.dumps(snap.get('instances', []), default=str)}"
        ) from None
    return holder["id"]


def _spawn_env(stack: Any, letter: str, instance_id: str) -> str | None:
    """The CUDA_VISIBLE_DEVICES value in host-*letter*'s spawn record, if any."""
    tail = stack.extra_hosts[letter].tail(800)
    for line in tail.splitlines():
        if (
            f"Spawning instance {instance_id}" in line
            and "CUDA_VISIBLE_DEVICES=" in line
        ):
            return line.split("CUDA_VISIBLE_DEVICES=", 1)[1].split(" ", 1)[0]
    return None


async def _spawn_env_is(
    stack: Any, letter: str, instance_id: str, expected: str, *, timeout: float = 60.0
) -> None:
    await wait_for(
        lambda: _spawn_env(stack, letter, instance_id) == expected,
        timeout=timeout,
        interval=0.5,
        description=f"spawn env CUDA_VISIBLE_DEVICES={expected} for {instance_id}",
    )


async def _instance_body(alias: str) -> dict:
    """A create-instance payload on the host (test_host_channel shape)."""
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


async def _intent_replica_count(http_control: Any, intent_id: str) -> int:
    intent = await get_intent(http_control, intent_id)
    if intent is None:
        return -1
    return len(
        [r for r in intent["status"].get("replica_set", []) if r.get("instance_id")]
    )


# ── 1. Multi-GPU device set through the S-038 coordinator ─────────────


async def test_multi_gpu_reservation_selects_device_set(
    http_control, stack, clean_state
):
    """gpu_count=2 → the two most-free devices, most-free first, held on the
    host. The coordinator path (no backend involved) is the only way the
    fake environment can drive gpu_count>1 end to end — the fixture model
    backend is HF, which is single-GPU by validation."""
    await stack.spawn_extra_host(
        "c",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 0.1), (1, 20.0), (2, 23.0)])
        },
    )
    try:
        host_id = await _host_id(http_control, "host-c")

        async def gpus_seen() -> bool:
            snap = await _snapshot(http_control, host_id)
            return [g["index"] for g in (snap.get("gpus") or [])] == [0, 1, 2]

        await wait_for(
            gpus_seen, timeout=30.0, interval=0.5, description="host-c gpus visible"
        )

        resp = await http_control.post(
            "/api/resources/reservations",
            json={
                "requester": "gpu-pinning-integration",
                "job_id": f"job-{uuid.uuid4().hex[:6]}",
                "workload_type": "training",
                "vram_gb": 4.0,
                "gpu_count": 2,
                "ram_gb": 1.0,
                "disk_gb": 1.0,
                "host_roles": ["inference"],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # Most-free first: device 2 (23 GB) before device 1 (20 GB).
        assert body["gpu_ids"] == [2, 1], body
        assert body["host_id"] == host_id, body

        # The host actually holds the chosen set (its own ledger + the WS
        # read model agree).
        async def held() -> bool:
            snap = await _snapshot(http_control, host_id)
            return any(r.get("gpu_ids") == [2, 1] for r in snap.get("reservations", []))

        await wait_for(
            held, timeout=30.0, interval=0.5, description="host holds [2, 1]"
        )
    finally:
        stack.remove_extra_host("c")


# ── 2. Instance pinning + claims lifecycle ────────────────────────────


async def test_instance_pinning_and_release_lifecycle(http_control, stack, clean_state):
    """An intent's replica is pinned to the chosen device and spawns with
    CUDA_VISIBLE_DEVICES=<device>; deleting the intent releases the device
    for the next intent (claims + reservation lifecycle end to end)."""
    await stack.spawn_extra_host(
        "c",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 0.1), (1, 0.1), (2, 23.5)])
        },
    )
    try:
        host_id = await _host_id(http_control, "host-c")
        footprint = {"vram_gb": 15.0, "gpu_count": 1}

        intent_a = await create_intent(
            http_control,
            alias=_alias("db-a"),
            placement={"host_allow": [host_id]},
            resources=dict(footprint),
        )
        # B is created immediately — its placement attempts overlap A's
        # cold start, which is exactly the window the claim ledger guards.
        intent_b = await create_intent(
            http_control,
            alias=_alias("db-b"),
            placement={"host_allow": [host_id]},
            resources=dict(footprint),
        )

        # Observe the double-booking window against the *host's own* ledger
        # (not control's cached read model, which lags the 10 s health push).
        # Whoever wins device 2 first holds it via their cold-start
        # reservation; the other must not place while that reservation
        # exists. A replica may only first appear when the winner's
        # reservation is already gone (the claim was released).
        import time as _time

        a_job = f"intent:{intent_a['id']}"
        b_job = f"intent:{intent_b['id']}"
        seen_replica = {"a": False, "b": False}
        observed_claim = False
        deadline = _time.monotonic() + 90.0
        async with httpx.AsyncClient(
            base_url=stack.extra_host_urls["c"],
            headers={"X-API-Key": stack.host_key("c")},
            timeout=15.0,
        ) as host_c_client:
            while _time.monotonic() < deadline:
                hresp = await host_c_client.get("/resources")
                assert hresp.status_code == 200, hresp.text
                hbody = hresp.json()
                reservations = [
                    r
                    for r in hbody.get("reservations", [])
                    if r.get("gpu_ids") == [2] and r.get("vram_gb") == 15.0
                ]
                a_holds = any(r.get("job_id") == a_job for r in reservations)
                b_holds = any(r.get("job_id") == b_job for r in reservations)
                if a_holds or b_holds:
                    observed_claim = True

                a_count = await _intent_replica_count(http_control, intent_a["id"])
                b_count = await _intent_replica_count(http_control, intent_b["id"])
                if b_count > 0 and not seen_replica["b"]:
                    seen_replica["b"] = True
                    if a_holds:
                        raise AssertionError(
                            "intent B was placed while A's reservation still "
                            "pinned device 2 — double-booking"
                        )
                if a_count > 0 and not seen_replica["a"]:
                    seen_replica["a"] = True
                    if b_holds:
                        raise AssertionError(
                            "intent A was placed while B's reservation still "
                            "pinned device 2 — double-booking"
                        )

                a_state = await get_intent(http_control, intent_a["id"])
                b_state = await get_intent(http_control, intent_b["id"])
                a_ready = (
                    a_state is not None and a_state["status"].get("phase") == "ready"
                )
                b_ready = (
                    b_state is not None and b_state["status"].get("phase") == "ready"
                )
                if not a_holds and not b_holds and (a_ready or b_ready):
                    break
                await asyncio.sleep(0.5)
        assert (
            observed_claim
        ), "never observed a cold-start reservation — fixture problem?"

        # A's pinning was in place the whole time.
        snap = await _snapshot(http_control, host_id)
        a_instances = [
            i for i in snap.get("instances", []) if i.get("gpu_ids") is not None
        ]
        assert any(i.get("gpu_ids") == [2] for i in a_instances), snap.get("instances")

        # Release A → the reservation and the claim are gone → B takes over.
        resp = await http_control.delete(f"/api/intents/{intent_a['id']}")
        assert resp.status_code in (200, 202, 204), resp.text
        ready_b = await wait_intent_ready(http_control, intent_b["id"], timeout=180.0)
        replica_b = next(
            r
            for r in (ready_b["status"].get("replica_set") or [])
            if r.get("host_id") == host_id
        )
        await _wait_instance_gpu_ids(
            http_control, host_id, replica_b["instance_id"], [2]
        )
    finally:
        stack.remove_extra_host("c")


# ── 3. Explicit migration re-pins on the target ───────────────────────


async def test_explicit_migration_re_pins_on_target(http_control, stack, clean_state):
    """POST /api/instances/migrate re-derives devices on host-d, whose layout
    is deliberately different (only device 0 free) — the target never
    inherits the source's physical ids."""
    await stack.spawn_extra_host(
        "c",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 0.1), (1, 0.1), (2, 23.5)])
        },
    )
    await stack.spawn_extra_host(
        "d",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 23.0), (1, 0.1), (2, 0.1)])
        },
    )
    try:
        host_c = await _host_id(http_control, "host-c")
        host_d = await _host_id(http_control, "host-d")

        intent = await create_intent(
            http_control,
            alias=_alias("mg"),
            placement={"host_allow": [host_c, host_d]},
            resources={"vram_gb": 4.0, "gpu_count": 1},
        )
        ready = await wait_intent_ready(http_control, intent["id"], timeout=180.0)
        replica = next(
            r
            for r in (ready["status"].get("replica_set") or [])
            if r.get("host_id") == host_c
        )
        source_id = replica["instance_id"]
        assert source_id
        await _wait_instance_gpu_ids(http_control, host_c, source_id, [2])

        # The migration only re-derives if host-d's telemetry is visible.
        async def d_gpus_seen() -> bool:
            snap = await _snapshot(http_control, host_d)
            return [g["index"] for g in snap.get("gpus")] == [0, 1, 2]

        await wait_for(
            d_gpus_seen, timeout=30.0, interval=0.5, description="host-d gpus visible"
        )

        resp = await http_control.post(
            "/api/instances/migrate",
            json={
                "instance_id": source_id,
                "source_host_id": host_c,
                "target_host_id": host_d,
                "allow_production": True,
            },
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["status"] == "completed", result

        # Target: re-derived on host-d → device 0; the reconciler starts the
        # managed target (G3), which pins the child via the same env.
        target_id = await _wait_pinned_replica(
            http_control, host_d, exclude_id=source_id, expected=[0]
        )
        await _spawn_env_is(stack, "d", target_id, "0")

        # The migration moved the replica: the intent's replica_set now points at
        # host-d (the source was disowned, not deleted — S-037 leaves it as
        # a marker-less stopped instance).
        async def moved() -> bool:
            state = await get_intent(http_control, intent["id"])
            if state is None:
                return False
            hosts = [
                r.get("host_id")
                for r in state["status"].get("replica_set", [])
                if r.get("instance_id")
            ]
            return hosts == [host_d]

        await wait_for(
            moved, timeout=60.0, interval=0.5, description="replica moved to host-d"
        )
    finally:
        stack.remove_extra_host("c")
        stack.remove_extra_host("d")


# ── 4. Drain / evacuation re-pins on the target (reconciler path) ─────


async def test_drain_evacuation_re_pins_on_target(http_control, stack, clean_state):
    """Draining host-c moves the pinned instance to host-d and the reconciler
    re-assigns devices on the target (EVACUATE path)."""
    await stack.spawn_extra_host(
        "c",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 0.1), (1, 0.1), (2, 23.5)])
        },
    )
    await stack.spawn_extra_host(
        "d",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 23.0), (1, 0.1), (2, 0.1)])
        },
    )
    try:
        host_c = await _host_id(http_control, "host-c")
        host_d = await _host_id(http_control, "host-d")

        intent = await create_intent(
            http_control,
            alias=_alias("dr"),
            placement={"host_allow": [host_c, host_d]},
            resources={"vram_gb": 4.0, "gpu_count": 1},
        )
        ready = await wait_intent_ready(http_control, intent["id"], timeout=180.0)
        replica = next(
            r
            for r in (ready["status"].get("replica_set") or [])
            if r.get("host_id") == host_c
        )
        source_id = replica["instance_id"]
        assert source_id
        await _wait_instance_gpu_ids(http_control, host_c, source_id, [2])

        resp = await http_control.post(f"/api/hosts/{host_c}/drain")
        assert resp.status_code in (200, 202), resp.text

        # The evacuation moves the replica to host-d and pins it there.
        target_id = await _wait_pinned_replica(
            http_control, host_d, exclude_id=source_id, expected=[0]
        )
        await _spawn_env_is(stack, "d", target_id, "0")

        # Drain converges: nothing managed left on host-c.
        async def drained() -> bool:
            resp = await http_control.get(f"/api/hosts/{host_c}/drain")
            if resp.status_code != 200:
                return False
            return resp.json().get("drain_state") == "drained"

        try:
            await wait_for(
                drained, timeout=180.0, interval=0.5, description="host-c drained"
            )
        except AssertionError:
            resp = await http_control.get(f"/api/hosts/{host_c}/drain")
            snap = await _snapshot(http_control, host_c)
            raise AssertionError(
                f"host-c did not drain; endpoint={resp.text}; "
                f"instances={json.dumps(snap.get('instances', []), default=str)}"
            ) from None
    finally:
        stack.remove_extra_host("c")
        stack.remove_extra_host("d")


# ── 5. Host-side enforcement: reservation pinning + spawn re-verify ─────


async def test_host_enforces_device_pinning_and_fails_spawn_naming_device(
    http_control, stack, clean_state
):
    """Directly on the host (legacy/SuperNova path, no coordinator): device 2
    reports only 10 GB free, so a reservation pins it, a competing
    reservation is rejected naming it, and an instance relying on it fails
    at spawn-time re-verification (TOCTOU guard) with the device named."""
    await stack.spawn_extra_host(
        "c",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 0.1), (1, 0.1), (2, 10.0)])
        },
    )
    try:
        host_id = await _host_id(http_control, "host-c")
        # The host refuses repo:// sources at create until the model is
        # pulled — distribute the fixture model first, like the host-channel
        # test does, so the create itself is valid.
        resp = await http_control.post(
            "/api/models/distribute",
            json={"target_host_id": host_id, "source_uri": MODEL_SOURCE_URI},
        )
        assert resp.status_code == 200, resp.text
        async with httpx.AsyncClient(
            base_url=stack.extra_host_urls["c"],
            headers={"X-API-Key": stack.host_key("c")},
            timeout=15.0,
        ) as http:
            # The host accepts local:// sources only; read the pulled slug.
            resp = await http.get("/models")
            assert resp.status_code == 200, resp.text
            entry = next(
                m for m in resp.json() if m.get("source_uri") == MODEL_SOURCE_URI
            )
            slug = entry["path"].rstrip("/").split("/")[-1]

            # Pin 8 of device 2's 10 GB.
            resp = await http.post(
                "/resources/reservations",
                json={
                    "job_id": f"job-{uuid.uuid4().hex[:6]}",
                    "workload_type": "training",
                    "vram_gb": 8.0,
                    "ram_gb": 1.0,
                    "gpu_ids": [2],
                },
            )
            assert resp.status_code in (200, 201), resp.text

            # A competing 9.5 GB reservation on the same device → 409 naming it.
            resp = await http.post(
                "/resources/reservations",
                json={
                    "job_id": f"job-{uuid.uuid4().hex[:6]}",
                    "workload_type": "training",
                    "vram_gb": 9.5,
                    "ram_gb": 1.0,
                    "gpu_ids": [2],
                },
            )
            assert resp.status_code == 409, resp.text
            body = resp.json()
            assert body["dimension"] == "vram"
            assert body["device_index"] == 2

            # An instance relying on the over-committed device passes create
            # (no capacity gate there) and fails at SPAWN: the raw device
            # has 10 GB free < 20 GB requested, so verify_gpu_capacity
            # refuses before any process is spawned.
            payload = await _instance_body(_alias("en"))
            payload["config"]["model_source"] = f"local://{slug}"
            payload["gpu_ids"] = [2]
            payload["vram_gb"] = 20.0
            resp = await http.post("/instances", json=payload)
            assert resp.status_code == 200, resp.text
            instance_id = resp.json()["instance"]["id"]

            # Start it: the spawn-time re-verify must refuse device 2
            # (10 GB free < 20 GB requested) before any process is spawned.
            resp = await http.post(f"/instances/{instance_id}/start")
            assert resp.status_code in (200, 500, 502), resp.text

            await wait_for(
                lambda: _failed_instance(http, instance_id),
                timeout=45.0,
                interval=0.5,
                description="instance failed at spawn re-verify",
            )
            error = await _host_instance_error(http, instance_id)
            assert error is not None and "GPU 2" in error
            assert "10.0 GB" in error and "20.0 GB" in error
    finally:
        stack.remove_extra_host("c")


async def _failed_instance(host: httpx.AsyncClient, instance_id: str) -> bool:
    error = await _host_instance_error(host, instance_id)
    if error is None:
        # Diagnose: surface the live instance state on timeout.
        resp = await host.get(f"/instances/{instance_id}")
        if resp.status_code == 200 and resp.json().get("status") != "failed":
            raise AssertionError(
                f"instance {instance_id} did not fail; live state: {resp.text}"
            )
    return error is not None


async def _host_instance_error(host: httpx.AsyncClient, instance_id: str) -> str | None:
    resp = await host.get(f"/instances/{instance_id}")
    if resp.status_code != 200:
        return None
    inst = resp.json()
    if inst.get("status") == "failed":
        return inst.get("error_message") or ""
    return None


# ── 6. Shortfall: a footprint no device can serve ─────────────────────


async def test_shortfall_leaves_no_replica(http_control, stack, clean_state):
    """host-c's best device has 10 GB; the intent needs 20 → no replica may
    be fabricated and the intent stays unready (truthful status)."""
    await stack.spawn_extra_host(
        "c",
        env_extra={
            "GPU_TELEMETRY_OVERRIDE": _override([(0, 0.1), (1, 0.1), (2, 10.0)])
        },
    )
    try:
        host_id = await _host_id(http_control, "host-c")

        intent = await create_intent(
            http_control,
            alias=_alias("sh"),
            placement={"host_allow": [host_id]},
            resources={"vram_gb": 20.0, "gpu_count": 1},
        )

        # Bound the observation: 30 ticks of the reconciler; a replica must
        # never appear. A manual loop because the "stays absent" semantics
        # is not expressible as a wait_for condition.
        import time as _time

        deadline = _time.monotonic() + 15.0
        while _time.monotonic() < deadline:
            count = await _intent_replica_count(http_control, intent["id"])
            assert (
                count == 0
            ), f"replica appeared despite impossible footprint ({count})"
            await asyncio.sleep(0.5)
        state = await get_intent(http_control, intent["id"])
        assert state is not None
        assert state["status"]["phase"] != "ready"
        assert state["status"].get("ready_replicas", 0) == 0
    finally:
        stack.remove_extra_host("c")
