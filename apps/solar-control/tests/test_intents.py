"""Tests for intent API (S-040)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models.intent import (
    IntentCreate,
    IntentPhase,
    IntentResponse,
    IntentStatus,
    PlacementConstraints,
    ReconcileState,
    ResourceRequirements,
)
from app.validation import validate_intent_create, validate_intent_update

# ── Validation unit tests ──────────────────────────────────────


def test_validate_intent_create_valid_minimal():
    """Minimal valid intent passes validation."""
    data = {
        "alias": "test-model",
        "model_source": "repo://test:v1",
        "backend": {"backend_type": "huggingface_classification"},
    }
    errors = validate_intent_create(data)
    assert errors == []


def test_validate_intent_create_valid_full():
    """Full intent with all optional fields passes."""
    data = {
        "alias": "test-model",
        "model_source": "huggingface://org/model",
        "replicas": 3,
        "priority": "staging",
        "strategy": "immediate",
        "backend": {
            "backend_type": "llamacpp",
            "dtype": "float16",
            "max_length": 512,
        },
        "placement": {
            "roles": ["inference"],
            "gpu_type": "nvidia_cuda",
            "host_allow": ["h1"],
            "host_deny": ["h2"],
        },
        "resources": {"vram_gb": 8.0, "ram_gb": 16.0},
        "metadata": {"source": "supernova"},
    }
    errors = validate_intent_create(data)
    assert errors == []


def test_validate_intent_missing_alias():
    errors = validate_intent_create(
        {
            "model_source": "repo://x:v1",
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert any(e["field"] == "alias" for e in errors)


def test_validate_intent_missing_model_source():
    errors = validate_intent_create(
        {
            "alias": "x",
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert any(e["field"] == "model_source" for e in errors)


def test_validate_intent_invalid_scheme():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "http://example.com/model",
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert any(e["field"] == "model_source" for e in errors)


def test_validate_intent_local_scheme():
    """local:// is a valid scheme."""
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "local:///opt/models/model.gguf",
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert errors == []


def test_validate_intent_huggingface_scheme():
    """huggingface:// is a valid scheme."""
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://meta-llama/Llama-2-7b-hf",
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert errors == []


def test_validate_intent_negative_replicas():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "replicas": -1,
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert any(e["field"] == "replicas" for e in errors)


def test_validate_intent_zero_replicas():
    """replicas=0 is valid (pre-create then scale up)."""
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "replicas": 0,
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert errors == []


def test_validate_intent_invalid_priority():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "priority": "critical",
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert any(e["field"] == "priority" for e in errors)


def test_validate_intent_all_valid_priorities():
    """All three valid priorities pass."""
    for p in ["production", "staging", "ephemeral"]:
        errors = validate_intent_create(
            {
                "alias": "x",
                "model_source": "repo://x:v1",
                "priority": p,
                "backend": {"backend_type": "llamacpp"},
            }
        )
        assert errors == [], f"Priority '{p}' should be valid"


def test_validate_intent_invalid_strategy():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "strategy": "blue-green",
            "backend": {"backend_type": "llamacpp"},
        }
    )
    assert any(e["field"] == "strategy" for e in errors)


def test_validate_intent_all_valid_strategies():
    """Both valid strategies pass."""
    for s in ["rolling", "immediate"]:
        errors = validate_intent_create(
            {
                "alias": "x",
                "model_source": "repo://x:v1",
                "strategy": s,
                "backend": {"backend_type": "llamacpp"},
            }
        )
        assert errors == [], f"Strategy '{s}' should be valid"


def test_validate_intent_missing_backend_type():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "backend": {},
        }
    )
    assert any(e["field"] == "backend.backend_type" for e in errors)


def test_validate_intent_invalid_backend_type():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "backend": {"backend_type": "unknown_type"},
        }
    )
    assert any(e["field"] == "backend.backend_type" for e in errors)


def test_validate_intent_all_valid_backend_types():
    """All five valid backend types pass."""
    for bt in [
        "llamacpp",
        "huggingface_causal",
        "huggingface_classification",
        "huggingface_embedding",
        "huggingface_vision",
    ]:
        errors = validate_intent_create(
            {
                "alias": "x",
                "model_source": "repo://x:v1",
                "backend": {"backend_type": bt},
            }
        )
        assert errors == [], f"backend_type '{bt}' should be valid"


