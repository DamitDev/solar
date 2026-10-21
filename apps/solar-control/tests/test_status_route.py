"""Tests for the public GET /api/status summary route.

The route is the one unauthenticated /api read; every other /api route keeps
requiring the management key. Data sources are patched so the response shape
and the auth boundary are pinned without Redis or Postgres.
"""

from datetime import datetime, timedelta, timezone
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models.gateway import RegistryEntry
from app.models.intent import IntentPhase
from app.models.status_message import StatusMessageResponse
from app.models.virtual_model import VirtualModelContract, VirtualModelResponse
from app.services.status_summary import status_summary_service
from app.services.virtual_model_cache import virtual_model_cache

API_KEY = settings.management_api_key


@pytest.fixture
def client():
    from app.main import app

    status_summary_service.clear_cache()
    virtual_model_cache.invalidate()
    yield TestClient(app)
    status_summary_service.clear_cache()
    virtual_model_cache.invalidate()


def _entry(host_id: str, instance_id: str, alias: str) -> RegistryEntry:
    return RegistryEntry(
        host_id=host_id,
        instance_id=instance_id,
        url=f"http://{host_id}",
        api_key="k",
        model_alias=alias,
    )


def _vm(name: str, targets: list[str]) -> VirtualModelResponse:
    return VirtualModelResponse(
        id="00000000-0000-0000-0000-000000000000",
        name=name,
        targets=targets,
        contract=VirtualModelContract(),
    )


def _fresh_snapshot() -> dict:
    return {"at": datetime.now(timezone.utc).isoformat(), "resources": {}}


def _day_epoch(days_ago: int) -> int:
    today = int(datetime.now(timezone.utc).timestamp()) // 86400 * 86400
    return today - days_ago * 86400


def _patched(
    registry,
    *,
    failing=(),
    vms=(),
    intents=(),
    buckets=None,
    components=(),
    messages=(),
):
    async def availability_buckets(start, end, bucket_s, **kwargs):
        return buckets if buckets is not None else {}

    sources = {
        "app.services.status_summary.status_message_db.list_not_ended": AsyncMock(
            return_value=list(messages)
        ),
        "app.services.status_summary.registry_store.get_registry": AsyncMock(
            return_value=registry
        ),
        "app.services.state_sampler.host_store.get_host_resource_snapshot": AsyncMock(
            return_value=_fresh_snapshot()
        ),
        "app.services.status_summary.intent_db.list_intents": AsyncMock(
            return_value=list(intents)
        ),
        "app.services.status_summary.virtual_model_db.list_all": AsyncMock(
            return_value=list(vms)
        ),
        "app.services.status_summary.state_history_db.list_components": AsyncMock(
            return_value=list(components)
        ),
        "app.services.status_summary.state_history_db.availability_buckets": availability_buckets,
    }
    # A failing source raises like an unreachable Redis/Postgres would.
    for target in failing:
        sources[target] = AsyncMock(side_effect=RuntimeError("source unavailable"))
    return [patch(target, mock) for target, mock in sources.items()]


def _get(client, registry, **kwargs):
    patches = _patched(registry, **kwargs)
    for p in patches:
        p.start()
    try:
        return client.get("/api/status")
    finally:
        for p in patches:
            p.stop()


def test_status_is_public(client: TestClient):
    resp = _get(client, {})
    assert resp.status_code == 200


def test_other_api_routes_still_require_management_key(client: TestClient):
    assert client.get("/api/virtual-models").status_code == 401
    assert client.get("/api/status/anything").status_code == 401


def test_status_write_methods_require_management_key(client: TestClient):
    assert client.post("/api/status").status_code == 401


def test_empty_registry_returns_empty_components(client: TestClient):
    body = _get(client, {}).json()
    assert body["window_days"] == 90
    assert body["bucket"] == "1d"
    assert body["virtual_models"] == []
    assert body["aliases"] == []
    assert body["instances"] == []


def test_response_shape(client: TestClient):
    registry = {
        "core": [_entry("h1", "i1", "core")],
        "wind": [_entry("h1", "i2", "wind"), _entry("h2", "i3", "wind")],
        "adhoc": [_entry("h2", "i4", "adhoc")],
    }
    intents = [
        SimpleNamespace(
            alias="wind", status=SimpleNamespace(phase=IntentPhase.DEGRADED)
        ),
        SimpleNamespace(alias="core", status=SimpleNamespace(phase=IntentPhase.READY)),
    ]
    vms = [_vm("team-chat", ["core", "ghost"])]
    buckets = {_day_epoch(0): (10, 10)}

    body = _get(client, registry, vms=vms, intents=intents, buckets=buckets).json()

    [vm] = body["virtual_models"]
    assert vm["name"] == "team-chat"
    assert vm["targets"] == ["core", "ghost"]
    assert vm["target_status"] == {"core": "satisfied", "ghost": "missing"}
    # One of two targets can serve.
    assert vm["availability"] == "degraded"

    by_alias = {a["alias"]: a for a in body["aliases"]}
    assert set(by_alias) == {"core", "wind", "adhoc"}
    assert by_alias["core"]["availability"] == "operational"
    assert by_alias["core"]["intent_phase"] == "ready"
    assert by_alias["wind"]["intent_phase"] == "degraded"
    assert by_alias["wind"]["instances_total"] == 2
    assert by_alias["adhoc"]["intent_phase"] is None

    # Only the alias no intent manages lists its instance as a component.
    assert [(i["instance_id"], i["alias"]) for i in body["instances"]] == [
        ("i4", "adhoc")
    ]
    assert body["instances"][0]["availability"] == "operational"


