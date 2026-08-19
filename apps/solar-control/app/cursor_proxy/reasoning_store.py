"""Conversation-scoped reasoning_content cache for the /cursor/v1 proxy.

The scope/key derivation functions are ported verbatim from
deepseek-cursor-proxy's ``reasoning_store.py`` (MIT). They compute stable
cache keys from message content so the proxy can restore DeepSeek's
multi-round ``reasoning_content`` chains that Cursor never sends back.

The SQLite backend is replaced with Redis so the cache is shared across
solar-control replicas (a per-pod file would miss on the replica that did
not record the previous turn). Values are plain strings with a TTL; a
sorted set tracks insertion order for the max-rows cap.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from app.redis_state.connection import redis_client

KEY_PREFIX = "solar:cursor:reasoning:"
INDEX_KEY = f"{KEY_PREFIX}index"


def normalize_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
    function = tool_call.get("function") or {}
    if not isinstance(function, dict):
        function = {}

    arguments = function.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    normalized: dict[str, Any] = {
        "id": tool_call.get("id"),
        "type": tool_call.get("type") or "function",
        "function": {
            "name": function.get("name") or "",
            "arguments": arguments,
        },
    }
    return normalized


def tool_call_signature(tool_call: dict[str, Any]) -> str:
    normalized = normalize_tool_call(tool_call)
    normalized.pop("id", None)
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def tool_call_ids(message: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for tool_call in message.get("tool_calls") or []:
        if isinstance(tool_call, dict) and tool_call.get("id"):
            ids.append(str(tool_call["id"]))
    return ids


def tool_call_names(message: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    return names


def message_signature(message: dict[str, Any]) -> str:
    tool_calls = [
        normalize_tool_call(tool_call)
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    ]
    payload = {
        "content": message.get("content") or "",
        "tool_calls": tool_calls,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_scope_message(message: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {"role": message.get("role")}
    for key in ("content", "name", "tool_call_id", "prefix"):
        if key in message:
            canonical[key] = message[key]
    if message.get("tool_calls"):
        canonical["tool_calls"] = [
            normalize_tool_call(tool_call)
            for tool_call in message.get("tool_calls") or []
            if isinstance(tool_call, dict)
        ]
    return canonical


def conversation_scope(messages: list[dict[str, Any]], namespace: str = "") -> str:
    scope_messages = [canonical_scope_message(message) for message in messages]
    payload: Any = scope_messages
    if namespace:
        payload = {"namespace": namespace, "messages": scope_messages}
    return _sha256_json(payload)


def turn_context_signature(prior_messages: list[dict[str, Any]]) -> str:
    last_user_index = next(
        (
            index
            for index in range(len(prior_messages) - 1, -1, -1)
            if prior_messages[index].get("role") == "user"
        ),
        -1,
    )
    start_index = 0
    if last_user_index != -1:
        start_index = last_user_index
        while start_index > 0 and prior_messages[start_index - 1].get("role") == "user":
            start_index -= 1

    context_messages = [
        canonical_scope_message(message)
        for message in prior_messages[start_index:]
        if message.get("role") != "system"
    ]
    return _sha256_json(context_messages)


def scoped_reasoning_keys(message: dict[str, Any], scope: str) -> list[str]:
    keys = [f"scope:{scope}:signature:{message_signature(message)}"]
    keys.extend(
        f"scope:{scope}:tool_call:{tool_call_id}"
        for tool_call_id in tool_call_ids(message)
    )
    keys.extend(
        f"scope:{scope}:tool_call_signature:{tool_call_signature(tool_call)}"
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    )
    # Recovery-of-last-resort key. Catches the case where a streaming response
    # was interrupted (user pressed Stop) before the tool_call.id chunk arrived,
    # so neither tool_call_id nor tool_call_signature (which canonicalizes
    # arguments) survives the round-trip through Cursor's transcript.
    keys.extend(
        f"scope:{scope}:tool_name:{tool_name}" for tool_name in tool_call_names(message)
    )
    return keys


def portable_reasoning_keys(
    message: dict[str, Any],
    cache_namespace: str,
    prior_messages: list[dict[str, Any]],
) -> list[str]:
    if not cache_namespace:
        return []

    turn_signature = turn_context_signature(prior_messages)
    keys = [
        (
            f"namespace:{cache_namespace}:turn:{turn_signature}:"
            f"signature:{message_signature(message)}"
        )
    ]
    keys.extend(
        (
            f"namespace:{cache_namespace}:turn:{turn_signature}:"
            f"tool_call:{tool_call_id}"
        )
        for tool_call_id in tool_call_ids(message)
    )
    keys.extend(
        (
            f"namespace:{cache_namespace}:turn:{turn_signature}:"
            f"tool_call_signature:{tool_call_signature(tool_call)}"
        )
        for tool_call in (message.get("tool_calls") or [])
        if isinstance(tool_call, dict)
    )
    keys.extend(
        (f"namespace:{cache_namespace}:turn:{turn_signature}:" f"tool_name:{tool_name}")
        for tool_name in tool_call_names(message)
    )
    return keys


class ReasoningStore:
    """Redis-backed reasoning_content cache (async; shared across replicas)."""

    def __init__(
        self,
        max_age_seconds: int | None = None,
        max_rows: int | None = None,
    ) -> None:
        self.max_age_seconds = max_age_seconds
        self.max_rows = max_rows

    async def _raw_key(self, key: str) -> str:
        return f"{KEY_PREFIX}{key}"

    async def put(self, key: str, reasoning: str, message: dict[str, Any]) -> None:
        if not isinstance(reasoning, str):
            return
        r = redis_client()
        raw_key = await self._raw_key(key)
        now = time.time()
        await r.set(
            raw_key,
            reasoning,
            ex=self.max_age_seconds if self.max_age_seconds else None,
        )
        await r.zadd(INDEX_KEY, {raw_key: now})
        await self._prune_locked()

    async def get(self, key: str) -> str | None:
        r = redis_client()
        raw = await r.get(await self._raw_key(key))
        if raw is None:
            return None
        return str(raw)

    async def store_assistant_message(
        self,
        message: dict[str, Any],
        scope: str,
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
    ) -> int:
        if message.get("role") != "assistant":
            return 0
        reasoning = message.get("reasoning_content")
        if not isinstance(reasoning, str):
            return 0

        keys = scoped_reasoning_keys(message, scope)
        if prior_messages is not None:
            keys.extend(
                portable_reasoning_keys(message, cache_namespace, prior_messages)
            )
        keys = list(dict.fromkeys(keys))
        for key in keys:
            await self.put(key, reasoning, message)
        return len(keys)

    async def lookup_for_message(
        self,
        message: dict[str, Any],
        scope: str,
        cache_namespace: str = "",
        prior_messages: list[dict[str, Any]] | None = None,
    ) -> str | None:
        keys = scoped_reasoning_keys(message, scope)
        if prior_messages is not None:
            keys.extend(
                portable_reasoning_keys(message, cache_namespace, prior_messages)
            )
        for key in keys:
            reasoning = await self.get(key)
            if reasoning is not None:
                return reasoning
        return None

    async def backfill_portable_aliases(
        self,
        message: dict[str, Any],
        reasoning: str,
        cache_namespace: str,
        prior_messages: list[dict[str, Any]],
    ) -> int:
        if not isinstance(reasoning, str):
            return 0
        keys = portable_reasoning_keys(message, cache_namespace, prior_messages)
        if not keys:
            return 0
        message_with_reasoning = dict(message)
        message_with_reasoning["reasoning_content"] = reasoning
        for key in dict.fromkeys(keys):
            await self.put(key, reasoning, message_with_reasoning)
        return len(keys)

    async def clear(self) -> int:
        r = redis_client()
        members = await r.zrange(INDEX_KEY, 0, -1)
        if members:
            await r.delete(*members)
        count = len(members)
        await r.delete(INDEX_KEY)
        return count

    async def prune(self) -> int:
        return await self._prune_locked()

    async def _prune_locked(self) -> int:
        if not self.max_rows:
            return 0
        r = redis_client()
        size = await r.zcard(INDEX_KEY)
        if size <= self.max_rows:
            return 0
        overflow = size - self.max_rows
        removed = await r.zpopmin(INDEX_KEY, count=overflow)
        if removed:
            await r.delete(*[member for member, _ in removed])
        return len(removed)
