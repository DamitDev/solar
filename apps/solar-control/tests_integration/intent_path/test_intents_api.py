"""intent_path: intents API surface (marker: intent_path)."""

from __future__ import annotations

import uuid

import pytest
from fixtures.constants import MODEL_SOURCE_URI
from fixtures.intents import create_intent, intent_payload, update_intent

pytestmark = pytest.mark.intent_path


def _alias(prefix: str = "intent") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def test_create_intent_201_pending(http_control, clean_state):
    """POST /api/intents -> 201, phase pending, desired fields echoed."""
    alias = _alias()
    intent = await create_intent(http_control, alias=alias, replicas=1)

    assert intent["alias"] == alias
    assert intent["model_source"] == MODEL_SOURCE_URI
    assert intent["replicas"] == 1
    assert intent["priority"] == "production"
    assert intent["strategy"] == "rolling"
    assert intent["backend"]["backend_type"] == "huggingface_classification"

    status = intent["status"]
    assert status["phase"] == "pending"
    assert status["reconcile"] in ("idle", "in_progress")
    assert status["desired_replicas"] == 1
    assert status["observed_replicas"] == 0


async def test_alias_conflict_409(http_control, clean_state):
    """A second active intent with the same alias -> 409."""
    alias = _alias()
    await create_intent(http_control, alias=alias)
    resp = await http_control.post("/api/intents", json=intent_payload(alias))
    assert resp.status_code == 409, resp.text
    assert "already exists" in resp.json()["detail"]["detail"]


async def test_validation_422(http_control, clean_state):
    """Bad model_source scheme / bad enums -> 422 with structured errors."""
    from fixtures.intents import intent_payload

    # Bad scheme (passes Pydantic, hits custom validation)
    resp = await http_control.post(
        "/api/intents",
        json=intent_payload(_alias(), model_source="http://not-a-uri"),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"]["detail"] == "Invalid intent"
    assert isinstance(body["detail"]["errors"], list) and body["detail"]["errors"]

    # Bad priority enum
    resp = await http_control.post(
        "/api/intents",
        json=intent_payload(_alias(), priority="ultra"),
    )
    assert resp.status_code == 422, resp.text

    # Bad strategy enum
    resp = await http_control.post(
        "/api/intents",
        json=intent_payload(_alias(), strategy="canary"),
    )
    assert resp.status_code == 422, resp.text


async def test_list_filters_and_get_404(http_control, clean_state):
    """GET /api/intents filters by alias/phase; unknown id -> 404."""
    alias = _alias()
    created = await create_intent(http_control, alias=alias)

    resp = await http_control.get("/api/intents")
    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()]
    assert created["id"] in ids

    resp = await http_control.get("/api/intents", params={"alias": alias})
    assert [i["id"] for i in resp.json()] == [created["id"]]

    resp = await http_control.get("/api/intents", params={"phase": "ready"})
    assert created["id"] not in [i["id"] for i in resp.json()]

    resp = await http_control.get(f"/api/intents/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_update_replaces_spec(http_control, clean_state):
    """PUT /api/intents/{id} -> 200 with the new spec (S-044 §12.5)."""
    intent = await create_intent(http_control, alias=_alias(), replicas=1)

    updated = await update_intent(
        http_control,
        intent,
        replicas=2,
        priority="staging",
        strategy="immediate",
        metadata={"owner": "integration"},
    )

    assert updated["id"] == intent["id"]
    assert updated["replicas"] == 2
    assert updated["priority"] == "staging"
    assert updated["strategy"] == "immediate"
    assert updated["metadata"] == {"owner": "integration"}
    assert updated["status"]["desired_replicas"] == 2

    # The change is durable, not just echoed back
    resp = await http_control.get(f"/api/intents/{intent['id']}")
    assert resp.status_code == 200
    assert resp.json()["replicas"] == 2


async def test_update_omitted_fields_reset_to_defaults(http_control, clean_state):
    """Full-replace semantics: an omitted field is not a partial patch."""
    intent = await create_intent(
        http_control, alias=_alias(), replicas=2, metadata={"owner": "integration"}
    )

    payload = intent_payload(intent["alias"])
    resp = await http_control.put(f"/api/intents/{intent['id']}", json=payload)
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["replicas"] == 1
    assert body["metadata"] == {}


async def test_update_alias_is_immutable(http_control, clean_state):
    """The alias is the served name and the deployment identity -> 422."""
    intent = await create_intent(http_control, alias=_alias())

    payload = intent_payload(_alias("renamed"))
    resp = await http_control.put(f"/api/intents/{intent['id']}", json=payload)

    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]["errors"]
    assert [e["field"] for e in errors] == ["alias"]

    resp = await http_control.get(f"/api/intents/{intent['id']}")
    assert resp.json()["alias"] == intent["alias"]


async def test_update_validation_and_404(http_control, clean_state):
    """An update cannot write a spec creation would reject; unknown id 404s."""
    intent = await create_intent(http_control, alias=_alias())

    resp = await http_control.put(
        f"/api/intents/{intent['id']}",
        json=intent_payload(intent["alias"], model_source="http://not-a-uri"),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["detail"] == "Invalid intent"

    resp = await http_control.put(
        f"/api/intents/{uuid.uuid4()}", json=intent_payload(_alias())
    )
    assert resp.status_code == 404, resp.text


async def test_delete_202_deleting(http_control, clean_state):
    """DELETE /api/intents/{id} -> 202 with phase deleting."""
    intent = await create_intent(http_control, alias=_alias())
    resp = await http_control.delete(f"/api/intents/{intent['id']}")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["id"] == intent["id"]
    assert body["phase"] == "deleting"

    # If the reconciler already finished cleanup (a no-instance intent
    # soft-deletes within a tick) the second delete 404s; otherwise it is
    # idempotently accepted while phase == deleting. Both are legal.
    resp = await http_control.delete(f"/api/intents/{intent['id']}")
    assert resp.status_code in (202, 404), resp.text
