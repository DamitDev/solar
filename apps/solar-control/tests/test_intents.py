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
from app.validation import (
    VALID_GPU_TYPES,
    canonicalize_intent_backend,
    normalize_gpu_type,
    validate_intent_create,
    validate_intent_update,
    validate_intent_warnings,
)

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
            "backend_type": "huggingface_causal",
            "device": "cuda",
            "dtype": "float16",
            "max_length": 512,
            "use_flash_attention": True,
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


def test_validate_intent_dspark_requires_a_draft_model():
    """draft-dspark drafts with a second GGUF, so the spec must name one."""
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://unsloth/Model-GGUF",
            "backend": {"backend_type": "llamacpp", "spec_type": "draft-dspark"},
        }
    )
    assert any(e["field"] == "backend.spec_draft_model" for e in errors)


def test_validate_intent_dspark_backend_is_accepted():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://unsloth/Model-GGUF",
            "backend": {
                "backend_type": "llamacpp",
                "spec_type": "draft-dspark",
                "spec_draft_model": "*DSpark*.gguf",
                "spec_draft_n_max": 7,
                "spec_draft_conf_min": 0.4,
            },
        }
    )
    assert errors == []


def test_validate_intent_draft_model_requires_dspark():
    for backend in [
        {"backend_type": "llamacpp", "spec_draft_model": "*DSpark*.gguf"},
        {
            "backend_type": "llamacpp",
            "spec_type": "draft-mtp",
            "spec_draft_model": "*DSpark*.gguf",
        },
    ]:
        errors = validate_intent_create(
            {
                "alias": "x",
                "model_source": "huggingface://unsloth/Model-GGUF",
                "backend": backend,
            }
        )
        assert any(
            e["field"] == "backend.spec_draft_model" for e in errors
        ), f"Expected a spec_draft_model error for {backend!r}"


def test_validate_intent_conf_min_requires_dspark():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://unsloth/Model-GGUF",
            "backend": {
                "backend_type": "llamacpp",
                "spec_type": "draft-mtp",
                "spec_draft_conf_min": 0.4,
            },
        }
    )
    assert any(e["field"] == "backend.spec_draft_conf_min" for e in errors)


def test_validate_intent_conf_min_must_be_a_probability():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://unsloth/Model-GGUF",
            "backend": {
                "backend_type": "llamacpp",
                "spec_type": "draft-dspark",
                "spec_draft_model": "*DSpark*.gguf",
                "spec_draft_conf_min": 1.5,
            },
        }
    )
    assert any(e["field"] == "backend.spec_draft_conf_min" for e in errors)


def test_validate_intent_spec_type_requires_llamacpp():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://org/model",
            "backend": {
                "backend_type": "huggingface_causal",
                "spec_type": "draft-dspark",
            },
        }
    )
    assert any(e["field"] == "backend.spec_type" for e in errors)


def test_validate_intent_unknown_spec_type_is_rejected():
    errors = validate_intent_create(
        {
            "alias": "x",
            "model_source": "huggingface://unsloth/Model-GGUF",
            "backend": {"backend_type": "llamacpp", "spec_type": "draft-eagle3"},
        }
    )
    assert any(e["field"] == "backend.spec_type" for e in errors)


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


@pytest.fixture(autouse=True)
def _mock_fleet_validation(monkeypatch):
    """Route tests run without a database — stub the fleet validation layer.

    The fleet layer (host roster + Redis snapshots, C3) is covered by its
    own unit tests (tests/test_intent_validation_fleet.py) and the
    integration suite; here it would just trip 'Database not initialized'.
    """

    async def _noop_fleet(data):
        return [], []

    monkeypatch.setattr(
        "app.services.intent_validation.validate_intent_fleet", _noop_fleet
    )


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


