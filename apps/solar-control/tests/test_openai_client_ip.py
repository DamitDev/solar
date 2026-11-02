"""Tests that every OpenAI route uses the resolved client IP."""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from app.models import (
    ChatCompletionRequest,
    ChatMessage,
    ClassifyRequest,
    CompletionRequest,
    EmbeddingRequest,
    RerankRequest,
)
from app.routes import openai as openai_routes

CASES = [
    (
        openai_routes.chat_completions,
        ChatCompletionRequest,
        {"model": "wind", "messages": [ChatMessage(role="user", content="hi")]},
    ),
    (
        openai_routes.completions,
        CompletionRequest,
        {"model": "wind", "prompt": "hi"},
    ),
    (
        openai_routes.classify,
        ClassifyRequest,
        {"model": "wind", "input": "hi"},
    ),
    (
        openai_routes.embeddings,
        EmbeddingRequest,
        {"model": "wind", "input": "hi"},
    ),
    (
        openai_routes.rerank,
        RerankRequest,
        {"model": "wind", "query": "hi", "documents": ["a"]},
    ),
]
IDS = ["chat", "completions", "classify", "embeddings", "rerank"]


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


@pytest.mark.parametrize(("handler", "model_cls", "payload"), CASES, ids=IDS)
@pytest.mark.anyio
async def test_handler_uses_the_resolved_client_ip(handler, model_cls, payload):
    route = AsyncMock(return_value="ok")
    with patch.object(openai_routes.gateway, "route_request", route):
        await handler(model_cls(**payload), _request())
    assert route.await_args is not None
    assert route.await_args.args[3] == "203.0.113.5"


@pytest.mark.parametrize(("handler", "model_cls", "payload"), CASES, ids=IDS)
@pytest.mark.anyio
async def test_handler_keeps_the_peer_for_an_untrusted_caller(
    handler, model_cls, payload
):
    route = AsyncMock(return_value="ok")
    with patch.object(openai_routes.gateway, "route_request", route):
        await handler(model_cls(**payload), _request(peer="198.51.100.9"))
    assert route.await_args is not None
    assert route.await_args.args[3] == "198.51.100.9"
