"""infrastructure: GET /api/routing/state composes the authoritative snapshot
(marker: infrastructure).

The unit tests mock the builder at the route; this proves the live chain:
REST route (management auth) -> snapshot builder -> real Redis stores plus the
hosts table -> versioned payload. Active requests and instance states are
seeded in-process into the session Redis the control subprocess reads; the
host rows come from the live stack's Postgres.
"""

from __future__ import annotations

import pytest
from fixtures.helpers import wait_for

pytestmark = pytest.mark.infrastructure


async def test_routing_state_rest_mirrors_seeded_stores(
    stack, http_control, clean_state
):
    """Seeded registry/instance-state entries are projected by the live
    control's REST mirror, with server-derived statuses and aggregates."""
    from app.models.routing_snapshot import SCHEMA_VERSION
    from app.redis_state import close_redis, init_redis
    from app.redis_state.instance_states import InstanceStatesStore
    from app.redis_state.routing import RoutingStore

    # See test_instance_state_ttl: init the shared client for this process
    # only, and rely on clean_state's prefix wipe instead of a flushall (which
    # would break the session's WS connection state).
    await init_redis(stack.db_env["redis"])
    requests = RoutingStore()
    states = InstanceStatesStore()

    try:
        await requests.create_request(
            "req-queued",
            model="m-a",
            endpoint="/v1",
            client_ip="1.1.1.1",
            timestamp="ts",
        )
        await requests.create_request(
            "req-processing",
            model="m-b",
            endpoint="/v2",
            client_ip="2.2.2.2",
            timestamp="ts",
        )
        await requests.update_request(
            "req-processing",
            host_id="host-a",
            host_name="host A",
            instance_id="inst-1",
            resolved_model="m-b",
            attempt=1,
        )
        await states.set(
            "host-a",
            "inst-1",
            {
                "host_id": "host-a",
                "host_name": "host A",
                "instance_id": "inst-1",
                "timestamp": "2026-09-02T10:00:00+00:00",
                "data": {"load": 0.5},
            },
        )

        async def snapshot_has_both_requests() -> bool:
            resp = await http_control.get("/api/routing/state")
            if resp.status_code != 200:
                return False
            return {r["request_id"] for r in resp.json()["active_requests"]} >= {
                "req-queued",
                "req-processing",
            }

        await wait_for(
            snapshot_has_both_requests,
            timeout=5.0,
            interval=0.25,
            description="REST snapshot projecting the seeded requests",
        )
        resp = await http_control.get("/api/routing/state")
        body = resp.json()

        assert body["schema_version"] == SCHEMA_VERSION
        assert body["generated_at"]

        by_id = {r["request_id"]: r for r in body["active_requests"]}
        assert by_id["req-queued"]["status"] == "queued"
        assert by_id["req-queued"]["host_id"] is None
        assert by_id["req-processing"]["status"] == "processing"
        assert by_id["req-processing"]["instance_id"] == "inst-1"
        assert by_id["req-processing"]["resolved_model"] == "m-b"

        agg = body["aggregates"]
        assert agg["queued"] == 1
        assert agg["processing"] == 1
        assert agg["errored"] == 0
        assert agg["by_instance"] == {"host-a:inst-1": 1}
        assert agg["by_host"] == {"host-a": 1}
        assert agg["by_model"] == {"m-a": 1, "m-b": 1}
        assert agg["by_endpoint"] == {"/v1": 1, "/v2": 1}

        istates = {(e["host_id"], e["instance_id"]): e for e in body["instance_states"]}
        assert istates[("host-a", "inst-1")]["data"] == {"load": 0.5}

        hosts = {h["name"]: h for h in body["hosts"]}
        assert hosts["host-a"]["connected"] is True
        assert hosts["host-b"]["connected"] is True

        # Terminal delete through the shared store removes the request from
        # the next snapshot (cross-process: written here, projected by control).
        await requests.delete_request("req-processing")
        body = (await http_control.get("/api/routing/state")).json()
        assert {r["request_id"] for r in body["active_requests"]} == {"req-queued"}
        assert body["aggregates"]["processing"] == 0
        assert body["aggregates"]["queued"] == 1
    finally:
        for rid in ("req-queued", "req-processing"):
            await requests.delete_request(rid)
        await states.delete("host-a", "inst-1")
        await close_redis()