def test_validate_intent_update_grandfathers_unchanged_backend_field():
    """A stored intent must stay editable after the ownership table tightens.

    Every update replays the full spec, so a field the table only started
    rejecting later would block edits to unrelated fields forever.
    """
    stored = {"backend_type": "llamacpp", "device": "cuda", "ctx_size": 4096}
    payload = _update_payload(
        backend={"backend_type": "llamacpp", "device": "cuda", "ctx_size": 8192}
    )
    errors = validate_intent_update(
        payload, current_alias="test-model", current_backend=stored
    )
    assert errors == []


def test_validate_intent_update_still_rejects_newly_added_bad_field():
    """Grandfathering is per-field: a value the user actually changes is checked."""
    stored = {"backend_type": "llamacpp", "device": "cuda"}
    payload = _update_payload(
        backend={"backend_type": "llamacpp", "device": "mps"},
    )
    errors = validate_intent_update(
        payload, current_alias="test-model", current_backend=stored
    )
    assert any(e["field"] == "backend.device" for e in errors)


def test_validate_intent_update_grandfathering_does_not_hide_new_contradiction():
    """Exemption covers ownership, not cross-field value checks.

    device is unchanged, but pointing gpu_type at a different accelerator is a
    contradiction the user just introduced.
    """
    stored = {"backend_type": "huggingface_causal", "device": "mps"}
    payload = _update_payload(
        model_source="huggingface://org/model",
        backend={"backend_type": "huggingface_causal", "device": "mps"},
        placement={"gpu_type": "nvidia_cuda"},
    )
    errors = validate_intent_update(
        payload, current_alias="test-model", current_backend=stored
    )
    assert any(
        e["field"] == "backend.device" and "nvidia_cuda" in e["message"] for e in errors
    )


def test_validate_intent_update_backend_type_change_drops_grandfathering():
    """Switching backend_type re-homes every field, so nothing is exempt."""
    stored = {"backend_type": "llamacpp", "ctx_size": 4096}
    payload = _update_payload(
        backend={"backend_type": "huggingface_causal", "ctx_size": 4096}
    )
    errors = validate_intent_update(
        payload, current_alias="test-model", current_backend=stored
    )
    assert any(e["field"] == "backend.ctx_size" for e in errors)


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
        self.locked_read = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, _model, _pk, *, with_for_update=False):
        self.locked_read = with_for_update
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
async def test_update_status_keeps_an_edit_that_landed_mid_pass():
    """A reconcile pass must not erase a spec change it never saw.

    The whole status document is written at once, so a pass that read the
    intent before the edit would otherwise clear the marker and the progress
    reset the edit wrote — and the edit would be lost, since the marker is the
    only record that the replicas still have to be compared against it.
    """
    from app.database.intents import IntentDB

    row = _intent_row(
        status_json={
            "spec_changed_at": "2026-07-24T02:00:00Z",
            "strategy_progress": None,
        }
    )
    db = IntentDB()
    session = _FakeSession(row)

    with patch.object(IntentDB, "_session", return_value=session):
        await db.update_status(
            row.id,
            status_json={
                "spec_changed_at": None,
                "strategy_progress": {"strategy": "rolling", "phase": "stopping"},
                "ready_replicas": 2,
            },
            spec_version_seen=None,
        )

    # The comparison this test exercises is only sound on a locked read: an
    # unlocked one lets the edit commit after it, leaving the stored value
    # looking unchanged so the branch below never runs. See
    # tests_integration/infrastructure/test_spec_marker_concurrency.py.
    assert session.locked_read
    assert row.status_json["spec_changed_at"] == "2026-07-24T02:00:00Z"
    assert row.status_json["strategy_progress"] is None
    assert row.status_json["ready_replicas"] == 2


