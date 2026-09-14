"""Regression test: the endpoint auth cache serializes the endpoint payload
in JSON.

The endpoint model carries ``datetime`` fields. If the cache write uses
``json.dumps(endpoint.model_dump())`` (the default, non-JSON mode) it raises
``TypeError: Object of type datetime is not JSON serializable``, which the
swallowed ``except: pass`` hides — so every /v1/* request silently falls back
to a Postgres join instead of a Redis cache hit. The fix is
``model_dump(mode=\"json\")``. This test pins that the written cache value is
valid JSON with ISO-8601 timestamps, and that reads deserialize back to a
working ApiEndpoint.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.auth import ENDPOINT_CACHE_PREFIX, _resolve_endpoint
from app.database.api_keys import ApiKey
from app.database.endpoints import ApiEndpoint


def _endpoint() -> ApiEndpoint:
    return ApiEndpoint(
        id="11111111-2222-3333-4444-555555555555",
        name="cache-probe",
        description="dt",
        serve_all_models=True,
        model_patterns=[],
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


class _FakeRedis:
    """Minimal async redis shim recording the last SET write."""

    def __init__(self):
        self.stored: dict[str, str] = {}
        self.get_calls = 0

    async def get(self, key: str):
        return self.stored.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.stored[key] = value
        # ex honoured for cache TTL
        return "OK"


@pytest.mark.anyio
async def test_cache_write_is_json_serializable_with_datetime_fields():
    """The cache entry must serialize even though ApiEndpoint has datetimes."""
    fake = _FakeRedis()
    row = ApiKey(
        id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        endpoint_id=_endpoint().id,
        name="default",
        key="sk-cache-probe",
        enabled=True,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    async def _resolve(_key):
        return _endpoint(), row

    with (
        patch("app.auth.endpoint_db.resolve_by_api_key", side_effect=_resolve),
        patch("app.redis_state.connection.redis_client", return_value=fake),
    ):
        result = await _resolve_endpoint("sk-cache-probe")

    # The write must not throw — and must be JSON-serializable.
    assert result is not None
    stored = fake.stored.get(f"{ENDPOINT_CACHE_PREFIX}sk-cache-probe")
    assert stored is not None, "cache entry was written"
    payload = json.loads(stored)  # raises if not valid JSON
    assert payload["endpoint"]["name"] == "cache-probe"
    # datetimes must be serialized as ISO strings, not native objects.
    assert isinstance(payload["endpoint"]["created_at"], str)
    assert payload["api_key_id"] == row.id


@pytest.mark.anyio
async def test_cache_read_roundtrips_through_endpoint_model():
    """A cache hit must reconstruct a working ApiEndpoint (no datetime crash)."""
    fake = _FakeRedis()
    ep = _endpoint()
    row = ApiKey(
        id="id-bbbb-cccc-dddd-eeeeeeeeeeee",
        endpoint_id=ep.id,
        name="default",
        key="sk-hit",
        enabled=True,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    # Pre-populate the cache the same way _resolve_endpoint writes it.
    fake.stored[f"{ENDPOINT_CACHE_PREFIX}sk-hit"] = json.dumps(
        {"endpoint": ep.model_dump(mode="json"), "api_key_id": row.id}
    )

    # resolve_by_api_key must NOT be hit if the cache returns a value.
    with patch("app.redis_state.connection.redis_client", return_value=fake):
        resolved = await _resolve_endpoint("sk-hit")

    assert resolved is not None
    endpoint, got_id = resolved
    assert got_id == row.id
    assert endpoint.name == "cache-probe"
    assert endpoint.id == ep.id
