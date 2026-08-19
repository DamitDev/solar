"""Gateway-facing operations for the /cursor/v1 proxy endpoint.

The proxy is deliberately thin: it authenticates the caller's API key at
the /cursor/v1 hop (auth middleware), rewrites the request for DeepSeek
thinking-mode semantics, then performs a self-HTTP call back to
solar-control's own /v1 gateway with the SAME Authorization header the
client sent. Solar-control therefore resolves the key a second time on
the /v1 hop and attributes gateway telemetry to the caller's endpoint --
the proxy holds no credentials of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from app.cursor_proxy.aliases import (
    DEFAULT_MAX_MODEL_LEN,
    UPSTREAM_MODEL,
    alias_model_entries,
)
from app.cursor_proxy.config import ProxyConfig
from app.cursor_proxy.reasoning_store import ReasoningStore, conversation_scope
from app.cursor_proxy.streaming import (
    CursorReasoningDisplayAdapter,
    StreamAccumulator,
)
from app.cursor_proxy.transform import (
    PreparedRequest,
    prepare_upstream_request,
    rewrite_response_body,
)

logger = logging.getLogger("app.cursor_proxy")


class CursorProxyUpstreamError(Exception):
    """Non-2xx response from the self-HTTP /v1 call, passed through as-is."""

    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        super().__init__(f"upstream {status}")
        self.status = status
        self.body = body
        self.content_type = content_type


_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def ensure_session() -> aiohttp.ClientSession:
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession()
        return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _store_for(config: ProxyConfig) -> ReasoningStore:
    return ReasoningStore(
        max_age_seconds=config.reasoning_cache_max_age_seconds,
        max_rows=config.reasoning_cache_max_rows,
    )


def _upstream_headers(authorization: str, stream: bool) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "solar-cursor-proxy",
    }
    if authorization:
        # Forward the caller's credential verbatim; the /v1 hop re-resolves
        # it against the api_keys table (Redis-cached) for scoping/telemetry.
        headers["Authorization"] = authorization
    return headers


def _upstream_url(config: ProxyConfig) -> str:
    return f"{config.upstream_base_url}/chat/completions"


async def get_models_data(config: ProxyConfig) -> list[dict[str, Any]]:
    """Advertise the four cursor aliases with the upstream model's context.

    Context comes from the live registry entry for the upstream model when
    available (1M on the served flash instance); falls back to the static
    default otherwise.
    """
    max_model_len = DEFAULT_MAX_MODEL_LEN
    try:
        from app.redis_state import registry_store

        registry = await registry_store.get_registry()
        instances = registry.get(UPSTREAM_MODEL) or []
        if instances:
            context_size = instances[0].context_size
            if isinstance(context_size, int) and context_size > 0:
                max_model_len = context_size
    except Exception:  # noqa: BLE001 - best-effort, static default is fine
        logger.debug(
            "registry lookup for %s failed; using default context", UPSTREAM_MODEL
        )
    return alias_model_entries(max_model_len)


async def _make_prepared(
    payload: dict[str, Any],
    config: ProxyConfig,
    store: ReasoningStore,
    authorization: str,
    reasoning_effort: str | None = None,
) -> PreparedRequest:
    return await prepare_upstream_request(
        payload,
        config,
        store,
        authorization=authorization or None,
        reasoning_effort=reasoning_effort,
    )


async def proxy_non_stream(
    payload: dict[str, Any],
    config: ProxyConfig,
    authorization: str,
    original_model: str,
    reasoning_effort: str | None = None,
) -> tuple[int, str, bytes]:
    """Non-streaming proxy: prepare -> self-HTTP /v1 -> rewrite -> respond."""
    store = _store_for(config)
    prepared = await _make_prepared(
        payload, config, store, authorization, reasoning_effort
    )
    session = await ensure_session()
    timeout = aiohttp.ClientTimeout(total=config.request_timeout)
    async with session.post(
        _upstream_url(config),
        headers=_upstream_headers(authorization or "", stream=False),
        data=json.dumps(prepared.payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    ) as response:
        body = await response.read()
        content_type = response.headers.get("Content-Type", "application/json")
        if response.status != 200:
            raise CursorProxyUpstreamError(response.status, body, content_type)
        try:
            rewritten = await rewrite_response_body(
                body,
                original_model,
                store,
                prepared.record_response_messages,
                prepared.cache_namespace,
                content_prefix=prepared.recovery_notice,
                scope=prepared.record_response_scope,
                prior_messages=prepared.record_response_messages,
                recording_contexts=prepared.record_response_contexts,
                display_reasoning=config.display_reasoning,
                collapsible_reasoning=config.collapsible_reasoning,
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("failed to rewrite upstream JSON response", exc_info=True)
            rewritten = body
        return response.status, content_type, rewritten


async def proxy_stream(
    payload: dict[str, Any],
    config: ProxyConfig,
    authorization: str,
    original_model: str,
    reasoning_effort: str | None = None,
) -> AsyncIterator[bytes]:
    """Streaming proxy mirror of the upstream deepseek-cursor-proxy handler.

    Yields rewritten SSE bytes. Non-2xx upstream responses raise
    CursorProxyUpstreamError so the router can emit a plain JSON error.
    """
    store = _store_for(config)
    prepared = await _make_prepared(
        payload, config, store, authorization, reasoning_effort
    )
    session = await ensure_session()
    timeout = aiohttp.ClientTimeout(total=config.request_timeout)

    async with session.post(
        _upstream_url(config),
        headers=_upstream_headers(authorization or "", stream=True),
        data=json.dumps(prepared.payload, ensure_ascii=False).encode("utf-8"),
        timeout=timeout,
    ) as response:
        if response.status != 200:
            body = await response.read()
            content_type = response.headers.get("Content-Type", "application/json")
            raise CursorProxyUpstreamError(response.status, body, content_type)

        accumulator = StreamAccumulator()
        display_adapter = (
            CursorReasoningDisplayAdapter(config.collapsible_reasoning)
            if config.display_reasoning
            else None
        )
        scope = (
            prepared.record_response_scope
            if prepared.record_response_scope is not None
            else conversation_scope(
                prepared.record_response_messages, prepared.cache_namespace
            )
        )
        response_prior_messages = prepared.record_response_messages
        response_contexts = (
            prepared.record_response_contexts
            if prepared.record_response_contexts
            else [(scope, response_prior_messages)]
        )
        finalized = False
        try:
            while True:
                line = await response.content.readline()
                if not line:
                    break
                rewritten, finalized = await _rewrite_sse_line(
                    line,
                    original_model,
                    accumulator,
                    store,
                    prepared.cache_namespace,
                    response_contexts,
                    display_adapter,
                )
                yield rewritten
                if finalized:
                    break
        finally:
            if not finalized:
                for ctx_scope, prior_messages in response_contexts:
                    await accumulator.store_reasoning(
                        store, ctx_scope, prepared.cache_namespace, prior_messages
                    )


async def _rewrite_sse_line(
    line: bytes,
    original_model: str,
    accumulator: StreamAccumulator,
    store: ReasoningStore,
    cache_namespace: str,
    response_contexts: list[tuple[str, list[dict[str, Any]]]],
    display_adapter: CursorReasoningDisplayAdapter | None,
) -> tuple[bytes, bool]:
    stripped = line.strip()
    if not stripped.startswith(b"data:"):
        return line, False

    data = stripped[len(b"data:") :].strip()
    if data == b"[DONE]":
        for scope, prior_messages in response_contexts:
            await accumulator.store_reasoning(
                store, scope, cache_namespace, prior_messages
            )
        prefix = b""
        if display_adapter is not None:
            closing_chunk = display_adapter.flush_chunk(original_model)
            if closing_chunk is not None:
                prefix += _sse_data(closing_chunk)
        return prefix + b"data: [DONE]\n\n", True

    try:
        chunk = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line, False

    if isinstance(chunk, dict):
        accumulator.ingest_chunk(chunk)
        for scope, prior_messages in response_contexts:
            await accumulator.store_ready_reasoning(
                store, scope, cache_namespace, prior_messages
            )
        if display_adapter is not None:
            display_adapter.rewrite_chunk(chunk)
        if "model" in chunk:
            chunk["model"] = original_model
        ending = b"\r\n" if line.endswith(b"\r\n") else b"\n"
        return (
            _sse_data(chunk, ending),
            False,
        )
    return line, False


def _sse_data(payload: dict[str, Any], ending: bytes = b"\n\n") -> bytes:
    return (
        b"data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + ending
    )