def test_validate_intent_forbidden_backend_fields():
    """Server-derived fields must not appear in backend."""
    for forbidden in ["alias", "model_source", "host", "port", "api_key"]:
        errors = validate_intent_create(
            {
                "alias": "x",
                "model_source": "repo://x:v1",
                "backend": {"backend_type": "llamacpp", forbidden: "value"},
            }
        )
        assert any(
            e["field"] == f"backend.{forbidden}" for e in errors
        ), f"Expected error for backend.{forbidden}"


def test_validate_intent_model_file_requires_llamacpp():
    """model_file selects a GGUF, which only llama.cpp consumes."""
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://org/model",
            "backend": {
                "backend_type": "huggingface_causal",
                "model_file": "*Q4*.gguf",
            },
        }
    )
    assert any(e["field"] == "backend.model_file" for e in errors)


def test_validate_intent_model_file_accepted_for_llamacpp():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://unsloth/Model-GGUF",
            "backend": {"backend_type": "llamacpp", "model_file": "*UD-Q4_K_XL*.gguf"},
        }
    )
    assert errors == []


def test_validate_intent_file_filters_require_huggingface_source():
    """Only a HuggingFace snapshot can be restricted to a subset of files."""
    for source in ["repo://x:v1", "local:///opt/models/x"]:
        errors = validate_intent_create(
            {
                "alias": "x",
                "model_source": source,
                "backend": {
                    "backend_type": "llamacpp",
                    "file_filters": ["*UD-Q4_K_XL*"],
                },
            }
        )
        assert any(
            e["field"] == "backend.file_filters" for e in errors
        ), f"Expected a file_filters error for '{source}'"


def test_validate_intent_file_filters_accepted_for_huggingface_source():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://unsloth/Model-GGUF",
            "backend": {
                "backend_type": "llamacpp",
                "file_filters": ["*UD-Q4_K_XL*", "mmproj-BF16.gguf"],
            },
        }
    )
    assert errors == []


def test_validate_intent_file_filters_must_be_patterns():
    for filters in ["*UD-Q4_K_XL*", ["  "], [3]]:
        errors = validate_intent_create(
            {
                "alias": "x",
                "model_source": "huggingface://unsloth/Model-GGUF",
                "backend": {"backend_type": "llamacpp", "file_filters": filters},
            }
        )
        assert any(
            e["field"] == "backend.file_filters" for e in errors
        ), f"Expected a file_filters error for {filters!r}"


def test_validate_intent_empty_file_filters_is_allowed():
    """An empty list means no filtering, which is valid for any source."""
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "backend": {"backend_type": "llamacpp", "file_filters": []},
        }
    )
    assert errors == []


def test_validate_intent_empty_placement_roles():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "repo://x:v1",
            "backend": {"backend_type": "llamacpp"},
            "placement": {"roles": []},
        }
    )
    assert any(e["field"] == "placement.roles" for e in errors)


# ── Model unit tests ────────────────────────────────────────────


def test_intent_create_defaults():
    """IntentCreate applies correct defaults."""
    intent = IntentCreate(
        alias="m",
        model_source="repo://m:v1",
        backend={"backend_type": "llamacpp"},
    )
    assert intent.replicas == 1
    assert intent.priority == "production"
    assert intent.strategy == "rolling"
    assert intent.placement.roles == ["inference"]
    assert intent.resources.vram_gb is None


def test_intent_status_defaults():
    """IntentStatus has correct new-intent defaults."""
    status = IntentStatus()
    assert status.phase == IntentPhase.PENDING
    assert status.reconcile == ReconcileState.IDLE
    assert status.observed_replicas == 0
    assert status.ready_replicas == 0
    assert status.available is False


# ── Route integration tests (mock IntentDB) ────────────────────


@pytest.fixture
def valid_intent_create() -> IntentCreate:
    return IntentCreate(
        alias="test-model",
        model_source="repo://test:v1",
        replicas=2,
        priority="production",
        strategy="rolling",
        backend={"backend_type": "huggingface_classification", "max_length": 512},
        placement=PlacementConstraints(roles=["inference"], gpu_type="nvidia_cuda"),
        resources=ResourceRequirements(vram_gb=6.0),
    )


