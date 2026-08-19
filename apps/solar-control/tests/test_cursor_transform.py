"""Ported deepseek-cursor-proxy transform tests (async-adapted).

Covers the reasoning-effort plumbing (the alias -> effort mechanism the
cursor proxy exists for), the upstream model rewrite, and the
reasoning_content repair/display pipeline against a fake redis store.
"""

import json
from unittest.mock import patch

import pytest

from app.cursor_proxy.config import ProxyConfig
from app.cursor_proxy.reasoning_store import ReasoningStore, conversation_scope
from app.cursor_proxy.transform import (
    normalize_reasoning_effort,
    prepare_upstream_request,
    reasoning_cache_namespace,
    rewrite_response_body,
    upstream_model_for,
)

pytestmark = pytest.mark.anyio


CONFIG = ProxyConfig(
    upstream_base_url="http://127.0.0.1:8015/v1",
    upstream_model="deepseek-v4-flash:284b",
    thinking="enabled",
    reasoning_effort="max",
)


class _FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key: str):
        return self.strings.get(key)

    async def set(self, key: str, value, ex=None):
        self.strings[key] = value

    async def zadd(self, name: str, mapping: dict[str, float]):
        self.zsets.setdefault(name, {}).update(mapping)

    async def zcard(self, name: str) -> int:
        return len(self.zsets.get(name, {}))

    async def zpopmin(self, name: str, count: int = 1):
        return []

    async def zrange(self, name: str, start: int, stop: int):
        return []

    async def delete(self, *keys: str) -> int:
        return 0


@pytest.fixture
def store():
    with patch(
        "app.cursor_proxy.reasoning_store.redis_client", return_value=_FakeRedis()
    ):
        yield ReasoningStore()


def test_effort_aliases_normalize_to_upstream_values():
    assert normalize_reasoning_effort("max") == "max"
    assert normalize_reasoning_effort("xhigh") == "max"
    assert normalize_reasoning_effort("high") == "high"
    assert normalize_reasoning_effort("medium") == "high"
    assert normalize_reasoning_effort("low") == "high"
    assert normalize_reasoning_effort("bogus") == "high"
    assert normalize_reasoning_effort(None) == "high"


def test_upstream_model_for_aliases():
    assert (
        upstream_model_for("deepseek-v4-flash:max", CONFIG) == "deepseek-v4-flash:284b"
    )
    assert upstream_model_for("krumpli:high", CONFIG) == "deepseek-v4-flash:284b"
    assert (
        upstream_model_for("deepseek-v4-flash:284b", CONFIG) == "deepseek-v4-flash:284b"
    )
    assert upstream_model_for("some-other-model", CONFIG) == "deepseek-v4-flash:284b"


async def test_prepare_rewrites_model_and_injects_effort(store):
    payload = {
        "model": "deepseek-v4-flash:max",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "max_completion_tokens": 500,
    }
    prepared = await prepare_upstream_request(
        payload, CONFIG, store, reasoning_effort="max"
    )
    assert prepared.upstream_model == "deepseek-v4-flash:284b"
    assert prepared.payload["model"] == "deepseek-v4-flash:284b"
    assert prepared.payload["thinking"] == {"type": "enabled"}
    assert prepared.payload["reasoning_effort"] == "max"
    # max_completion_tokens converted to max_tokens (upstream-compatible)
    assert prepared.payload["max_tokens"] == 500
    assert prepared.original_model == "deepseek-v4-flash:max"


async def test_prepare_honors_per_alias_effort(store):
    payload = {
        "model": "krumpli:high",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    high_prepared = await prepare_upstream_request(
        payload, CONFIG, store, reasoning_effort="high"
    )
    assert high_prepared.payload["reasoning_effort"] == "high"

    payload["model"] = "krumpli:max"
    max_prepared = await prepare_upstream_request(
        payload, CONFIG, store, reasoning_effort="max"
    )
    assert max_prepared.payload["reasoning_effort"] == "max"


async def test_prepare_repairs_missing_reasoning_from_store(store):
    prior = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Use the echo tool."},
    ]
    # The namespace prepare_upstream_request will compute for this request.
    ns = reasoning_cache_namespace(
        CONFIG, "deepseek-v4-flash:284b", {"type": "enabled"}, "max", None
    )
    # Record the original assistant turn WITH reasoning, as the upstream would.
    original = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "I will call echo with hi.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
            }
        ],
    }
    scope = conversation_scope(prior, ns)
    await store.store_assistant_message(original, scope, ns, prior)

    # Cursor sends the same turn back WITHOUT reasoning_content.
    from_cursor = [
        *prior,
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
        {"role": "user", "content": "Done?"},
    ]
    prepared = await prepare_upstream_request(
        {"model": "deepseek-v4-flash:max", "messages": from_cursor, "stream": False},
        CONFIG,
        store,
        reasoning_effort="max",
    )
    assert prepared.patched_reasoning_messages >= 1
    patched_assistant = next(
        message for message in prepared.payload["messages"] if message.get("tool_calls")
    )
    assert patched_assistant["reasoning_content"] == "I will call echo with hi."


async def test_rewrite_adds_details_block_and_renames_model(store):
    upstream_body = json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 123,
            "model": "deepseek-v4-flash-284b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hi",
                        "reasoning_content": "Say hi.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    ).encode()
    rewritten = await rewrite_response_body(
        upstream_body,
        "krumpli:max",
        store,
        [{"role": "user", "content": "Say hi"}],
        display_reasoning=True,
        collapsible_reasoning=True,
    )
    payload = json.loads(rewritten.decode())
    assert payload["model"] == "krumpli:max"
    content = payload["choices"][0]["message"]["content"]
    assert "<details>" in content
    assert "<summary>Thinking</summary>" in content
    assert "Say hi." in content
    assert content.endswith("Hi")


async def test_rewrite_records_reasoning_to_store(store):
    upstream_body = json.dumps(
        {
            "id": "chatcmpl-2",
            "object": "chat.completion",
            "created": 123,
            "model": "deepseek-v4-flash-284b",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Tool reasoning.",
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {
                                    "name": "echo",
                                    "arguments": '{"text": "x"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    ).encode()
    prior = [{"role": "user", "content": "Call echo."}]
    await rewrite_response_body(
        upstream_body,
        "deepseek-v4-flash:max",
        store,
        prior,
        display_reasoning=False,
    )
    probe = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_9",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "x"}'},
            }
        ],
    }
    restored = await store.lookup_for_message(probe, conversation_scope(prior, ""), "")
    assert restored == "Tool reasoning."
