"""Unit test: the _emit_error cap keeps the raised error full (IT Sec #97).

An upstream 400 whose body carries the canary plus 1000 characters of
padding flows through ``OpenAIGateway._emit_error``. The persisted and
broadcast record is trimmed at ``_MAX_UPSTREAM_ERROR_CHARS`` (200) — the
``ValueError`` raised for the API caller still carries the full text, so
``HTTPException(detail=str(e))`` keeps returning the whole upstream error.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import _MAX_UPSTREAM_ERROR_CHARS, OpenAIGateway
from app.models import RegistryEntry, WSMessageType

CANARY = "canary-ITSEC-97-solar-prompt-fragment"
_TAIL = "tail-of-the-upstream-body-" * 30


class _Response:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _RequestContext:
    def __init__(self, response: _Response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Session:
    closed = False

    def __init__(self, response: _Response):
        self._response = response

    def post(self, *args, **kwargs):
        return _RequestContext(self._response)


@pytest.mark.anyio
async def test_emit_error_caps_the_record_and_keeps_the_raised_error_full():
    """The persisted/broadcast error_message is trimmed to 200 characters and
    loses the tail of the upstream body; the raised ValueError carries the
    full upstream text."""
    gateway = OpenAIGateway()
    gateway.session = _Session(_Response(400, f"{CANARY} {_TAIL}"))
    emitted: list[str] = []

    async def _capture(event, **kwargs):
        if event["type"] == WSMessageType.REQUEST_ERROR:
            emitted.append(event["data"]["error_message"])

    instance = RegistryEntry(
        host_id="host-1",
        instance_id="inst-1",
        url="http://upstream:8000",
        api_key="upstream-key",
        model_alias="test-model",
    )

    with (
        patch.object(gateway, "_broadcast_routing_event", side_effect=_capture),
        patch.object(
            gateway, "_find_instance_or_retry", new=AsyncMock(return_value=instance)
        ),
        patch("app.gateway.host_db.get_host", new=AsyncMock(return_value=None)),
        patch("app.gateway.routing_store.increment_active", new=AsyncMock()),
        patch("app.gateway.routing_store.increment_host_active", new=AsyncMock()),
        patch("app.gateway.routing_store.decrement_active", new=AsyncMock()),
        patch("app.gateway.routing_store.decrement_host_active", new=AsyncMock()),
        patch("app.gateway.routing_store.add_weight", new=AsyncMock()),
        pytest.raises(ValueError) as excinfo,
    ):
        await gateway.route_request(
            "test-model",
            "/v1/chat/completions",
            {"model": "test-model", "prompt": "hello"},
        )

    full_error = f"Request failed: 400 - {CANARY} {_TAIL}"
    assert str(excinfo.value) == full_error
    assert len(full_error) > _MAX_UPSTREAM_ERROR_CHARS + len(CANARY)

    # The ValueError path emits twice — the upstream branch's terminal
    # _emit_error, then the outer except-Exception replay with str(e).
    # Both copies are capped at 200 and both lose the padding tail.
    assert emitted == [
        full_error[:_MAX_UPSTREAM_ERROR_CHARS],
        full_error[:_MAX_UPSTREAM_ERROR_CHARS],
    ]
    assert all(_TAIL not in message for message in emitted)
