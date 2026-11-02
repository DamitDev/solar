"""Tests that the OpenAI routes use the resolved client IP."""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from app.models import ChatCompletionRequest, ChatMessage
from app.routes import openai as openai_routes


def _request(peer: str = "10.42.1.2", forwarded: str = "203.0.113.5") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(b"x-forwarded-for", forwarded.encode())],
        "client": (peer, 1234),
        "query_string": b"",
    }
    return Request(scope)


@pytest.mark.anyio
async def test_chat_uses_the_resolved_client_ip():
    route = AsyncMock(return_value="ok")
    request = ChatCompletionRequest(
        model="wind",
        messages=[ChatMessage(role="user", content="hi")],
    )
    with patch.object(openai_routes.gateway, "route_request", route):
        await openai_routes.chat_completions(request, _request())
    assert route.await_args is not None
    assert route.await_args.args[3] == "203.0.113.5"


@pytest.mark.anyio
async def test_chat_keeps_the_peer_for_an_untrusted_caller():
    route = AsyncMock(return_value="ok")
    request = ChatCompletionRequest(
        model="wind",
        messages=[ChatMessage(role="user", content="hi")],
    )
    with patch.object(openai_routes.gateway, "route_request", route):
        await openai_routes.chat_completions(request, _request(peer="198.51.100.9"))
    assert route.await_args is not None
    assert route.await_args.args[3] == "198.51.100.9"
