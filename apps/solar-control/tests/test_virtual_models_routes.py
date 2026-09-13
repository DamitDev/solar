"""Tests for virtual model management API (S-060)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.virtual_model import (
    VirtualModelContract,
    VirtualModelResponse,
)

MANAGEMENT_KEY = {"X-API-Key": "change-me-management"}

_WARNING = (
    "target 'planned:8b' is not in the registry yet — "
    "will be checked at routing time"
)


def _vm(name="team-chat", targets=("a:8b",), contract=None, warnings=None):
    return VirtualModelResponse(
        id="00000000-0000-0000-0000-000000000000",
        name=name,
        targets=list(targets),
        contract=contract or VirtualModelContract(),
        warnings=warnings or [],
    )


def _client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def no_cache():
    """Isolate the module-level TTL cache between tests."""
    from app.services.virtual_model_cache import virtual_model_cache

    virtual_model_cache.invalidate()
    yield
    virtual_model_cache.invalidate()


class TestVirtualModelRoutes:
    @pytest.mark.anyio
    async def test_create_success_with_warning(self, no_cache):
        created = _vm(targets=("planned:8b",), warnings=[_WARNING])
        with (
            patch(
                "app.routes.management.virtual_models._live_registry",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.routes.management.virtual_models.virtual_model_db.create",
                new=AsyncMock(return_value=created),
            ),
            patch(
                "app.routes.management.virtual_models.validate_virtual_model",
                MagicMock(return_value=([], [_WARNING])),
            ),
        ):
            response = _client().post(
                "/api/virtual-models",
                json={"name": "team-chat", "targets": ["planned:8b"]},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "team-chat"
        assert data["warnings"] and "planned:8b" in data["warnings"][0]

    @pytest.mark.anyio
    async def test_create_validation_error_422(self, no_cache):
        with (
            patch(
                "app.routes.management.virtual_models._live_registry",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.routes.management.virtual_models.validate_virtual_model",
                MagicMock(return_value=(["name collides", "other error"], [])),
            ),
        ):
            response = _client().post(
                "/api/virtual-models",
                json={"name": "a:8b", "targets": ["x:1b"]},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 422
        assert "errors" in response.json()["detail"]

    @pytest.mark.anyio
    async def test_create_duplicate_409(self, no_cache):
        with (
            patch(
                "app.routes.management.virtual_models._live_registry",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.routes.management.virtual_models.validate_virtual_model",
                MagicMock(return_value=([], [])),
            ),
            patch(
                "app.routes.management.virtual_models.virtual_model_db.create",
                new=AsyncMock(
                    side_effect=ValueError("virtual model 'x' already exists")
                ),
            ),
        ):
            response = _client().post(
                "/api/virtual-models",
                json={"name": "x", "targets": ["a:8b"]},
                headers=MANAGEMENT_KEY,
            )
        assert response.status_code == 409

    @pytest.mark.anyio
    async def test_list_includes_target_status(self, no_cache):
        from types import SimpleNamespace

        registry = {
            "a:8b": [SimpleNamespace(context_size=4096, capabilities=None)],
            "dead:2b": [],
        }
        entries = [
            _vm(name="good", targets=("a:8b",)),
            _vm(
                name="violating",
                targets=("a:8b",),
                contract=VirtualModelContract(context_size=200_000),
            ),
            _vm(name="missing", targets=("dead:2b",)),
        ]
        with (
            patch(
                "app.routes.management.virtual_models.virtual_model_cache.get_all",
                new=MagicMock(return_value=entries),
            ),
            patch(
                "app.routes.management.virtual_models.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            response = _client().get("/api/virtual-models", headers=MANAGEMENT_KEY)
        assert response.status_code == 200
        by_name = {vm["name"]: vm for vm in response.json()}
        assert by_name["good"]["target_status"] == {"a:8b": "satisfied"}
        assert by_name["missing"]["target_status"] == {"dead:2b": "missing"}
        assert by_name["violating"]["target_status"]["a:8b"].startswith("violating:")

    @pytest.mark.anyio
    async def test_delete_missing_404(self, no_cache):
        with patch(
            "app.routes.management.virtual_models.virtual_model_db.delete",
            new=AsyncMock(return_value=False),
        ):
            response = _client().delete(
                "/api/virtual-models/nope", headers=MANAGEMENT_KEY
            )
        assert response.status_code == 404

    @pytest.mark.anyio
    async def test_routes_require_management_key(self, no_cache):
        response = _client().get("/api/virtual-models")
        assert response.status_code == 401
