"""Tests for the WebUI connect snapshot emit (US-004).

Verifies that a freshly connected WebUI client receives the authoritative
``routing_snapshot`` (produced by the routing snapshot builder) before any
request/instance deltas, so a late-joining or reconnecting client always
reconciles. Follows the existing ``webui_handlers`` test pattern by patching
the emitted-event surface.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.routing_snapshot import RoutingSnapshot, SCHEMA_VERSION

_cm = pytest.mark.anyio


def _snapshot() -> RoutingSnapshot:
    return RoutingSnapshot(
        generated_at="2026-09-03T00:00:00+00:00",
        active_requests=[
            {
                "request_id": "req-1",
                "host_id": "host-1",
                "instance_id": "inst-1",
                "status": "processing",
            }
        ],
    )


def _emitted_event_names(call_args_list) -> list[str]:
    return [c.args[0] for c in call_args_list]


@_cm
async def test_routing_snapshot_emitted_on_connect():
    from app.socketio_app import webui_handlers

    snapshot = _snapshot()
    with (
        patch.object(
            webui_handlers, "settings", SimpleNamespace(management_api_key="mgmt-key")
        ),
        patch.object(
            webui_handlers.host_db, "get_all_hosts", AsyncMock(return_value=[])
        ),
        patch.object(
            webui_handlers, "is_host_connected", AsyncMock(return_value=False)
        ),
        patch.object(
            webui_handlers, "get_connected_host_ids", AsyncMock(return_value=[])
        ),
        patch.object(webui_handlers, "get_pending_hosts", AsyncMock(return_value=[])),
        patch.object(
            webui_handlers, "build_routing_snapshot", AsyncMock(return_value=snapshot)
        ),
        patch.object(webui_handlers.sio, "emit", AsyncMock()) as mock_emit,
    ):
        await webui_handlers.webui_connect(
            "sid-1", {"headers": []}, {"api_key": "mgmt-key"}
        )

    events = _emitted_event_names(mock_emit.call_args_list)
    assert "routing_snapshot" in events
    snapshot_call = next(
        c for c in mock_emit.call_args_list if c.args[0] == "routing_snapshot"
    )
    payload = snapshot_call.args[1]
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["generated_at"] == snapshot.generated_at
    assert payload["active_requests"][0]["request_id"] == "req-1"
    assert snapshot_call.kwargs["to"] == "sid-1"


@_cm
async def test_routing_snapshot_precedes_deltas():
    from app.socketio_app import webui_handlers

    snapshot = _snapshot()
    with (
        patch.object(
            webui_handlers, "settings", SimpleNamespace(management_api_key="mgmt-key")
        ),
        patch.object(
            webui_handlers.host_db, "get_all_hosts", AsyncMock(return_value=[])
        ),
        patch.object(
            webui_handlers, "is_host_connected", AsyncMock(return_value=False)
        ),
        patch.object(
            webui_handlers, "get_connected_host_ids", AsyncMock(return_value=["host-1"])
        ),
        patch.object(
            webui_handlers,
            "get_host_instances",
            AsyncMock(return_value=[{"id": "inst-1"}]),
        ),
        patch.object(webui_handlers, "get_pending_hosts", AsyncMock(return_value=[])),
        patch.object(
            webui_handlers, "build_routing_snapshot", AsyncMock(return_value=snapshot)
        ),
        patch.object(webui_handlers.sio, "emit", AsyncMock()) as mock_emit,
    ):
        await webui_handlers.webui_connect(
            "sid-1", {"headers": []}, {"api_key": "mgmt-key"}
        )

    events = _emitted_event_names(mock_emit.call_args_list)
    assert events.index("routing_snapshot") < events.index("instances_update")