def test_history_is_gap_free_90_day_series(client: TestClient):
    registry = {"core": [_entry("h1", "i1", "core")]}
    buckets = {_day_epoch(0): (10, 10), _day_epoch(5): (10, 5)}

    body = _get(client, registry, buckets=buckets).json()

    [core] = body["aliases"]
    history = core["history"]
    assert len(history) == 90
    stamps = [datetime.fromisoformat(p["ts"]) for p in history]
    assert all(b - a == timedelta(days=1) for a, b in pairwise(stamps))
    assert history[-1]["uptime"] == 1.0
    assert history[-6]["uptime"] == 0.5
    # Days between the two sampled ones are outage gaps, not interpolated.
    assert history[-2] == {"ts": history[-2]["ts"], "uptime": 0.0, "samples": 0}
    assert core["uptime_pct"] is not None


def test_requestless_outage_still_counts_as_downtime(client: TestClient):
    """Uptime comes from state samples, not gateway request logs."""
    registry = {"core": [_entry("h1", "i1", "core")]}
    buckets = {_day_epoch(2): (10, 10), _day_epoch(0): (10, 10)}

    body = _get(client, registry, buckets=buckets).json()

    [core] = body["aliases"]
    # Day -1 has no samples at all: it is downtime, so 2 of 3 days are up.
    assert core["history"][-2]["uptime"] == 0.0
    assert core["uptime_pct"] == pytest.approx(66.667, abs=0.001)


def _message(
    type_: str, start: datetime, end: datetime | None
) -> StatusMessageResponse:
    return StatusMessageResponse(
        id="00000000-0000-0000-0000-000000000001",
        body=f"{type_} body",
        type=type_,
        start_time=start,
        end_time=end,
    )


def test_messages_are_public_and_window_filtered(client: TestClient):
    now = datetime.now(timezone.utc)
    hour = timedelta(hours=1)
    messages = [
        _message("incident", now - hour, None),
        _message("planned maintenance", now + hour, now + 2 * hour),
        _message("maintenance", now + hour, now + 2 * hour),
    ]

    body = _get(client, {}, messages=messages).json()

    # Future maintenance is hidden; planned maintenance is published ahead of
    # its start and the client tells it apart via start_time vs generated_at.
    assert [m["type"] for m in body["messages"]] == ["incident", "planned maintenance"]
    assert body["messages"][0]["body"] == "incident body"
    assert body["messages"][0]["end_time"] is None
    generated_at = datetime.fromisoformat(body["generated_at"])
    assert datetime.fromisoformat(body["messages"][1]["start_time"]) > generated_at


def test_no_messages_returns_empty_list(client: TestClient):
    body = _get(client, {}).json()
    assert body["messages"] == []
    assert "upcoming_messages" not in body


def test_failing_registry_read_degrades_to_unknown_availability(client: TestClient):
    """An unreachable Redis must not turn the status page into a 500."""
    resp = _get(
        client,
        {},
        failing=["app.services.status_summary.registry_store.get_registry"],
        components=[("core", "i1")],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_at"] and body["window_days"] == 90

    [core] = body["aliases"]
    assert core["alias"] == "core"
    # Live state is unreadable: never claim "down" for a source we cannot see.
    assert core["availability"] == "unknown"
    assert core["uptime_pct"] is None
    assert all(point["samples"] == 0 for point in core["history"])

    [instance] = body["instances"]
    assert instance["availability"] == "unknown"


def test_failing_history_read_degrades_to_unknown_availability(client: TestClient):
    """An unreachable Postgres must not turn the public status page into a 500."""
    registry = {"core": [_entry("h1", "i1", "core")]}
    resp = _get(
        client,
        registry,
        failing=[
            "app.services.status_summary.state_history_db.list_components",
            "app.services.status_summary.state_history_db.availability_buckets",
        ],
    )

    assert resp.status_code == 200
    body = resp.json()

    [core] = body["aliases"]
    assert core["availability"] == "unknown"
    assert core["uptime_pct"] is None
    assert core["history"] == []
    assert [instance["instance_id"] for instance in body["instances"]] == ["i1"]
    assert body["instances"][0]["availability"] == "unknown"
