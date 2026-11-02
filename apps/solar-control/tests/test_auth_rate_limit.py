"""Auth middleware rate-limit behaviour on /v1."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import Response
from starlette.requests import Request

from app.auth import auth_middleware
from app.config import settings

pytestmark = pytest.mark.anyio


def _request(
    path: str,
    api_key: str | None,
    *,
    internal: bool = False,
    peer: str = "127.0.0.1",
) -> Request:
    headers = []
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode()))
    if internal:
        headers.append((b"x-solar-internal", b"1"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (peer, 12345),
        "server": ("test", 80),
    }
    return Request(scope)


def _resolve_ok(*args, **kwargs):
    endpoint = type(
        "Endpoint",
        (),
        {"id": "ep-1", "name": "ep", "serve_all_models": True, "model_patterns": []},
    )()
    return AsyncMock(return_value=(endpoint, "key-1", "default"))(*args, **kwargs)


async def _call(api_key: str, hit, *, path: str = "/v1/models", internal: bool = False):
    request = _request(path, api_key, internal=internal)

    async def _next(request):
        return Response("ok")

    with (
        patch("app.auth.settings.management_api_key", "mgmt-key"),
        patch("app.auth._resolve_endpoint", new=_resolve_ok),
        patch("app.auth.rate_limit_store.hit", hit),
    ):
        return await auth_middleware(request, _next)


async def test_over_limit_returns_429_with_retry_after():
    hit = AsyncMock(return_value=(False, 30))
    response = await _call("sk-user", hit)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert json.loads(response.body)["error"]["code"] == "rate_limit_exceeded"


async def test_under_limit_passes():
    hit = AsyncMock(return_value=(True, 0))
    response = await _call("sk-user", hit)
    assert response.status_code == 200
    hit.assert_awaited_once_with("key-1", settings.rate_limit_rpm)


async def test_cursor_proxy_is_limited_too():
    hit = AsyncMock(return_value=(False, 30))
    response = await _call("sk-user", hit, path="/cursor/v1/chat/completions")
    assert response.status_code == 429
    assert hit.await_count == 1


async def test_internal_self_call_is_exempt():
    hit = AsyncMock(return_value=(False, 30))
    response = await _call("sk-user", hit, internal=True)
    assert response.status_code == 200
    hit.assert_not_awaited()


async def test_internal_header_from_a_non_loopback_peer_is_still_limited():
    hit = AsyncMock(return_value=(False, 30))
    request = _request("/v1/models", "sk-user", internal=True, peer="10.42.1.2")

    async def _next(request):
        return Response("ok")

    with (
        patch("app.auth.settings.management_api_key", "mgmt-key"),
        patch("app.auth._resolve_endpoint", new=_resolve_ok),
        patch("app.auth.rate_limit_store.hit", hit),
    ):
        response = await auth_middleware(request, _next)

    assert response.status_code == 429
    hit.assert_awaited_once()


async def test_management_key_is_exempt():
    hit = AsyncMock(return_value=(False, 30))

    async def _resolve_none(*_args, **_kwargs):
        return None

    request = _request("/v1/models", "mgmt-key")

    async def _next(request):
        return Response("ok")

    with (
        patch("app.auth.settings.management_api_key", "mgmt-key"),
        patch("app.auth._resolve_endpoint", new=_resolve_none),
        patch("app.auth.rate_limit_store.hit", hit),
    ):
        response = await auth_middleware(request, _next)

    assert response.status_code == 200
    hit.assert_not_awaited()


async def test_disabled_limiter_does_not_count():
    hit = AsyncMock(return_value=(False, 30))
    request = _request("/v1/models", "sk-user")

    async def _next(request):
        return Response("ok")

    with (
        patch("app.auth.settings.management_api_key", "mgmt-key"),
        patch("app.auth.settings.rate_limit_enabled", False),
        patch("app.auth._resolve_endpoint", new=_resolve_ok),
        patch("app.auth.rate_limit_store.hit", hit),
    ):
        response = await auth_middleware(request, _next)

    assert response.status_code == 200
    hit.assert_not_awaited()


async def test_redis_failure_fails_open():
    hit = AsyncMock(side_effect=RuntimeError("redis down"))
    response = await _call("sk-user", hit)
    assert response.status_code == 200
