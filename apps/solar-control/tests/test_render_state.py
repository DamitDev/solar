"""Tests for app.services.render_state — the snapshot builder (US-003).

Patches the store/db namespaces the builder imports directly and verifies the
composed payload shape, the server-computed tallies, and the field contract the
WebUI consumers (US-006/007/009) code against.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Host, HostStatus
from app.models.routing_snapshot import SCHEMA_VERSION
from app.services.render_state import build_routing_snapshot


def _host(*, host_id: str = "host-1", name: str = "host-one", **kw) -> Host:
    defaults = {
        "url": f"http://{host_id}:8000",
        "api_key": "key",
        "status": HostStatus.ONLINE,
        "last_seen": datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
        "gpu_type": "H100",
    }
    defaults.update(kw)
    return Host(id=host_id, name=name, **defaults)


def _istate_entry(
    *, host_id: str = "host-1", instance_id: str = "inst-1", data: dict | None = None
) -> dict:
    return {
        "host_id": host_id,
        "host_name": "host-one",
        "instance_id": instance_id,
        "timestamp": "2026-09-02T10:00:00+00:00",
        "data": data or {"load": 0.6},
    }


def _active_request(
    *,
    request_id: str = "req-1",
    host_id: str | None = "host-1",
    instance_id: str | None = "inst-1",
    model: str | None = "qwen-7b",
    resolved_model: str | None = None,
    endpoint: str | None = "ep-1",
) -> dict:
    return {
        "request_id": request_id,
        "model": model,
        "resolved_model": resolved_model or model,
        "endpoint": endpoint,
        "host_id": host_id,
        "host_name": "host-one" if host_id else None,
        "instance_id": instance_id,
        "attempt": 1,
        "timestamp": "2026-09-02T10:00:01+00:00",
    }


_cm = pytest.mark.anyio


@_cm
async def test_empty_snapshot_shapes():
    with _patched():
        snap = await build_routing_snapshot()

    assert snap.schema_version == SCHEMA_VERSION
    assert snap.generated_at
    assert snap.hosts == []
    assert snap.instance_states == []
    assert snap.active_requests == []
    assert snap.endpoints == []
    assert snap.pending_hosts == []
    assert snap.aggregates.by_instance == {}
    assert snap.aggregates.queued == 0
    assert snap.aggregates.processing == 0
    assert snap.aggregates.errored == 0


@_cm
async def test_hosts_compiled_with_status_drain_and_health():
    h = _host(host_id="host-1", name="box-a")
    overrides = {
        "app.services.render_state.host_db.get_all_hosts": AsyncMock(return_value=[h]),
        "app.services.render_state.get_connected_host_ids": AsyncMock(
            return_value=["host-1"]
        ),
        "app.services.render_state.get_host_instances": AsyncMock(
            return_value=[{"id": "inst-1", "alias": "a"}]
        ),
    }
    with _patched(**overrides):
        snap = await build_routing_snapshot()

    assert len(snap.hosts) == 1
    host = snap.hosts[0]
    assert host.host_id == "host-1"
    assert host.name == "box-a"
    assert host.status == "online"
    assert host.connected is True
    assert host.gpu_type == "H100"
    assert host.instances == [{"id": "inst-1", "alias": "a"}]


@_cm
async def test_instance_states_carry_data_and_timestamp():
    overrides = {
        "app.services.render_state.instance_states_store.get_all": AsyncMock(
            return_value=[_istate_entry(host_id="host-1", instance_id="inst-1")]
        )
    }
    with _patched(**overrides):
        snap = await build_routing_snapshot()

    assert len(snap.instance_states) == 1
    state = snap.instance_states[0]
    assert state.host_id == "host-1"
    assert state.instance_id == "inst-1"
    assert state.data["load"] == 0.6


@_cm
async def test_aggregates_count_inflight_by_instance_host_model_endpoint():
    requests = [
        _active_request(
            request_id="r1",
            host_id="host-1",
            instance_id="inst-1",
            model="qwen-7b",
            endpoint="ep-1",
        ),
        _active_request(
            request_id="r2",
            host_id="host-1",
            instance_id="inst-1",
            model="qwen-7b",
            endpoint="ep-1",
        ),
        _active_request(
            request_id="r3",
            host_id="host-1",
            instance_id="inst-2",
            model="llama-8b",
            endpoint="ep-2",
        ),
    ]
    overrides = {
        "app.services.render_state.routing_store.list_requests": AsyncMock(
            return_value=requests
        )
    }
    with _patched(**overrides):
        snap = await build_routing_snapshot()

    agg = snap.aggregates
    assert agg.by_instance["host-1:inst-1"] == 2
    assert agg.by_instance["host-1:inst-2"] == 1
    assert agg.by_host["host-1"] == 3
    assert agg.by_model["qwen-7b"] == 2
    assert agg.by_model["llama-8b"] == 1
    assert agg.by_endpoint["ep-1"] == 2
    assert agg.by_endpoint["ep-2"] == 1
    assert agg.processing == 3
    assert agg.queued == 0
    assert agg.errored == 0

    assert len(snap.active_requests) == 3
    assert {r.request_id for r in snap.active_requests} == {"r1", "r2", "r3"}


@_cm
async def test_unrouted_request_counts_as_queued():
    requests = [
        _active_request(
            request_id="r1",
            host_id=None,
            instance_id=None,
            model="qwen-7b",
            endpoint="ep-1",
        )
    ]
    overrides = {
        "app.services.render_state.routing_store.list_requests": AsyncMock(
            return_value=requests
        )
    }
    with _patched(**overrides):
        snap = await build_routing_snapshot()

    request = snap.active_requests[0]
    assert request.status == "queued"
    assert request.host_id is None
    assert snap.aggregates.queued == 1
    assert snap.aggregates.processing == 0
    assert snap.aggregates.by_instance == {}
    assert snap.aggregates.by_host == {}


@_cm
async def test_aggregates_use_resolved_model_fallback_and_no_endpoint():
    requests = [
        _active_request(
            request_id="r1", model="alias-x", resolved_model="resolved-x", endpoint=None
        )
    ]
    overrides = {
        "app.services.render_state.routing_store.list_requests": AsyncMock(
            return_value=requests
        )
    }
    with _patched(**overrides):
        snap = await build_routing_snapshot()

    assert snap.aggregates.by_model["resolved-x"] == 1
    assert snap.aggregates.by_endpoint == {}


@_cm
async def test_endpoints_and_pending_hosts_passed_through():
    endpoint = _ApiEndpoint(
        {"id": "ep-1", "name": "Endpoint One", "serve_all_models": True}
    )
    pending = {"pending_id": "p-1", "host_name": "pending-box"}
    overrides = {
        "app.services.render_state.endpoint_db.get_all_endpoints": AsyncMock(
            return_value=[endpoint]
        ),
        "app.services.render_state.get_pending_hosts": AsyncMock(
            return_value=[pending]
        ),
    }
    with _patched(**overrides):
        snap = await build_routing_snapshot()

    assert snap.endpoints == [
        {"id": "ep-1", "name": "Endpoint One", "serve_all_models": True}
    ]
    assert snap.pending_hosts == [pending]


class _patched:
    """Patch every external dependency, allowing per-call overrides."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __enter__(self):
        defaults = {
            "app.services.render_state.host_db.get_all_hosts": AsyncMock(
                return_value=[]
            ),
            "app.services.render_state.endpoint_db.get_all_endpoints": AsyncMock(
                return_value=[]
            ),
            "app.services.render_state.get_connected_host_ids": AsyncMock(
                return_value=[]
            ),
            "app.services.render_state.get_host_instances": AsyncMock(return_value=[]),
            "app.services.render_state.get_pending_hosts": AsyncMock(return_value=[]),
            "app.services.render_state.instance_states_store.get_all": AsyncMock(
                return_value=[]
            ),
            "app.services.render_state.routing_store.list_requests": AsyncMock(
                return_value=[]
            ),
        }
        defaults.update(self._overrides)
        self._stack = ExitStack()
        for target, mock in defaults.items():
            self._stack.enter_context(patch(target, new=mock))
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


class _ApiEndpoint:
    def __init__(self, data: dict):
        self._data = data

    def model_dump(self) -> dict:
        return self._data