@pytest.mark.anyio
async def test_update_status_keeps_the_breaker_reset_of_a_mid_pass_edit():
    """The edit's drift reset survives the pass that never saw the edit.

    An edit puts the C1 breaker back to zero because the counter and the
    mismatching keys describe the spec it replaced. A pass still in flight
    counted those against that old spec, so writing its numbers back starts
    the new edit at the previous vector's bound: the reconciler reports it
    unsettled and never attempts the rollout even once.
    """
    from app.database.intents import IntentDB

    row = _intent_row(
        status_json={
            # What the edit wrote: new marker, breaker wound back.
            "spec_changed_at": "2026-07-24T02:00:00Z",
            "strategy_progress": None,
            "last_error": None,
            "drift_replace_attempts": 0,
            "drift_unsettled_keys": [],
        }
    )
    db = IntentDB()

    with patch.object(IntentDB, "_session", return_value=_FakeSession(row)):
        await db.update_status(
            row.id,
            status_json={
                "spec_changed_at": None,
                "strategy_progress": None,
                # What the pass concluded about the previous spec.
                "last_error": {
                    "code": "BackendDriftUnsettled",
                    "message": "max_length",
                    "at": "2026-07-24T01:59:00Z",
                },
                "drift_replace_attempts": 3,
                "drift_unsettled_keys": ["max_length"],
                "ready_replicas": 1,
            },
            spec_version_seen=None,
        )

    assert row.status_json["drift_replace_attempts"] == 0
    assert row.status_json["drift_unsettled_keys"] == []
    assert row.status_json["last_error"] is None
    # Fields the spec write does not own still come from the pass.
    assert row.status_json["ready_replicas"] == 1


@pytest.mark.anyio
async def test_update_status_settles_the_spec_the_pass_reconciled():
    """The pass that did see the edit is the one allowed to clear it."""
    from app.database.intents import IntentDB

    row = _intent_row(status_json={"spec_changed_at": "2026-07-24T02:00:00Z"})
    db = IntentDB()

    with patch.object(IntentDB, "_session", return_value=_FakeSession(row)):
        await db.update_status(
            row.id,
            status_json={"spec_changed_at": None, "strategy_progress": None},
            spec_version_seen="2026-07-24T02:00:00Z",
        )

    assert row.status_json["spec_changed_at"] is None


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


# ── Status round trip (C1) ─────────────────────────────────────

# One entry per key the reconciler writes into status_json, each holding a
# value that differs from the model's default. Hydration is proven by the
# result differing from the default: a key _row_to_response forgets is one
# Pydantic silently fills in, which is how the drift circuit breaker shipped
# unable to fire. Adding a status field means adding a sentinel here.
_STATUS_JSON_SENTINELS: dict[str, object] = {
    "observed_replicas": 3,
    "ready_replicas": 2,
    "updated_replicas": 1,
    "available": True,
    "shortfall": 4,
    "replica_set": [{"instance_id": "inst-1", "host_id": "host-1", "healthy": True}],
    "conditions": [
        {
            "type": "Degraded",
            "status": True,
            "reason": "DriftUnsettled",
            "message": "backend config drift unsettled",
            "last_transition": "2026-08-06T00:00:00+00:00",
        }
    ],
    "strategy_progress": {"strategy": "rolling", "phase": "stopping"},
    "last_error": {
        "code": "BackendDriftUnsettled",
        "message": "mismatching keys: chat_template_kwargs",
        "at": "2026-08-06T00:00:00+00:00",
    },
    "spec_changed_at": "2026-08-06T00:00:00+00:00",
    "drift_replace_attempts": 2,
    "drift_unsettled_keys": ["chat_template_kwargs"],
    "shortfall_reason": "no host matches gpu_type=mps",
}


