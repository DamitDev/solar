"""Proxy configuration for the /cursor/v1 endpoint.

Fields mirror the upstream deepseek-cursor-proxy ``ProxyConfig`` so the
ported transform/streaming modules stay close to their source. Values come
from solar-control's env-driven ``app.config.settings``; the webui-managed
row (app.database.cursor_proxy.CursorProxySettings) supplies display flags
and the upstream API key id (the key itself is fetched by the router).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024
REASONING_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
REASONING_CACHE_MAX_ROWS = 100_000


@dataclass(frozen=True)
class ProxyConfig:
    """Transport-level proxy knobs, all env-configurable."""

    upstream_base_url: str = "http://127.0.0.1:8015/v1"
    upstream_model: str = "deepseek-v4-flash:284b"
    thinking: str = "enabled"
    reasoning_effort: str = "max"
    request_timeout: float = 300.0
    max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES
    missing_reasoning_strategy: str = "recover"
    reasoning_cache_max_age_seconds: int = REASONING_CACHE_MAX_AGE_SECONDS
    reasoning_cache_max_rows: int = REASONING_CACHE_MAX_ROWS
    display_reasoning: bool = True
    collapsible_reasoning: bool = True


def proxy_config_from_settings(
    *,
    display_reasoning: bool | None = None,
    collapsible_reasoning: bool | None = None,
) -> ProxyConfig:
    """Build the request-time proxy config from env + the settings row.

    Only the display flags are webui-managed today; everything else reads
    from ``app.config.settings`` so operators can tune them via env vars
    (or the .env file on local dev).
    """
    return ProxyConfig(
        upstream_base_url=settings.cursor_upstream_base_url.rstrip("/"),
        upstream_model=settings.cursor_upstream_model,
        thinking=settings.cursor_thinking,
        reasoning_effort=settings.cursor_reasoning_effort,
        request_timeout=settings.cursor_request_timeout_s,
        max_request_body_bytes=settings.cursor_max_request_body_bytes,
        missing_reasoning_strategy=settings.cursor_missing_reasoning_strategy,
        reasoning_cache_max_age_seconds=settings.cursor_reasoning_cache_max_age_s,
        reasoning_cache_max_rows=settings.cursor_reasoning_cache_max_rows,
        display_reasoning=(
            display_reasoning
            if display_reasoning is not None
            else settings.cursor_display_reasoning
        ),
        collapsible_reasoning=(
            collapsible_reasoning
            if collapsible_reasoning is not None
            else settings.cursor_collapsible_reasoning
        ),
    )
