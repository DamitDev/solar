"""Tests for per-endpoint model scoping on /v1 routes (S-045).

The auth middleware resolves a key to (endpoint, key_id); handlers then
thread the endpoint's patterns into the gateway. A scoped key must not
leak aliases in /v1/models and must not route to well-a- denied model,
not even through a prefix abbreviation ("sec" for "secret-7b").
"""

import pytest
from fastapi import HTTPException

from app.database.endpoints import ApiEndpoint
from app.routes.openai import _model_patterns, _raise_model_not_found


class FakeState:
    def __init__(self, endpoint=None):
        self.endpoint = endpoint
        self.endpoint_id = getattr(endpoint, "id", None) if endpoint else None
        self.endpoint_name = getattr(endpoint, "name", None) if endpoint else None


class FakeRequest:
    def __init__(self, state):
        self.state = state


def _scoped(*patterns: str) -> ApiEndpoint:
    return ApiEndpoint(
        id="ep-scoped",
        name="scoped",
        serve_all_models=False,
        model_patterns=list(patterns),
    )


def _unrestricted() -> ApiEndpoint:
    return ApiEndpoint(id="ep-open", name="open", serve_all_models=True)


# ── _model_patterns ────────────────────────────────────────────────


def test_patterns_unrestricted_for_serve_all():
    req = FakeRequest(FakeState(_unrestricted()))
    assert _model_patterns(req) is None


def test_patterns_none_for_management_key():
    req = FakeRequest(FakeState(None))
    assert _model_patterns(req) is None


def test_patterns_returned_for_scoped_endpoint():
    req = FakeRequest(FakeState(_scoped("iris-*")))
    assert _model_patterns(req) == ["iris-*"]


def test_patterns_empty_list_for_scoped_with_no_patterns():
    req = FakeRequest(FakeState(ApiEndpoint(id="e", name="e", serve_all_models=False)))
    assert _model_patterns(req) == []


# ── 404 shape ──────────────────────────────────────────────────────


def test_model_not_found_shape():
    with pytest.raises(HTTPException) as exc:
        _raise_model_not_found("sec")
    assert exc.value.status_code == 404
    body = exc.value.detail
    assert body["code"] == "model_not_found"
    assert body["type"] == "invalid_request_error"
    assert "'sec'" in body["message"]


# ── end / gateway filtering is exercised via the service layer ─────


@pytest.mark.anyio
async def test_gateway_resolution_respects_scope():
    """The resolver closes the prefix-match bypass: a partial name of a
    denied alias must not resolve through the filtered registry."""
    from unittest.mock import patch

    from app.gateway import gateway
    from app.redis_state import registry_store
    from app.services.model_access import filter_aliases_for_patterns

    registry = {
        "iris-osl:8b": ["inst1"],
        "secret-7b": ["inst2"],
    }

    async def fake_registry():
        return dict(registry)

    with patch.object(registry_store, "get_registry", fake_registry):
        allowed = set(filter_aliases_for_patterns(["iris-*"], registry))
        assert "iris-osl:8b" in allowed
        assert "secret-7b" not in allowed
        # Full-model alias: scoped patterns reject the secret directly.
        resolved = await gateway._resolve_model_name("secret-7b", ["iris-*"])
        assert resolved is None
        # Prefix "sec" would resolve to secret-7b without scope; with scope
        # the registry is filtered *before* any prefix matching, so None.
        resolved = await gateway._resolve_model_name("sec", ["iris-*"])
        assert resolved is None
        # The allowed alias still resolves, including by prefix.
        assert await gateway._resolve_model_name("iris", ["iris-*"]) == ("iris-osl:8b")
