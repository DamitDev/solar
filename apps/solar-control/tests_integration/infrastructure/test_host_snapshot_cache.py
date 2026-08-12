"""infrastructure: C5 — host resource snapshots are WS-first (marker: infrastructure).

The host pushes the full resource snapshot with host_health; control stores
it in Redis and _fetch_host_resource_snapshot serves it cache-first. The
API exposes snapshot_source so the read model is observable.

- SUCCESS: connected hosts report snapshot_source == "ws", and a
  reservation made directly on a host appears (with per-reservation detail)
  in control's /api/resources after the next health push.
- FAILURE: a registered-but-dead host degrades to snapshot_source "none"
  with an error string (HTTP fallback fails fast, same response shape).
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.helpers import wait_for

pytestmark = pytest.mark.infrastructure


def _alias(prefix: str = "snap") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _resources_by_host(http_control) -> dict[str, dict]:
    resp = await http_control.get("/api/resources")
    assert resp.status_code == 200, resp.text
    return {h["host_id"]: h for h in resp.json()["hosts"]}


async def test_connected_hosts_serve_ws_snapshots(http_control, stack, clean_state):
    """Both connected hosts: snapshot_source ws, live dimensions present."""

    async def ws_source() -> bool:
        hosts = await _resources_by_host(http_control)
        return len(hosts) >= 2 and all(
            h.get("snapshot_source") == "ws" for h in hosts.values()
        )

    await wait_for(
        ws_source,
        timeout=30.0,
        interval=0.5,
        description="both hosts serving ws snapshots",
    )
    hosts = await _resources_by_host(http_control)
    for h in hosts.values():
        assert h["snapshot_source"] == "ws"
        assert h["reachable"] is True
        assert h.get("ram_total_gb") is not None or h.get("vram_total_gb") is not None


async def test_reservation_detail_flows_through_ws_snapshot(
    http_control, http_host, stack, clean_state
):
    """A reservation made directly on the host appears in control's
    /api/resources with per-reservation detail via the WS read model."""
    reservation_id = f"res-{uuid.uuid4().hex[:8]}"
    resp = await http_host.post(
        "/resources/reservations",
        json={
            "id": reservation_id,
            "job_id": f"job-{uuid.uuid4().hex[:6]}",
            "workload_type": "training",
            "vram_gb": 0.0,
            "ram_gb": 0.5,
            "disk_gb": 0.1,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    # The host generates its own reservation id (the request body id is not
    # part of ReservationRequest) — track the id the host actually issued.
    reservation_id = resp.json()["id"]

    async def reservation_seen() -> bool:
        hosts = await _resources_by_host(http_control)
        for h in hosts.values():
            for r in h.get("reservations", []):
                if r.get("id") == reservation_id:
                    return True
        return False

    try:
        await wait_for(
            reservation_seen,
            timeout=30.0,
            interval=0.5,
            description="reservation visible through control /api/resources",
        )
    except AssertionError:
        import json as _json

        hosts = await _resources_by_host(http_control)
        print(
            "RESERVATION-TIMEOUT:\n"
            + _json.dumps(
                {
                    hid: {
                        "snapshot_source": h.get("snapshot_source"),
                        "reservation_count": h.get("reservation_count"),
                        "reservations": [
                            r.get("id") for r in h.get("reservations", [])
                        ][:5],
                        "reachable": h.get("reachable"),
                    }
                    for hid, h in hosts.items()
                },
                indent=1,
            )
        )
        raise
    hosts = await _resources_by_host(http_control)
    found = [
        r
        for h in hosts.values()
        for r in h.get("reservations", [])
        if r.get("id") == reservation_id
    ]
    assert len(found) == 1
    assert found[0]["job_id"]
    assert found[0]["status"] in ("pending", "running")
    # The host that carried it served the snapshot over the WS channel.
    assert any(h["snapshot_source"] == "ws" for h in hosts.values())


async def test_dead_host_degrades_to_none(http_control, stack, clean_state):
    """FAILURE state: a registered host that never connects serves
    snapshot_source 'none' with an error — same body shape, no crash."""
    from fixtures.seed import register_host_via_api

    host_id = await register_host_via_api(
        http_control,
        f"ghost-{uuid.uuid4().hex[:6]}",
        "http://127.0.0.1:1",  # nothing listens here
        f"ghost-key-{uuid.uuid4().hex[:6]}",
        roles=["inference"],
    )

    # The hosts table is session-persistent: `clean_state` resets intents,
    # instances and volatile Redis but deliberately never truncates hosts.
    # A surviving ghost row is not a cosmetic leak — every later test sees a
    # permanently unreachable host, which degrades _collect_availability to
    # partial and makes the gateway's registry refresh fail on every cycle.
    # The teardown has to be a `finally`, not a trailing line: if an
    # assertion below fails the row must still go, or one failure here
    # poisons the rest of the session.
    try:

        async def ghost_degraded() -> bool:
            hosts = await _resources_by_host(http_control)
            ghost = hosts.get(host_id)
            return ghost is not None and ghost["snapshot_source"] == "none"

        await wait_for(
            ghost_degraded,
            timeout=30.0,
            interval=0.5,
            description="dead host degraded to none",
        )
        hosts = await _resources_by_host(http_control)
        ghost = hosts[host_id]
        assert ghost["snapshot_source"] == "none"
        assert ghost["reachable"] is False
        assert ghost.get("error")
        # The response still carries the full shape for every host.
        for field in (
            "host_id",
            "host_name",
            "status",
            "roles",
            "instance_count",
            "reservations",
        ):
            assert field in ghost
    finally:
        resp = await http_control.delete(f"/api/hosts/{host_id}")
        assert resp.status_code in (200, 404), resp.text
