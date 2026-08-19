"""Auth middleware behaviour for /cursor/v1.

S-059: the cursor proxy is tenant-facing — only api_keys-table credentials
are accepted, the management key is rejected. The management key keeps
working on /v1 for operator tooling.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import JSONResponse, Response
from starlette.requests import Request

from app.auth import auth_middleware

pytestmark = pytest.mark.anyio


def _request(path: str, auth_header: bytes | None = None) -> Request:
    headers = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    return Request(scope)


async def _call(path: str, *, api_key: str | None = None, resolve=None):
    auth = None
    if api_key is not None:
        auth = f"Bearer {api_key}".encode()
    request = _request(path, auth)

    async def _next(request):
        return Response("ok")

    with patch("app.auth.settings.management_api_key", "mgmt-key"):
        if resolve is not None:
            with patch("app.auth._resolve_endpoint", new=resolve):
                return await auth_middleware(request, _next)
        return await auth_middleware(request, _next)


def _resolve_ok(*args, **kwargs):
    endpoint = type(
        "Endpoint",
        (),
        {"id": "ep-1", "name": "ep", "serve_all_models": True, "model_patterns": []},
    )()
    return AsyncMock(return_value=(endpoint, "key-1"))(*args, **kwargs)


async def test_cursor_requires_a_key():
    response = await _call("/cursor/v1/models", api_key=None)
    assert response.status_code == 401


async def test_cursor_rejects_management_key():
    resolve = AsyncMock(return_value=None)
    response = await _call(
        "/cursor/v1/chat/completions", api_key="mgmt-key", resolve=resolve
    )
    assert response.status_code == 401


async def test_cursor_accepts_endpoint_key():
    response = await _call("/cursor/v1/models", api_key="sk-user", resolve=_resolve_ok)
    assert response.status_code == 200


async def test_v1_still_accepts_management_key():
    resolve = AsyncMock(return_value=None)
    response = await _call("/v1/models", api_key="mgmt-key", resolve=resolve)
    assert response.status_code == 200


async def test_cursor_invalid_key_is_401():
    resolve = AsyncMock(return_value=None)
    response = await _call("/cursor/v1/models", api_key="sk-bad", resolve=resolve)
    assert response.status_code == 401
    assert isinstance(response, JSONResponse)
