"""intent_path: C3 — impossible configurations are rejected at the API with
field-level errors; dynamic fleet conditions degrade to advisory warnings
(marker: intent_path).

- FAILURE: device: mps with placement.gpu_type: nvidia_cuda -> 422 with a
  backend.device error (the reported symptom).
- FAILURE: host_allow referencing an unknown host id -> 422.
- SUCCESS: replicas above the two-host fleet -> 201 with a warning, not an
  error (a temporarily offline host must never block an edit).
- SUCCESS: a gpu_type alias (NVIDIA) is canonicalized on the stored intent.
"""

from __future__ import annotations

import uuid

import pytest
from fixtures.intents import intent_payload

pytestmark = pytest.mark.intent_path


def _alias(prefix: str = "valid") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _detail_errors(response) -> list[dict]:
    return response.json().get("detail", {}).get("errors", [])


async def test_device_contradicting_gpu_type_is_422(http_control, clean_state):
    """The reported symptom: device mps + an NVIDIA-type host constraint."""
    payload = intent_payload(
        alias=_alias(),
        backend={
            "backend_type": "huggingface_classification",
            "device": "mps",
        },
        placement={"gpu_type": "nvidia_cuda"},
    )
    resp = await http_control.post("/api/intents", json=payload)
    assert resp.status_code == 422, resp.text
    errors = await _detail_errors(resp)
    assert any(
        e["field"] == "backend.device"
        and "apple_mps" in e["message"]
        and "nvidia_cuda" in e["message"]
        for e in errors
    )


async def test_unknown_host_allow_id_is_422(http_control, clean_state):
    payload = intent_payload(
        alias=_alias(),
        placement={"host_allow": ["host-id-that-does-not-exist"]},
    )
    resp = await http_control.post("/api/intents", json=payload)
    assert resp.status_code == 422, resp.text
    errors = await _detail_errors(resp)
    assert any(
        e["field"] == "placement.host_allow"
        and "host-id-that-does-not-exist" in e["message"]
        for e in errors
    )


async def test_replicas_above_fleet_warns_but_saves(http_control, clean_state):
    """3 replicas on a 2-host fleet: advisory warning, still 201."""
    payload = intent_payload(alias=_alias(), replicas=3)
    resp = await http_control.post("/api/intents", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    warnings = data.get("warnings") or []
    assert any(
        w["field"] == "replicas" and "3" in w["message"] for w in warnings
    ), f"expected a replicas warning, got {warnings}"


async def test_gpu_type_alias_canonicalized_on_save(http_control, clean_state):
    """NVIDIA -> nvidia_cuda stored on the intent; the save still succeeds
    (any gpu_type-not-reported warning is advisory, not an error)."""
    payload = intent_payload(
        alias=_alias(),
        placement={"gpu_type": "NVIDIA"},
    )
    resp = await http_control.post("/api/intents", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["placement"]["gpu_type"] == "nvidia_cuda"
    # Warnings may mention the fleet state, but never block the save.
    for w in data.get("warnings") or []:
        assert "gpu_type" not in w["field"] or "not reported" in w["message"]


async def test_validation_error_is_field_level_not_banner_only(
    http_control, clean_state
):
    """The 422 body carries machine-readable {field, message} entries."""
    payload = intent_payload(
        alias=_alias(),
        backend={"backend_type": "llamacpp", "device": "cuda"},
    )
    resp = await http_control.post("/api/intents", json=payload)
    assert resp.status_code == 422, resp.text
    errors = await _detail_errors(resp)
    assert any(e["field"] == "backend.device" for e in errors)
