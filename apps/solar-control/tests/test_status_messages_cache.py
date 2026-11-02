"""Tests for status-cache invalidation on status message writes."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.models.status_message import StatusMessageResponse

MANAGEMENT_KEY = {"X-API-Key": "change-me-management"}
MESSAGE_ID = "00000000-0000-0000-0000-000000000001"
DB = "app.routes.management.status_messages.status_message_db"
CACHE = "app.routes.management.status_messages.status_summary_service"

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _msg(**overrides) -> StatusMessageResponse:
    data = {
        "id": MESSAGE_ID,
        "body": "Weekend maintenance",
        "type": "planned maintenance",
        "start_time": NOW,
        "end_time": NOW + timedelta(hours=4),
        "created_at": NOW,
    }
    data.update(overrides)
    return StatusMessageResponse(**data)


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_create_invalidates_the_status_cache():
    clear = MagicMock()
    with patch(f"{DB}.create", new=AsyncMock(return_value=_msg())), patch(
        f"{CACHE}.clear_cache", clear
    ):
        response = _client().post(
            "/api/status-messages",
            json={
                "body": "Weekend maintenance",
                "type": "planned maintenance",
                "start_time": NOW.isoformat(),
                "end_time": (NOW + timedelta(hours=4)).isoformat(),
            },
            headers=MANAGEMENT_KEY,
        )
    assert response.status_code == 201
    assert clear.call_count == 1


def test_update_invalidates_the_status_cache():
    clear = MagicMock()
    with patch(
        f"{DB}.update", new=AsyncMock(return_value=_msg(body="Extended"))
    ), patch(f"{CACHE}.clear_cache", clear):
        response = _client().put(
            f"/api/status-messages/{MESSAGE_ID}",
            json={"body": "Extended"},
            headers=MANAGEMENT_KEY,
        )
    assert response.status_code == 200
    assert clear.call_count == 1


def test_delete_invalidates_the_status_cache():
    clear = MagicMock()
    with patch(f"{DB}.delete", new=AsyncMock(return_value=True)), patch(
        f"{CACHE}.clear_cache", clear
    ):
        response = _client().delete(
            f"/api/status-messages/{MESSAGE_ID}", headers=MANAGEMENT_KEY
        )
    assert response.status_code == 200
    assert clear.call_count == 1


def test_failed_update_does_not_invalidate():
    clear = MagicMock()
    with patch(f"{DB}.update", new=AsyncMock(return_value=None)), patch(
        f"{CACHE}.clear_cache", clear
    ):
        response = _client().put(
            f"/api/status-messages/{MESSAGE_ID}",
            json={"body": "Extended"},
            headers=MANAGEMENT_KEY,
        )
    assert response.status_code == 404
    clear.assert_not_called()


def test_failed_delete_does_not_invalidate():
    clear = MagicMock()
    with patch(f"{DB}.delete", new=AsyncMock(return_value=False)), patch(
        f"{CACHE}.clear_cache", clear
    ):
        response = _client().delete(
            f"/api/status-messages/{MESSAGE_ID}", headers=MANAGEMENT_KEY
        )
    assert response.status_code == 404
    clear.assert_not_called()