@pytest.fixture
def mock_intent_response() -> IntentResponse:
    return IntentResponse(
        id="550e8400-e29b-41d4-a716-446655440000",
        alias="test-model",
        model_source="repo://test:v1",
        replicas=2,
        priority="production",
        strategy="rolling",
        backend={"backend_type": "huggingface_classification", "max_length": 512},
        placement=PlacementConstraints(roles=["inference"], gpu_type="nvidia_cuda"),
        resources=ResourceRequirements(vram_gb=6.0),
        metadata={},
        status=IntentStatus(
            phase=IntentPhase.PENDING,
            reconcile=ReconcileState.IDLE,
            desired_replicas=2,
            observed_replicas=0,
            ready_replicas=0,
            updated_replicas=0,
            available=False,
            shortfall=0,
            created_at="2026-07-24T00:00:00Z",
            updated_at="2026-07-24T00:00:00Z",
        ),
    )


@pytest.mark.anyio
async def test_create_intent_success(valid_intent_create, mock_intent_response):
    """POST /api/intents returns 201 with pending status."""
    from fastapi.testclient import TestClient

    with (
        patch(
            "app.routes.management.intents.intent_db.create_intent",
            new=AsyncMock(return_value=mock_intent_response),
        ),
        patch(
            "app.routes.management.intents.intent_db.check_alias_conflict",
            new=AsyncMock(return_value=False),
        ),
    ):
        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/api/intents",
            json=valid_intent_create.model_dump(),
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == mock_intent_response.id
        assert data["alias"] == "test-model"
        assert data["status"]["phase"] == "pending"
        assert data["status"]["reconcile"] == "idle"


