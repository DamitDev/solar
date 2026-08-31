"""infrastructure: S-058 — GPU-aware placement end to end (marker: infrastructure).

host-c is spawned with ``GPU_TELEMETRY_OVERRIDE`` faking a 3-device NVIDIA
host where only device 2 has real capacity. An intent with ``gpu_count: 1``
and a per-GPU footprint pins to host-c via ``host_allow``, lands on device
2, the instance reports ``gpu_ids: [2]`` through the WS instance cache, and
the host's spawn record proves ``CUDA_VISIBLE_DEVICES=2`` reached the child
process environment.

- SUCCESS: /api/resources lists the fake per-device telemetry.
- SUCCESS: the instance carries gpu_ids [2] in the resource snapshot.
- SUCCESS: the spawn log line names the enforced device set (the harness
  has no POST-body capture; the log line is legitimate observability).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fixtures.helpers import wait_for
from fixtures.intents import create_intent, wait_intent_ready

from infrastructure.test_gpu_pinning import _spawn_env_is

pytestmark = pytest.mark.infrastructure

_GPU_OVERRIDE = json.dumps(
    [
        {"index": 0, "name": "RTX 4090", "total_gb": 24.0, "used_gb": 23.9},
        {"index": 1, "name": "RTX 4090", "total_gb": 24.0, "used_gb": 23.9},
        {"index": 2, "name": "RTX 4090", "total_gb": 24.0, "used_gb": 0.5},
    ]
)


def _alias(prefix: str = "gpu") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _host_c_snapshot(http_control: Any, host_id: str) -> dict:
    resp = await http_control.get("/api/resources")
    assert resp.status_code == 200, resp.text
    hosts = {h["host_id"]: h for h in resp.json()["hosts"]}
    assert host_id in hosts, f"host-c ({host_id}) missing from /api/resources"
    return hosts[host_id]


async def test_gpu_aware_placement_end_to_end(http_control, stack, clean_state):
    # ── 1. Spawn host-c: a fake 3-GPU NVIDIA host, only device 2 free ──
    await stack.spawn_extra_host(
        "c",
        env_extra={"GPU_TELEMETRY_OVERRIDE": _GPU_OVERRIDE},
    )
    try:
        hosts = {h["name"]: h for h in (await http_control.get("/api/hosts")).json()}
        host_id = hosts["host-c"]["id"]

        # The fake devices must appear in control's read model before
        # placement can bin-pack them.
        async def gpus_reported() -> bool:
            snap = await _host_c_snapshot(http_control, host_id)
            return len(snap.get("gpus") or []) == 3

        await wait_for(
            gpus_reported,
            timeout=30.0,
            interval=0.5,
            description="host-c gpus visible via the WS health push",
        )

        # ── 2. Intent pinned to host-c, needing 1 GPU with 4 GB free ──
        intent = await create_intent(
            http_control,
            alias=_alias(),
            placement={"host_allow": [host_id]},
            resources={"vram_gb": 4.0, "gpu_count": 1},
        )
        ready = await wait_intent_ready(http_control, intent["id"], timeout=180.0)

        replica = next(
            r for r in ready["status"]["replica_set"] if r.get("host_id") == host_id
        )
        instance_id = replica["instance_id"]
        assert instance_id, f"no replica on host-c: {ready['status']['replica_set']}"

        # ── 3. The instance reports the chosen device (WS instance cache) ──
        async def instance_shows_device() -> bool:
            snap = await _host_c_snapshot(http_control, host_id)
            return any(
                i.get("id") == instance_id and i.get("gpu_ids") == [2]
                for i in snap.get("instances", [])
            )

        await wait_for(
            instance_shows_device,
            timeout=30.0,
            interval=0.5,
            description="instance reports gpu_ids [2]",
        )

        # ── 4. The spawned child got the enforced environment ──
        # Scoped to THIS instance's spawn record (not "any spawn on device
        # 2 in the last 400 log lines") via the pinning suite's helper.
        await _spawn_env_is(stack, "c", instance_id, "2")
    finally:
        stack.remove_extra_host("c")
