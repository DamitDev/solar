"""Tests for the status message management API."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database.status_messages import StatusMessageWindowError
from app.models.status_message import StatusMessageResponse

MANAGEMENT_KEY = {"X-API-Key": "change-me-management"}
MESSAGE_ID = "00000000-0000-0000-0000-000000000001"
DB = "app.routes.management.status_messages.status_message_db"

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


class TestStatusMessageRoutes:
    def test_create_returns_201(self):
        created = _msg()
        with patch(f"{DB}.create", new=AsyncMock(return_value=created)) as create:
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
        data = response.json()
        assert data["id"] == MESSAGE_ID
        assert data["type"] == "planned maintenance"
        sent = create.await_args.args[0]
        assert sent.start_time == NOW

    def test_create_open_ended(self):
        with patch(
            f"{DB}.create", new=AsyncMock(return_value=_msg(end_time=None))
        ) as create:
            response = _client().post(
                "/api/status-messages",
                json={
                    "body": "Investigating elevated errors",
                    "type": "incident",
                    "start_time": NOW.isoformat(),
                },
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 201
        assert response.json()["end_time"] is None
        assert create.await_args.args[0].end_time is None

    def test_create_naive_times_are_utc(self):
        with patch(f"{DB}.create", new=AsyncMock(return_value=_msg())) as create:
            response = _client().post(
                "/api/status-messages",
                json={
                    "body": "x",
                    "type": "incident",
                    "start_time": "2026-10-21T12:00:00",
                    "end_time": "2026-10-21T13:00:00",
                },
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 201
        assert create.await_args.args[0].start_time == NOW

    def test_list(self):
        with patch(
            f"{DB}.list_all",
            new=AsyncMock(return_value=[_msg(), _msg(type="incident")]),
        ):
            response = _client().get("/api/status-messages", headers=MANAGEMENT_KEY)
        assert response.status_code == 200
        assert [m["type"] for m in response.json()] == [
            "planned maintenance",
            "incident",
        ]

    def test_update_success(self):
        updated = _msg(body="Maintenance extended")
        with patch(f"{DB}.update", new=AsyncMock(return_value=updated)):
            response = _client().put(
                f"/api/status-messages/{MESSAGE_ID}",
                json={"body": "Maintenance extended"},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 200
        assert response.json()["body"] == "Maintenance extended"

    def test_update_can_clear_end_time(self):
        with patch(
            f"{DB}.update", new=AsyncMock(return_value=_msg(end_time=None))
        ) as update:
            response = _client().put(
                f"/api/status-messages/{MESSAGE_ID}",
                json={"end_time": None},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 200
        assert "end_time" in update.await_args.args[1].model_fields_set

    def test_update_missing_404(self):
        with patch(f"{DB}.update", new=AsyncMock(return_value=None)):
            response = _client().put(
                f"/api/status-messages/{MESSAGE_ID}",
                json={"body": "x"},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 404

    def test_delete_success(self):
        with patch(f"{DB}.delete", new=AsyncMock(return_value=True)):
            response = _client().delete(
                f"/api/status-messages/{MESSAGE_ID}", headers=MANAGEMENT_KEY
            )
        assert response.status_code == 200

    def test_delete_missing_404(self):
        with patch(f"{DB}.delete", new=AsyncMock(return_value=False)):
            response = _client().delete(
                f"/api/status-messages/{MESSAGE_ID}", headers=MANAGEMENT_KEY
            )
        assert response.status_code == 404

    def test_malformed_id_is_404_without_db(self):
        with patch(f"{DB}.delete", new=AsyncMock()) as delete:
            response = _client().delete(
                "/api/status-messages/not-a-uuid", headers=MANAGEMENT_KEY
            )
        assert response.status_code == 404
        delete.assert_not_awaited()


class TestStatusMessageValidation:
    def test_create_rejects_unknown_type(self):
        response = _client().post(
            "/api/status-messages",
            json={"body": "x", "type": "outage", "start_time": NOW.isoformat()},
            headers=MANAGEMENT_KEY,
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "type_", ["incident", "maintenance", "planned maintenance", "resolution"]
    )
    def test_create_accepts_every_allowed_type(self, type_):
        with patch(f"{DB}.create", new=AsyncMock(return_value=_msg(type=type_))):
            response = _client().post(
                "/api/status-messages",
                json={"body": "x", "type": type_, "start_time": NOW.isoformat()},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 201

    def test_create_rejects_start_after_end(self):
        response = _client().post(
            "/api/status-messages",
            json={
                "body": "x",
                "type": "incident",
                "start_time": (NOW + timedelta(hours=1)).isoformat(),
                "end_time": NOW.isoformat(),
            },
            headers=MANAGEMENT_KEY,
        )
        assert response.status_code == 422

    def test_create_allows_start_equal_end(self):
        with patch(f"{DB}.create", new=AsyncMock(return_value=_msg())):
            response = _client().post(
                "/api/status-messages",
                json={
                    "body": "x",
                    "type": "resolution",
                    "start_time": NOW.isoformat(),
                    "end_time": NOW.isoformat(),
                },
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 201

    def test_create_rejects_empty_body_and_missing_start(self):
        client = _client()
        empty = client.post(
            "/api/status-messages",
            json={"body": "", "type": "incident", "start_time": NOW.isoformat()},
            headers=MANAGEMENT_KEY,
        )
        no_start = client.post(
            "/api/status-messages",
            json={"body": "x", "type": "incident"},
            headers=MANAGEMENT_KEY,
        )
        assert empty.status_code == 422
        assert no_start.status_code == 422

    def test_update_rejects_unknown_type(self):
        response = _client().put(
            f"/api/status-messages/{MESSAGE_ID}",
            json={"type": "outage"},
            headers=MANAGEMENT_KEY,
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("field", ["body", "type", "start_time"])
    def test_update_rejects_explicit_null(self, field):
        with patch(f"{DB}.update", new=AsyncMock(return_value=_msg())) as update:
            response = _client().put(
                f"/api/status-messages/{MESSAGE_ID}",
                json={field: None},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 422
        update.assert_not_awaited()

    def test_update_partial_body_leaves_other_fields_unset(self):
        updated = _msg(body="Maintenance extended")
        with patch(f"{DB}.update", new=AsyncMock(return_value=updated)) as update:
            response = _client().put(
                f"/api/status-messages/{MESSAGE_ID}",
                json={"body": "Maintenance extended"},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 200
        assert update.await_args.args[1].model_fields_set == {"body"}

    def test_update_rejects_inverted_explicit_window(self):
        response = _client().put(
            f"/api/status-messages/{MESSAGE_ID}",
            json={
                "start_time": (NOW + timedelta(hours=2)).isoformat(),
                "end_time": NOW.isoformat(),
            },
            headers=MANAGEMENT_KEY,
        )
        assert response.status_code == 422

    def test_update_rejects_window_inverted_against_stored_value(self):
        """The database rejects the merged window; the route maps it to 422."""
        with patch(
            f"{DB}.update",
            new=AsyncMock(
                side_effect=StatusMessageWindowError(
                    "start_time must not be after end_time"
                )
            ),
        ) as update:
            response = _client().put(
                f"/api/status-messages/{MESSAGE_ID}",
                json={"end_time": (NOW - timedelta(hours=1)).isoformat()},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 422
        assert response.json()["detail"] == "start_time must not be after end_time"
        update.assert_awaited_once()


class TestStatusMessageAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/status-messages"),
            ("post", "/api/status-messages"),
            ("put", f"/api/status-messages/{MESSAGE_ID}"),
            ("delete", f"/api/status-messages/{MESSAGE_ID}"),
        ],
    )
    def test_routes_require_management_key(self, method, path):
        kwargs = {"json": {}} if method in ("post", "put") else {}
        response = getattr(_client(), method)(path, **kwargs)
        assert response.status_code == 401
