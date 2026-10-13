"""Auth middleware behaviour for /metrics (management key required).

/metrics was public; it now requires the management key so pool-health
gauges are not exposed to anonymous scrapers. /health stays public.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import Response
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
        "method": "GET",
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
    return AsyncMock(return_value=(endpoint, "key-1", "default"))(*args, **kwargs)


async def test_metrics_requires_a_key():
    response = await _call("/metrics", api_key=None)
    assert response.status_code == 401


async def test_metrics_rejects_endpoint_key():
    response = await _call("/metrics", api_key="sk-user", resolve=_resolve_ok)
    assert response.status_code == 401


async def test_metrics_accepts_management_key():
    response = await _call("/metrics", api_key="mgmt-key")
    assert response.status_code == 200


async def test_health_stays_public():
    response = await _call("/health", api_key=None)
    assert response.status_code == 200
