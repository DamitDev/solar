"""Authentication middleware for multi-tenant API.

Two authentication modes:
- /v1/* routes: API key looked up in api_endpoints table -> returns endpoint_id
- /api/* routes: compared against MANAGEMENT_API_KEY env var
"""

from typing import Optional, Tuple

from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.database.endpoints import endpoint_db, ApiEndpoint


def _extract_api_key(request: Request) -> Optional[str]:
    key = request.headers.get("X-API-Key")
    if key:
        return key
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Bearer "):
        return auth[7:]
    return None


_UNAUTHORIZED_RESPONSE = {
    "error": {
        "message": "Incorrect API key provided.",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key",
    }
}

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Methods": "*",
    "Access-Control-Allow-Headers": "*",
}


# Cache for endpoint API key lookups to avoid DB round-trip on every request.
# Invalidated when endpoints are created/updated/deleted.
_endpoint_cache: dict[str, ApiEndpoint] = {}


def invalidate_endpoint_cache() -> None:
    _endpoint_cache.clear()


async def _resolve_endpoint(api_key: str) -> Optional[ApiEndpoint]:
    if api_key in _endpoint_cache:
        return _endpoint_cache[api_key]
    ep = await endpoint_db.get_endpoint_by_api_key(api_key)
    if ep:
        _endpoint_cache[api_key] = ep
    return ep


async def auth_middleware(request: Request, call_next):
    """Unified authentication middleware."""
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    public_paths = ["/health", "/ready", "/", "/docs", "/redoc", "/openapi.json"]
    if path in public_paths:
        return await call_next(request)

    # Socket.IO handles its own auth via handshake
    if path.startswith("/socket.io"):
        return await call_next(request)

    api_key = _extract_api_key(request)
    if not api_key:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_UNAUTHORIZED_RESPONSE,
            headers=_CORS_HEADERS,
        )

    if path.startswith("/v1/"):
        # OpenAI endpoint: resolve API key to endpoint
        endpoint = await _resolve_endpoint(api_key)
        if not endpoint:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content=_UNAUTHORIZED_RESPONSE,
                headers=_CORS_HEADERS,
            )
        # Store endpoint info in request state for downstream use
        request.state.endpoint_id = endpoint.id
        request.state.endpoint_name = endpoint.name
        return await call_next(request)

    if path.startswith("/api/"):
        # Management API: compare against management key
        if api_key != settings.management_api_key:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content=_UNAUTHORIZED_RESPONSE,
                headers=_CORS_HEADERS,
            )
        request.state.endpoint_id = None
        return await call_next(request)

    # Unknown path - reject
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content=_UNAUTHORIZED_RESPONSE,
        headers=_CORS_HEADERS,
    )
