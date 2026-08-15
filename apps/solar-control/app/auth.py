"""Authentication middleware for multi-tenant API.

Two authentication modes:
- /v1/* routes: API key looked up in the api_keys table (joined to its
  endpoint) -> sets request.state.endpoint + endpoint_id, so gateway
  handlers can filter models without another DB round-trip
- /api/* routes: compared against MANAGEMENT_API_KEY env var
"""

import asyncio
import json
import logging
from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.config import settings
from app.database.endpoints import endpoint_db

logger = logging.getLogger(__name__)

ENDPOINT_CACHE_PREFIX = "solar:endpoint_cache:"
ENDPOINT_CACHE_TTL = 300  # 5 minutes
# Throttle window for last_used_at stamps: 60s means at most one DB update
# per key per minute, even under heavy load.
TOUCH_PREFIX = "solar:last_touch:"
TOUCH_TTL = 60


def _extract_api_key(request: Request) -> str | None:
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


async def invalidate_endpoint_cache() -> None:
    """Clear all cached endpoint lookups across all replicas."""
    try:
        from app.redis_state.connection import redis_client

        r = redis_client()
        keys: list[bytes] = []
        async for key in r.scan_iter(f"{ENDPOINT_CACHE_PREFIX}*"):
            keys.append(key)
        if keys:
            await r.delete(*keys)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to invalidate endpoint cache: %s", e)


async def _resolve_endpoint(api_key: str) -> tuple[Any, str] | None:
    """Resolve a raw key to an (endpoint, api_key_id) pair via Redis cache.

    The cache entry is ``{"endpoint": {...}, "api_key_id": "..."}`` keyed by
    the same ``solar:endpoint_cache:{api_key}`` as before the S-045 split, so
    a rolling deploy keeps a single cache namespace.
    """
    try:
        from app.redis_state.connection import redis_client

        r = redis_client()
        cached = await r.get(f"{ENDPOINT_CACHE_PREFIX}{api_key}")
        if cached:
            data = json.loads(cached)
            from app.database.endpoints import ApiEndpoint

            return ApiEndpoint(**data["endpoint"]), data["api_key_id"]
    except Exception:  # noqa: BLE001, S110
        pass

    resolved = await endpoint_db.resolve_by_api_key(api_key)
    if resolved:
        endpoint, api_key_row = resolved
        try:
            from app.redis_state.connection import redis_client

            r = redis_client()
            await r.set(
                f"{ENDPOINT_CACHE_PREFIX}{api_key}",
                json.dumps(
                    {"endpoint": endpoint.model_dump(), "api_key_id": api_key_row.id}
                ),
                ex=ENDPOINT_CACHE_TTL,
            )
        except Exception:  # noqa: BLE001, S110
            pass
        return endpoint, api_key_row.id
    return None


async def _touch_last_used(api_key_id: str) -> None:
    """Fire-and-forget last_used_at stamp, throttled by a short Redis marker.

    The marker is set optimistically with SET NX + TTL: whichever replica
    wins writes within the throttle window; the rest skip.
    """
    try:
        from app.database.api_keys import api_key_db
        from app.redis_state.connection import redis_client

        r = redis_client()
        marker = f"{TOUCH_PREFIX}{api_key_id}"
        if not await r.set(marker, "1", ex=TOUCH_TTL, nx=True):
            return
        await api_key_db.touch_last_used(api_key_id)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to touch last_used_at for key %s", api_key_id)


async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Unified authentication middleware."""
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    public_paths = ["/health", "/ready", "/", "/docs", "/redoc", "/openapi.json"]
    if path in public_paths:
        return await call_next(request)

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
        resolved = await _resolve_endpoint(api_key)
        if resolved:
            endpoint, api_key_id = resolved
            request.state.endpoint = endpoint
            request.state.api_key_id = api_key_id
            request.state.endpoint_id = endpoint.id
            request.state.endpoint_name = endpoint.name
            asyncio.create_task(_touch_last_used(api_key_id))
            return await call_next(request)
        if api_key == settings.management_api_key:
            request.state.endpoint = None
            request.state.api_key_id = None
            request.state.endpoint_id = None
            request.state.endpoint_name = None
            return await call_next(request)
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=_UNAUTHORIZED_RESPONSE,
            headers=_CORS_HEADERS,
        )

    if path.startswith("/api/"):
        if api_key != settings.management_api_key:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content=_UNAUTHORIZED_RESPONSE,
                headers=_CORS_HEADERS,
            )
        request.state.endpoint = None
        request.state.endpoint_id = None
        return await call_next(request)

    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content=_UNAUTHORIZED_RESPONSE,
        headers=_CORS_HEADERS,
    )
