"""Tests for the reserved /api/status management route.

The endpoint is a placeholder: it answers 501 Not Implemented with an empty
body so no payload schema is committed yet, and it enforces the same
management auth as the other management routes.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings

API_KEY = settings.management_api_key


@pytest.fixture
def client():
    from app.main import app

    yield TestClient(app)


def test_status_requires_management_key(client: TestClient):
    resp = client.get("/api/status")
    assert resp.status_code == 401


def test_status_returns_501_placeholder(client: TestClient):
    resp = client.get("/api/status", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 501
    assert resp.content == b""
