"""Redis-backed reasoning store tests (the /cursor/v1 cache).

The store is the replica-safety critical piece: solar-control is stateless
and runs with multiple replicas, so the reasoning_content cache must live
in shared Redis rather than a per-pod file. These tests exercise the real
store class against a fake async redis shim.
"""

from typing import Any
from unittest.mock import patch

import pytest

from app.cursor_proxy.reasoning_store import (
    INDEX_KEY,
    KEY_PREFIX,
    ReasoningStore,
    conversation_scope,
)


class _FakeRedis:
    """Minimal async redis shim supporting the store's surface."""

    def __init__(self) -> None:
        self.strings: dict[str, bytes | str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.strings.get(key)

    async def set(self, key: str, value, ex=None):
        self.strings[key] = value
        if ex is not None:
            self._ex = ex  # not enforced; TTL is Redis-native

    async def zadd(self, name: str, mapping: dict[str, float]):
        zset = self.zsets.setdefault(name, {})
        for member, score in mapping.items():
            zset[member] = score

    async def zcard(self, name: str) -> int:
        return len(self.zsets.get(name, {}))

    async def zpopmin(self, name: str, count: int = 1):
        zset = self.zsets.get(name, {})
        ordered = sorted(zset.items(), key=lambda item: (item[1], item[0]))
        popped = ordered[:count]
        for member, _ in popped:
            zset.pop(member, None)
        return popped

    async def zrange(self, name: str, start: int, stop: int):
        zset = self.zsets.get(name, {})
        ordered = sorted(zset.items(), key=lambda item: (item[1], item[0]))
        if stop == -1:
            return [member for member, _ in ordered[start:]]
        return [member for member, _ in ordered[start : stop + 1]]

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self.strings:
                del self.strings[key]
                count += 1
            if key in self.zsets:
                del self.zsets[key]
                count += 1
            if key in self.deleted:
                continue
        self.deleted.extend(keys)
        return count

    async def ping(self):
        return True


@pytest.fixture
def redis():
    fake = _FakeRedis()
    with patch("app.cursor_proxy.reasoning_store.redis_client", return_value=fake):
        yield fake


def _assistant_message(reasoning: str, tool_call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": reasoning,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
            }
        ],
    }


def _tool_messages():
    return [
        {"role": "user", "content": "Use the echo tool."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "hi"},
    ]


@pytest.mark.anyio
async def test_put_get_roundtrip(redis):
    store = ReasoningStore()
    await store.put("scope:abc:signature:def", "thinking text", {"role": "assistant"})
    assert await store.get("scope:abc:signature:def") == "thinking text"
    assert await store.get("missing") is None


@pytest.mark.anyio
async def test_put_ignores_non_string_reasoning(redis):
    store = ReasoningStore()
    bad: Any = ["not", "a", "string"]
    await store.put("scope:abc:signature:def", bad, {"role": "assistant"})
    assert await store.get("scope:abc:signature:def") is None


@pytest.mark.anyio
async def test_store_assistant_message_and_lookup(redis):
    store = ReasoningStore()
    messages = _tool_messages()
    scope = conversation_scope(messages[:-1], namespace="ns")
    stored = await store.store_assistant_message(
        _assistant_message("I will call echo."),
        scope,
        "ns",
        messages[:-2],
    )
    assert stored >= 3  # signature + tool_call + tool_call_signature + tool_name

    # A tool-call assistant message without reasoning finds it again.
    missing_reasoning = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
            }
        ],
    }
    restored = await store.lookup_for_message(
        missing_reasoning, scope, "ns", messages[:-2]
    )
    assert restored == "I will call echo."


@pytest.mark.anyio
async def test_reasoning_store_is_shared_across_store_instances(redis):
    """The Redis backend means a second store instance sees the same cache."""
    first = ReasoningStore(max_age_seconds=3600)
    second = ReasoningStore(max_age_seconds=3600)
    await first.put("k", "shared reasoning", {"role": "assistant"})
    assert await second.get("k") == "shared reasoning"


@pytest.mark.anyio
async def test_max_rows_prunes_oldest(redis):
    store = ReasoningStore(max_rows=2)
    await store.put("a", "1", {"role": "assistant"})
    await store.put("b", "2", {"role": "assistant"})
    await store.put("c", "3", {"role": "assistant"})
    assert await store.get("a") is None
    assert await store.get("b") == "2"
    assert await store.get("c") == "3"


@pytest.mark.anyio
async def test_clear_removes_everything(redis):
    store = ReasoningStore()
    await store.put("a", "1", {"role": "assistant"})
    await store.put("b", "2", {"role": "assistant"})
    cleared = await store.clear()
    assert cleared == 2
    assert await store.get("a") is None
    assert await store.get("b") is None
    assert redis.zsets.get(INDEX_KEY) in (None, {})


@pytest.mark.anyio
async def test_keys_are_prefixed(redis):
    store = ReasoningStore()
    await store.put("scope:x:signature:y", "v", {"role": "assistant"})
    assert f"{KEY_PREFIX}scope:x:signature:y" in redis.strings