@pytest.mark.anyio
async def test_create_intent_alias_conflict(valid_intent_create):
    """POST with duplicate alias returns 409."""
    with patch(
        "app.routes.management.intents.intent_db.check_alias_conflict",
        new=AsyncMock(return_value=True),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/api/intents",
            json=valid_intent_create.model_dump(),
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 409


@pytest.mark.anyio
async def test_create_intent_validation_error():
    """POST with invalid data returns 422."""
    with patch(
        "app.routes.management.intents.intent_db.check_alias_conflict",
        new=AsyncMock(return_value=False),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.post(
            "/api/intents",
            json={
                "alias": "valid-alias",
                "model_source": "http://bad",
                "backend": {"backend_type": "invalid_type"},
            },
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        assert "errors" in data["detail"]
        assert len(data["detail"]["errors"]) >= 2  # bad scheme + bad backend_type


@pytest.mark.anyio
async def test_create_intent_unauthorized(valid_intent_create):
    """POST without API key returns 401."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/intents",
        json=valid_intent_create.model_dump(),
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_list_intents(mock_intent_response):
    """GET /api/intents returns list."""
    with patch(
        "app.routes.management.intents.intent_db.list_intents",
        new=AsyncMock(return_value=[mock_intent_response]),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.get(
            "/api/intents",
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == mock_intent_response.id


@pytest.mark.anyio
async def test_get_intent_found(mock_intent_response):
    """GET /api/intents/{id} returns the intent."""
    with patch(
        "app.routes.management.intents.intent_db.get_intent",
        new=AsyncMock(return_value=mock_intent_response),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.get(
            "/api/intents/550e8400-e29b-41d4-a716-446655440000",
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 200
        assert response.json()["id"] == mock_intent_response.id


@pytest.mark.anyio
async def test_get_intent_not_found():
    """GET /api/intents/{id} with unknown ID returns 404."""
    with patch(
        "app.routes.management.intents.intent_db.get_intent",
        new=AsyncMock(return_value=None),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.get(
            "/api/intents/nonexistent",
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 404


@pytest.mark.anyio
async def test_delete_intent_success(mock_intent_response):
    """DELETE /api/intents/{id} returns 202 with deleting phase."""
    mock_intent_response.status.phase = IntentPhase.DELETING
    with patch(
        "app.routes.management.intents.intent_db.soft_delete_intent",
        new=AsyncMock(return_value=mock_intent_response),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.delete(
            "/api/intents/550e8400-e29b-41d4-a716-446655440000",
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["phase"] == "deleting"


@pytest.mark.anyio
async def test_delete_intent_not_found():
    """DELETE with unknown ID returns 404."""
    with patch(
        "app.routes.management.intents.intent_db.soft_delete_intent",
        new=AsyncMock(return_value=None),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.delete(
            "/api/intents/nonexistent",
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 404


@pytest.mark.anyio
async def test_delete_intent_with_orphan(mock_intent_response):
    """DELETE with ?orphan=true returns 202 with orphan message."""
    mock_intent_response.status.phase = IntentPhase.DELETING
    with patch(
        "app.routes.management.intents.intent_db.soft_delete_intent",
        new=AsyncMock(return_value=mock_intent_response),
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.delete(
            "/api/intents/550e8400-e29b-41d4-a716-446655440000?orphan=true",
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 202
        data = response.json()
        assert "orphaned" in data["message"].lower()


@pytest.mark.anyio
async def test_list_intents_with_filters(mock_intent_response):
    """GET /api/intents passes query params to list_intents."""
    mock_list = AsyncMock(return_value=[mock_intent_response])
    with patch(
        "app.routes.management.intents.intent_db.list_intents",
        new=mock_list,
    ):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        response = client.get(
            "/api/intents?priority=production&phase=pending&limit=10&offset=0",
            headers={"X-API-Key": "change-me-management"},
        )

        assert response.status_code == 200
        mock_list.assert_called_once_with(
            alias=None,
            priority="production",
            phase="pending",
            limit=10,
            offset=0,
        )


# ── Update validation (S-044) ──────────────────────────────────


def _update_payload(**overrides) -> dict:
    data = {
        "alias": "test-model",
        "model_source": "repo://test:v2",
        "replicas": 3,
        "priority": "production",
        "strategy": "rolling",
        "backend": {"backend_type": "llamacpp"},
    }
    data.update(overrides)
    return data


def test_validate_intent_update_accepts_unchanged_alias():
    assert validate_intent_update(_update_payload(), current_alias="test-model") == []


def test_validate_intent_update_rejects_changed_alias():
    """The alias is the served name and the deployment's identity (§12.5)."""
    errors = validate_intent_update(
        _update_payload(alias="other-model"), current_alias="test-model"
    )
    assert [e["field"] for e in errors] == ["alias"]
    assert "immutable" in errors[0]["message"]


def test_validate_intent_update_applies_creation_rules():
    """An update must not write a spec that creation would have rejected."""
    errors = validate_intent_update(
        _update_payload(model_source="http://bad", replicas=-1),
        current_alias="test-model",
    )
    fields = {e["field"] for e in errors}
    assert "model_source" in fields
    assert "replicas" in fields


# ── Update route (S-044) ───────────────────────────────────────


@pytest.fixture
def mock_updated_intent(mock_intent_response) -> IntentResponse:
    return mock_intent_response.model_copy(update={"model_source": "repo://test:v2"})


def _put_intent(payload: dict, intent_id: str = "550e8400-e29b-41d4-a716-446655440000"):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app).put(
        f"/api/intents/{intent_id}",
        json=payload,
        headers={"X-API-Key": "change-me-management"},
    )


@pytest.mark.anyio
async def test_update_intent_success(mock_intent_response, mock_updated_intent):
    """PUT /api/intents/{id} replaces the spec and wakes the reconciler."""
    mock_update = AsyncMock(return_value=mock_updated_intent)
    with (
        patch(
            "app.routes.management.intents.intent_db.get_intent",
            new=AsyncMock(return_value=mock_intent_response),
        ),
        patch(
            "app.routes.management.intents.intent_db.check_alias_conflict",
            new=AsyncMock(return_value=False),
        ),
        patch("app.routes.management.intents.intent_db.update_intent", new=mock_update),
        patch("app.services.reconciliation.reconciler.wake") as mock_wake,
    ):
        response = _put_intent(_update_payload())

    assert response.status_code == 200
    assert response.json()["model_source"] == "repo://test:v2"
    assert mock_update.await_args.kwargs["replicas"] == 3
    mock_wake.assert_called_once()


@pytest.mark.anyio
async def test_update_intent_not_found():
    with patch(
        "app.routes.management.intents.intent_db.get_intent",
        new=AsyncMock(return_value=None),
    ):
        response = _put_intent(_update_payload(), intent_id="nonexistent")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_update_intent_rejects_alias_change(mock_intent_response):
    """A changed alias is a 422, not a silent no-op."""
    mock_update = AsyncMock()
    with (
        patch(
            "app.routes.management.intents.intent_db.get_intent",
            new=AsyncMock(return_value=mock_intent_response),
        ),
        patch("app.routes.management.intents.intent_db.update_intent", new=mock_update),
    ):
        response = _put_intent(_update_payload(alias="renamed"))

    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["field"] == "alias"
    mock_update.assert_not_awaited()


@pytest.mark.anyio
async def test_update_intent_rejects_deleting_intent(mock_intent_response):
    """A spec write must not resurrect an intent that is being torn down."""
    mock_intent_response.status.phase = IntentPhase.DELETING
    mock_update = AsyncMock()
    with (
        patch(
            "app.routes.management.intents.intent_db.get_intent",
            new=AsyncMock(return_value=mock_intent_response),
        ),
        patch("app.routes.management.intents.intent_db.update_intent", new=mock_update),
    ):
        response = _put_intent(_update_payload())

    assert response.status_code == 409
    mock_update.assert_not_awaited()


@pytest.mark.anyio
async def test_update_intent_requires_api_key():
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).put(
        "/api/intents/550e8400-e29b-41d4-a716-446655440000",
        json=_update_payload(),
    )

    assert response.status_code == 401


# ── Update persistence (S-044) ─────────────────────────────────


class _FakeSession:
    """Minimal stand-in for an AsyncSession over a single known row."""

    def __init__(self, row):
        self._row = row
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, _model, _pk):
        return self._row

    async def commit(self):
        self.committed = True

    async def refresh(self, _row):
        return None