@pytest.mark.anyio
async def test_every_status_json_key_is_hydrated_on_read():
    """Whatever _update_status persists, _row_to_response must read back.

    The two halves are written independently — one builds a dict, the other
    an explicit keyword list — so nothing but this test stops a new status
    field from being persisted and then silently defaulted on every load.
    """
    from test_reconciliation import _make_intent, _make_observed

    from app.database.intents import IntentDB
    from app.services.reconciliation import Reconciler

    intent = _make_intent()
    with patch("app.database.intents.intent_db") as mock_db:
        mock_db.update_status = AsyncMock()
        mock_db.get_intent = AsyncMock(return_value=intent)
        await Reconciler()._update_status(intent, _make_observed())
    written = set(mock_db.update_status.call_args.kwargs["status_json"])

    assert written == set(_STATUS_JSON_SENTINELS), (
        "status_json keys and the sentinel table disagree; add the new field "
        "to _row_to_response and to _STATUS_JSON_SENTINELS"
    )

    row = _intent_row(status_json=dict(_STATUS_JSON_SENTINELS))
    status = IntentDB()._row_to_response(row).status

    defaults = IntentStatus()
    for key in _STATUS_JSON_SENTINELS:
        assert getattr(status, key) != getattr(defaults, key), (
            f"status.{key} came back as its default, so _row_to_response "
            f"never read it out of status_json"
        )


# ── C3: accelerator vocabulary, field ownership, device contract ──


def _llamacpp_backend(**extra) -> dict:
    return {"backend_type": "llamacpp", **extra}


class TestGpuTypeVocabulary:
    def test_aliases_normalize_to_canonical_tokens(self):
        assert normalize_gpu_type("nvidia") == "nvidia_cuda"
        assert normalize_gpu_type("NVIDIA") == "nvidia_cuda"
        assert normalize_gpu_type("cuda") == "nvidia_cuda"
        assert normalize_gpu_type("nvidia_cuda") == "nvidia_cuda"
        assert normalize_gpu_type("mps") == "apple_mps"
        assert normalize_gpu_type("Metal") == "apple_mps"
        assert normalize_gpu_type("apple_mps") == "apple_mps"
        assert normalize_gpu_type("none") == "cpu"
        assert normalize_gpu_type("cpu") == "cpu"

    def test_unknown_token_returns_none(self):
        assert normalize_gpu_type("quantum") is None
        assert normalize_gpu_type(42) is None

    def test_canonical_names_match_without_relying_on_the_alias_table(self):
        """Both sides of the membership test are folded the same way.

        Folding only the input meant a hyphenated token was compared against
        underscore-bearing canonical names, so every canonical name round-tripped
        purely because the alias table happened to list a hyphenated duplicate.
        """
        with patch.dict("app.validation._NORMALIZED_GPU_ALIASES", {}, clear=True):
            for canonical in VALID_GPU_TYPES:
                assert normalize_gpu_type(canonical) == canonical
                assert normalize_gpu_type(canonical.replace("_", "-")) == canonical
                assert normalize_gpu_type(f"  {canonical.upper()}  ") == canonical

    def test_hyphenated_and_underscored_aliases_are_equivalent(self):
        assert normalize_gpu_type("nvidia-cuda") == "nvidia_cuda"
        assert normalize_gpu_type("apple-mps") == "apple_mps"

    def test_validation_rejects_unknown_gpu_type(self):
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": _llamacpp_backend(model_file="m.gguf"),
            "placement": {"gpu_type": "quantum"},
        }
        errors = validate_intent_create(data)
        assert any(
            e["field"] == "placement.gpu_type" and "quantum" in e["message"]
            for e in errors
        )

    def test_validation_canonicalizes_gpu_type_in_place(self):
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": _llamacpp_backend(model_file="m.gguf"),
            "placement": {"gpu_type": "NVIDIA"},
        }
        assert validate_intent_create(data) == []
        assert data["placement"]["gpu_type"] == "nvidia_cuda"


