"""Router tests for /cursor/v1 (model listing, chat completions, errors)."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

pytestmark = pytest.mark.anyio

# The cursor router needs auth (api_keys); shortcut tests over the ASGI app
# with the auth resolution mocked to a valid endpoint.
VALID_REQUEST_STATE = patch(
    "app.auth._resolve_endpoint",
    new=AsyncMock(
        return_value=(
            type(
                "Endpoint",
                (),
                {
                    "id": "ep-cursor",
                    "name": "cursor",
                    "serve_all_models": True,
                    "model_patterns": [],
                },
            )(),
            "key-1",
        )
    ),
)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _noop_auth():
    """Authenticate every request as a valid endpoint key."""
    with VALID_REQUEST_STATE:
        yield


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer sk-test"}


async def test_models_lists_four_aliases(client):
    with patch(
        "app.routes.cursor.get_models_data",
        new=AsyncMock(
            return_value=[
                {"id": "deepseek-v4-flash:max", "object": "model"},
                {"id": "deepseek-v4-flash:high", "object": "model"},
                {"id": "krumpli:max", "object": "model"},
                {"id": "krumpli:high", "object": "model"},
            ]
        ),
    ):
        response = await client.get("/cursor/v1/models", headers=_auth())
    assert response.status_code == 200
    data = response.json()["data"]
    assert [m["id"] for m in data] == [
        "deepseek-v4-flash:max",
        "deepseek-v4-flash:high",
        "krumpli:max",
        "krumpli:high",
    ]


async def test_chat_completions_rejects_unknown_model(client):
    response = await client.post(
        "/cursor/v1/chat/completions",
        headers=_auth(),
        json={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "model_not_found"


async def test_chat_completions_non_stream(client):
    upstream = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "krumpli:max",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }
        ],
    }
    with patch(
        "app.routes.cursor.proxy_non_stream",
        new=AsyncMock(
            return_value=(200, "application/json", json.dumps(upstream).encode())
        ),
    ) as mock_proxy:
        response = await client.post(
            "/cursor/v1/chat/completions",
            headers=_auth(),
            json={
                "model": "krumpli:max",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
    assert response.status_code == 200
    assert response.json()["model"] == "krumpli:max"
    # The alias effort must reach the service call.
    assert mock_proxy.await_args.kwargs["reasoning_effort"] == "max"


async def test_chat_completions_passes_alias_effort(client):
    with patch(
        "app.routes.cursor.proxy_non_stream",
        new=AsyncMock(return_value=(200, "application/json", b"{}")),
    ) as mock_proxy:
        await client.post(
            "/cursor/v1/chat/completions",
            headers=_auth(),
            json={
                "model": "deepseek-v4-flash:high",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
    assert mock_proxy.await_args.kwargs["reasoning_effort"] == "high"


async def test_chat_completions_stream(client):
    frames = [
        b'data: {"id":"1","object":"chat.completion.chunk","model":"krumpli:high","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def _fake_stream(*args, **kwargs):
        for frame in frames:
            yield frame

    with patch("app.routes.cursor.proxy_stream", new=_fake_stream):
        response = await client.post(
            "/cursor/v1/chat/completions",
            headers=_auth(),
            json={
                "model": "krumpli:high",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"data: [DONE]" in response.content


async def test_chat_completions_upstream_error_passthrough(client):
    from app.cursor_proxy.service import CursorProxyUpstreamError

    error_body = json.dumps(
        {
            "error": {
                "message": "rate limited",
                "type": "rate_limit_error",
                "code": "rate_limit_error",
            }
        }
    ).encode()
    with patch(
        "app.routes.cursor.proxy_non_stream",
        new=AsyncMock(
            side_effect=CursorProxyUpstreamError(429, error_body, "application/json")
        ),
    ):
        response = await client.post(
            "/cursor/v1/chat/completions",
            headers=_auth(),
            json={
                "model": "deepseek-v4-flash:max",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_error"


async def test_chat_completions_body_too_large(client, monkeypatch):
    import app.cursor_proxy.config as cursor_config

    monkeypatch.setattr(cursor_config.settings, "cursor_max_request_body_bytes", 64)
    response = await client.post(
        "/cursor/v1/chat/completions",
        headers=_auth(),
        json={
            "model": "deepseek-v4-flash:max",
            "messages": [{"role": "user", "content": "x" * 200}],
            "stream": False,
        },
    )
    assert response.status_code == 413


async def test_chat_completions_invalid_json(client):
    response = await client.post(
        "/cursor/v1/chat/completions",
        headers={**_auth(), "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert response.status_code == 400