def _intent_row(**overrides):
    from datetime import datetime, timezone

    from app.database.tables import IntentRow

    row = IntentRow(
        id="550e8400-e29b-41d4-a716-446655440000",
        alias="test-model",
        model_source="repo://test:v1",
        replicas=2,
        priority="production",
        strategy="rolling",
        backend={"backend_type": "llamacpp"},
        placement={},
        resources={},
        metadata_={},
        phase="ready",
        reconcile="idle",
        status_json={
            "strategy_progress": {"strategy": "rolling", "phase": "stopping"},
            "last_error": {
                "code": "start_failed",
                "message": "previous spec failed to start",
                "at": "2026-07-24T01:00:00Z",
            },
        },
        created_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        deleted_at=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _update_kwargs(**overrides) -> dict:
    kwargs = {
        "model_source": "repo://test:v1",
        "replicas": 2,
        "priority": "production",
        "strategy": "rolling",
        "backend": {"backend_type": "llamacpp"},
        "placement": {},
        "resources": {},
        "metadata": {},
    }
    kwargs.update(overrides)
    return kwargs


@pytest.mark.anyio
async def test_update_intent_db_clears_rollout_on_spec_change():
    """A changed spec abandons the in-flight rollout so the next tick
    re-plans against the new target instead of the superseded one (§11.5.1)."""
    from app.database.intents import IntentDB

    row = _intent_row()
    session = _FakeSession(row)
    db = IntentDB()

    with patch.object(IntentDB, "_session", return_value=session):
        result = await db.update_intent(row.id, **_update_kwargs(replicas=4))

    assert row.replicas == 4
    assert row.status_json["strategy_progress"] is None
    assert row.status_json["spec_changed_at"] is not None
    assert row.status_json["last_error"] is None
    assert result is not None
    assert result.status.strategy_progress is None
    assert session.committed is True


@pytest.mark.anyio
async def test_update_intent_db_keeps_rollout_when_spec_is_identical():
    """Re-submitting the same spec must not disturb a running rollout."""
    from app.database.intents import IntentDB

    row = _intent_row()
    db = IntentDB()

    with patch.object(IntentDB, "_session", return_value=_FakeSession(row)):
        await db.update_intent(row.id, **_update_kwargs())

    assert row.status_json["strategy_progress"] == {
        "strategy": "rolling",
        "phase": "stopping",
    }
    assert "spec_changed_at" not in row.status_json


@pytest.mark.anyio
@pytest.mark.parametrize(
    "row_overrides",
    [
        pytest.param({"phase": "deleting"}, id="deleting"),
        pytest.param(
            {"deleted_at": "2026-07-25T00:00:00Z"},
            id="soft-deleted",
        ),
    ],
)
async def test_update_intent_db_refuses_deleted_intents(row_overrides):
    from app.database.intents import IntentDB

    row = _intent_row(**row_overrides)
    db = IntentDB()

    with patch.object(IntentDB, "_session", return_value=_FakeSession(row)):
        assert await db.update_intent(row.id, **_update_kwargs(replicas=9)) is None

    assert row.replicas == 2