class TestFieldOwnership:
    def test_device_on_llamacpp_is_rejected(self):
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": _llamacpp_backend(model_file="m.gguf", device="cuda"),
        }
        errors = validate_intent_create(data)
        assert any(
            e["field"] == "backend.device"
            and "huggingface" in e["message"]
            and "n_gpu_layers" in e["message"]
            for e in errors
        )

    def test_llamacpp_only_field_on_hf_backend_is_rejected(self):
        data = {
            "alias": "t",
            "model_source": "huggingface://org/model",
            "backend": {
                "backend_type": "huggingface_causal",
                "model_file": "x.gguf",
                "ctx_size": 4096,
            },
        }
        errors = validate_intent_create(data)
        assert any(e["field"] == "backend.model_file" for e in errors)
        assert any(e["field"] == "backend.ctx_size" for e in errors)

    def test_per_type_field_on_wrong_hf_backend_is_rejected(self):
        data = {
            "alias": "t",
            "model_source": "huggingface://org/model",
            "backend": {
                "backend_type": "huggingface_causal",
                "labels": ["a", "b"],
            },
        }
        errors = validate_intent_create(data)
        assert any(
            e["field"] == "backend.labels"
            and "huggingface_classification" in e["message"]
            for e in errors
        )

    def test_shared_field_is_legal_for_every_owner(self):
        """use_flash_attention belongs to causal *and* vision (many-to-many).

        A first-match owner lookup rejects it on whichever type is not first
        in the table, which is a false 422 on a configuration the host
        accepts.
        """
        for backend_type in ("huggingface_causal", "huggingface_vision"):
            data = {
                "alias": "t",
                "model_source": "huggingface://org/model",
                "backend": {
                    "backend_type": backend_type,
                    "use_flash_attention": True,
                },
            }
            assert validate_intent_create(data) == [], backend_type

    def test_shared_field_on_llamacpp_reports_one_error(self):
        """Both owners are named, but the field is only reported once."""
        data = {
            "alias": "t",
            "model_source": "repo://test:v1",
            "backend": {
                "backend_type": "llamacpp",
                "use_flash_attention": True,
            },
        }
        errors = validate_intent_create(data)
        flash = [e for e in errors if e["field"] == "backend.use_flash_attention"]
        assert len(flash) == 1
        assert "huggingface_causal" in flash[0]["message"]
        assert "huggingface_vision" in flash[0]["message"]

    def test_explicit_null_backend_field_is_not_a_rejection(self):
        """A null configures nothing: the host default is the same as omitting
        the key, and _validate_device already skips None."""
        data = {
            "alias": "t",
            "model_source": "repo://test:v1",
            "backend": {
                "backend_type": "llamacpp",
                "device": None,
                "dtype": None,
                "trust_remote_code": None,
            },
        }
        assert validate_intent_create(data) == []

    def test_llamacpp_device_reports_one_error_not_two(self):
        """The ownership table and _validate_device both cover device; only the
        more specific message (which names n_gpu_layers/ot) is shown."""
        data = {
            "alias": "t",
            "model_source": "repo://test:v1",
            "backend": {"backend_type": "llamacpp", "device": "cuda"},
        }
        errors = [
            e for e in validate_intent_create(data) if e["field"] == "backend.device"
        ]
        assert len(errors) == 1
        assert "n_gpu_layers" in errors[0]["message"]

    def test_hf_common_field_message_names_huggingface_backends(self):
        data = {
            "alias": "t",
            "model_source": "repo://test:v1",
            "backend": {"backend_type": "llamacpp", "dtype": "float16"},
        }
        errors = validate_intent_create(data)
        dtype = [e for e in errors if e["field"] == "backend.dtype"]
        assert len(dtype) == 1
        assert "huggingface_*" in dtype[0]["message"]

    def test_hf_backend_with_repo_source_is_accepted(self):
        """HuggingFace weights in a Harbor artifact are legal — must not be
        'fixed' into a rejection later."""
        data = {
            "alias": "t",
            "model_source": "repo://org/model:v1",
            "backend": {
                "backend_type": "huggingface_causal",
                "device": "cuda",
                "dtype": "float16",
            },
        }
        assert validate_intent_create(data) == []

    def test_ownership_table_matches_documented_field_lists(self):
        """Pins BACKEND_FIELD_OWNERS to the documented sets so a host-side
        field addition fails loudly.

        Asserted as equality, not containment: a subset check passes when the
        table gains a field the documented list omits, which is precisely the
        direction this test exists to catch — the table is control's mirror of
        the host's config models and a field added on one side only is the bug.
        """
        from app.validation import BACKEND_FIELD_OWNERS

        assert BACKEND_FIELD_OWNERS["llamacpp"] == {
            "model_file",
            "mmproj",
            "mmproj_offload",
            "threads",
            "n_gpu_layers",
            "temp",
            "top_p",
            "top_k",
            "min_p",
            "ctx_size",
            "chat_template_file",
            "chat_template_kwargs",
            "reasoning",
            "reasoning_budget",
            "spec_type",
            "spec_draft_n_max",
            "cache_type_k",
            "cache_type_v",
            "rope_scaling",
            "rope_scale",
            "yarn_orig_ctx",
            "special",
            "ot",
            "model_type",
            "pooling",
        }
        assert BACKEND_FIELD_OWNERS["huggingface"] == {
            "device",
            "dtype",
            "max_length",
            "trust_remote_code",
        }
        assert BACKEND_FIELD_OWNERS["huggingface_classification"] == {"labels"}
        assert BACKEND_FIELD_OWNERS["huggingface_embedding"] == {"normalize_embeddings"}
        assert BACKEND_FIELD_OWNERS["huggingface_causal"] == {"use_flash_attention"}
        assert BACKEND_FIELD_OWNERS["huggingface_vision"] == {"use_flash_attention"}
        # Shared fields belong to no single owner.
        for owner in BACKEND_FIELD_OWNERS.values():
            assert "file_filters" not in owner
            assert "backend_type" not in owner


