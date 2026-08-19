"""Service tests for the /cursor/v1 self-HTTP proxy path.

Verifies the critical S-059 invariant: the proxy holds no credentials of
its own — the caller's Authorization header must be forwarded verbatim on
the self-HTTP /v1 call, so solar-control re-resolves it for scoping and
telemetry.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.cursor_proxy.config import ProxyConfig
from app.cursor_proxy.service import (
    CursorProxyUpstreamError,
    proxy_non_stream,
    proxy_stream,
)

pytestmark = pytest.mark.anyio

CONFIG = ProxyConfig(
    upstream_base_url="http://127.0.0.1:8015/v1",
    request_timeout=30.0,
)


class _FakeContent:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self._index = 0

    async def readline(self) -> bytes:
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        lines: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}
        self.content = _FakeContent(lines or [])

    async def read(self) -> bytes:
        return self._body


class _ResponseCM:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_kwargs: dict[str, Any] = {}
        self.closed = False

    def post(self, *args: Any, **kwargs: Any) -> _ResponseCM:
        self.last_kwargs = kwargs
        return _ResponseCM(self.response)


@pytest.fixture
def fake_redis():
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

    with patch(
        "app.cursor_proxy.reasoning_store.redis_client",
        return_value=_FakeRedis(),
    ):
        yield


def _payload(model: str = "krumpli:max") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Say hi"}],
        "stream": False,
    }


async def test_non_stream_forwards_caller_key_and_rewrites(fake_redis):
    upstream_body = json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
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
        }
    ).encode()
    session = _FakeSession(_FakeResponse(status=200, body=upstream_body))
    with patch(
        "app.cursor_proxy.service.ensure_session", new=AsyncMock(return_value=session)
    ):
        status, content_type, body = await proxy_non_stream(
            _payload(),
            CONFIG,
            authorization="Bearer sk-user-key",
            original_model="krumpli:max",
            reasoning_effort="max",
        )
    assert status == 200
    assert content_type == "application/json"
    payload = json.loads(body.decode())
    assert payload["model"] == "krumpli:max"
    # The upstream call used the caller's key, not a proxy own credential.
    sent_headers = session.last_kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer sk-user-key"
    # The upstream payload carries the alias's model + effort.
    sent_data = json.loads(session.last_kwargs["data"])
    assert sent_data["model"] == "deepseek-v4-flash:284b"
    assert sent_data["reasoning_effort"] == "max"
    assert sent_data["thinking"] == {"type": "enabled"}


async def test_non_stream_upstream_error_raises(fake_redis):
    error_body = json.dumps({"error": {"message": "nope"}}).encode()
    session = _FakeSession(_FakeResponse(status=404, body=error_body))
    with (
        patch(
            "app.cursor_proxy.service.ensure_session",
            new=AsyncMock(return_value=session),
        ),
        pytest.raises(CursorProxyUpstreamError) as exc_info,
    ):
        await proxy_non_stream(
            _payload(),
            CONFIG,
            authorization="Bearer sk-user-key",
            original_model="krumpli:max",
        )
    assert exc_info.value.status == 404
    assert exc_info.value.body == error_body


async def test_stream_rewrites_sse_and_closes_blocks(fake_redis):
    stream_lines = [
        b'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"deepseek-v4-flash-284b","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"deepseek-v4-flash-284b","choices":[{"index":0,"delta":{"reasoning_content":"Think"},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"deepseek-v4-flash-284b","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n',
        b'data: {"id":"1","object":"chat.completion.chunk","created":2,"model":"deepseek-v4-flash-284b","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    session = _FakeSession(_FakeResponse(status=200, lines=stream_lines))
    payload = _payload()
    payload["stream"] = True
    with patch(
        "app.cursor_proxy.service.ensure_session", new=AsyncMock(return_value=session)
    ):
        chunks = [
            chunk
            async for chunk in proxy_stream(
                payload,
                CONFIG,
                authorization="Bearer sk-user-key",
                original_model="krumpli:max",
                reasoning_effort="max",
            )
        ]
    joined = b"".join(chunks)
    assert b"<details>" in joined
    assert b"<summary>Thinking</summary>" in joined
    assert b"Think" in joined
    # Chunks are renamed back to the cursor-facing alias.
    assert b'"model":"krumpli:max"' in joined
    assert b"data: [DONE]" in joined
    # The upstream request headers carried the caller's key.
    sent_headers = session.last_kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer sk-user-key"
    assert sent_headers["Accept"] == "text/event-stream"
