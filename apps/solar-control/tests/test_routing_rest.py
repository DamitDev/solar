"""Tests for the /api/routing management route.

The REST mirror returns the exact payload produced by the snapshot builder (so
it is identical in shape to the ``routing_snapshot`` WS event) and enforces the
same management auth as the other management routes.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models.routing_snapshot import SCHEMA_VERSION, RoutingSnapshot

API_KEY = settings.management_api_key


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


@pytest.fixture
def client():
    from app.main import app

    with patch(
        "app.routes.management.routing.build_routing_snapshot",
        AsyncMock(return_value=_snapshot()),
    ):
        yield TestClient(app)


def test_routing_state_requires_management_key(client: TestClient):
    resp = client.get("/api/routing/state")
    assert resp.status_code == 401


def test_routing_state_returns_snapshot_payload(client: TestClient):
    resp = client.get("/api/routing/state", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["generated_at"] == "2026-09-03T00:00:00+00:00"
    assert data["active_requests"][0]["request_id"] == "req-1"
    assert data["hosts"] == []