class TestDeviceContract:
    def test_invalid_device_value_rejected(self):
        data = {
            "alias": "t",
            "model_source": "huggingface://org/model",
            "backend": {"backend_type": "huggingface_causal", "device": "tpux"},
        }
        errors = validate_intent_create(data)
        assert any(e["field"] == "backend.device" for e in errors)

    def test_device_contradicting_gpu_type_is_rejected(self):
        """The reported symptom: device mps + gpu_type nvidia_cuda."""
        data = {
            "alias": "t",
            "model_source": "huggingface://org/model",
            "backend": {"backend_type": "huggingface_causal", "device": "mps"},
            "placement": {"gpu_type": "nvidia_cuda"},
        }
        errors = validate_intent_create(data)
        assert any(
            e["field"] == "backend.device"
            and "apple_mps" in e["message"]
            and "nvidia_cuda" in e["message"]
            for e in errors
        )

    def test_device_consistent_with_gpu_type_passes(self):
        data = {
            "alias": "t",
            "model_source": "huggingface://org/model",
            "backend": {"backend_type": "huggingface_causal", "device": "mps"},
            "placement": {"gpu_type": "apple_mps"},
        }
        assert validate_intent_create(data) == []


class TestModalityRules:
    def test_mmproj_on_embedding_mode_rejected(self):
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": _llamacpp_backend(
                model_file="m.gguf", mmproj="mmproj.gguf", model_type="embedding"
            ),
        }
        errors = validate_intent_create(data)
        assert any(
            e["field"] == "backend.mmproj" and "embedding" in e["message"]
            for e in errors
        )

    def test_mmproj_on_llm_mode_accepted(self):
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": _llamacpp_backend(model_file="m.gguf", mmproj="mmproj.gguf"),
        }
        assert validate_intent_create(data) == []

    def test_pooling_without_embedding_mode_warns_not_errors(self):
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": _llamacpp_backend(model_file="m.gguf", pooling="mean"),
        }
        assert validate_intent_create(data) == []
        warnings = validate_intent_warnings(data)
        assert any(w["field"] == "backend.pooling" for w in warnings)


class TestUpdateGrandfathersModalityRules:
    """Both configurations below were legal before the modality rules landed —
    the host silently dropped the field — so they exist in stored specs. An
    update replays the full spec, so without grandfathering the intent becomes
    permanently uneditable on a field the user is not touching.
    """

    def _update(self, backend: dict, current: dict, **overrides) -> list[dict]:
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": backend,
            **overrides,
        }
        return validate_intent_update(data, current_alias="t", current_backend=current)

    def test_carried_over_mmproj_on_embedding_mode_is_editable(self):
        stored = _llamacpp_backend(mmproj="mmproj.gguf", model_type="embedding")
        errors = self._update(dict(stored), stored, replicas=3)
        assert errors == []

    def test_newly_introduced_mmproj_on_embedding_mode_is_rejected(self):
        stored = _llamacpp_backend(model_type="embedding")
        edited = _llamacpp_backend(mmproj="mmproj.gguf", model_type="embedding")
        errors = self._update(edited, stored)
        assert any(
            e["field"] == "backend.mmproj" and "embedding" in e["message"]
            for e in errors
        )

    def test_changed_mmproj_on_embedding_mode_is_rejected(self):
        """Grandfathering is per value, not per key: touching it re-validates."""
        stored = _llamacpp_backend(mmproj="old.gguf", model_type="embedding")
        edited = _llamacpp_backend(mmproj="new.gguf", model_type="embedding")
        errors = self._update(edited, stored)
        assert any(e["field"] == "backend.mmproj" for e in errors)

    def test_carried_over_model_file_on_huggingface_is_editable(self):
        stored = {"backend_type": "huggingface_causal", "model_file": "m.gguf"}
        errors = self._update(dict(stored), stored, replicas=3)
        assert errors == []

    def test_newly_introduced_model_file_on_huggingface_is_rejected(self):
        stored = {"backend_type": "huggingface_causal"}
        edited = {"backend_type": "huggingface_causal", "model_file": "m.gguf"}
        errors = self._update(edited, stored)
        assert any(e["field"] == "backend.model_file" for e in errors)

    def test_file_filters_are_not_grandfathered(self):
        """file_filters is validated against model_source, which an update can
        change independently of the backend, so a carried-over value can become
        newly invalid and must still be reported."""
        stored = _llamacpp_backend(file_filters=["*Q4*"])
        errors = self._update(
            dict(stored), stored, model_source="huggingface://org/model"
        )
        assert errors == []

        # Same untouched backend, a model_source the filters cannot apply to.
        errors = self._update(dict(stored), stored, model_source="repo://x:v1")
        assert any(e["field"] == "backend.file_filters" for e in errors)


class TestChatTemplateKwargsCanonicalization:
    def test_string_kwargs_canonicalized_to_compact_json(self):
        backend = {
            "backend_type": "llamacpp",
            "chat_template_kwargs": '{"enable_thinking": "true", "depth": 3}',
        }
        canonicalize_intent_backend(backend)
        assert backend["chat_template_kwargs"] == '{"enable_thinking":true,"depth":3}'

    def test_dict_kwargs_canonicalized(self):
        backend = {
            "backend_type": "llamacpp",
            "chat_template_kwargs": {"enable_thinking": True},
        }
        canonicalize_intent_backend(backend)
        assert backend["chat_template_kwargs"] == '{"enable_thinking":true}'

    def test_malformed_json_raises_422(self):
        from fastapi import HTTPException

        backend = {"backend_type": "llamacpp", "chat_template_kwargs": "{nope"}
        with pytest.raises(HTTPException) as excinfo:
            canonicalize_intent_backend(backend)
        assert excinfo.value.status_code == 422
        errors = excinfo.value.detail["errors"]
        assert errors[0]["field"] == "backend.chat_template_kwargs"

    def test_non_object_kwargs_raises_422(self):
        from fastapi import HTTPException

        backend = {"backend_type": "llamacpp", "chat_template_kwargs": "[1, 2]"}
        with pytest.raises(HTTPException):
            canonicalize_intent_backend(backend)

    def test_validation_accepts_canonicalized_round_trip(self):
        data = {
            "alias": "t",
            "model_source": "repo://x:v1",
            "backend": _llamacpp_backend(
                model_file="m.gguf",
                chat_template_kwargs='{"enable_thinking": "true"}',
            ),
        }
        assert validate_intent_create(data) == []
