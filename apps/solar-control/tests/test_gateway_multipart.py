"""Multipart forwarding in the gateway.

A transcription request carries a binary audio file, so it cannot be forwarded as
JSON. The gateway streams it from a disk temp file, opens a fresh handle per
attempt (aiohttp does not close a user-supplied file after a successful post but
does close it after a connection error), relays the answer verbatim, and never runs
the token-usage machinery on it.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import (
    MultipartUpload,
    OpenAIGateway,
    UpstreamResponse,
    UpstreamServiceError,
)
from app.models import Host, HostStatus, RegistryEntry

ALIAS = "whisper-tiny"


class _Response:
    def __init__(self, status: int, body: bytes, content_type: str):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    async def read(self):
        return self._body

    async def text(self):
        return self._body.decode()

    async def json(self):
        return json.loads(self._body)


class _RequestContext:
    def __init__(self, response: _Response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingSession:
    """Captures the forwarded multipart and the file bytes it carried."""

    closed = False

    def __init__(self, responses):
        self._responses = list(responses)
        self.forms: list[object] = []
        self.posted: list[dict] = []
        self.headers: list[dict] = []
        self.sent_bytes: list[bytes] = []

    def post(self, url, *args, **kwargs):
        self.posted.append(kwargs)
        self.headers.append(kwargs.get("headers") or {})
        form = kwargs.get("data")
        self.forms.append(form)
        # Read the bytes while the gateway's handle is still open; the gateway
        # closes it per attempt, so a later read would fail.
        self.sent_bytes.append(_form_bytes(form) if form is not None else b"")
        response = self._responses.pop(0)
        return _RequestContext(response)


class _TimeoutSession:
    """Every post times out at the gateway."""

    closed = False

    def __init__(self):
        self.posted: list[dict] = []

    def post(self, url, *args, **kwargs):
        self.posted.append(kwargs)
        raise asyncio.TimeoutError


def _entry(**overrides) -> RegistryEntry:
    return RegistryEntry(
        **{
            "host_id": "host-1",
            "instance_id": "inst-1",
            "url": "http://test-host:3500",
            "api_key": "instance-api-key",
            "model_alias": ALIAS,
            "backend_type": "whispercpp",
            "served_model_name": ALIAS,
            **overrides,
        }
    )


def _host() -> Host:
    return Host(
        id="host-1",
        name="host-1",
        url="http://test-host:9000",
        api_key="host-key",
        status=HostStatus.ONLINE,
        supported_backends=["whispercpp"],
    )


def _upload(path: str, fields: list[tuple[str, str]]) -> MultipartUpload:
    return MultipartUpload(
        fields=fields,
        path=path,
        filename="sample.wav",
        content_type="audio/wav",
    )


def _form_fields(form) -> list[tuple[str, object]]:
    """The (name, value) pairs aiohttp will send."""
    return [(headers["name"], value) for headers, _extra, value in form._fields]


def _form_bytes(form) -> bytes:
    """Read the file payload out of the built multipart form."""
    for name, value in _form_fields(form):
        if name == "file":
            handle = getattr(value, "_value", value)
            handle.seek(0)
            data = handle.read()
            handle.seek(0)
            return data
    return b""


async def _route(
    gateway,
    session,
    upload,
    endpoint="/v1/audio/transcriptions",
    timeout_s=None,
):
    gateway.session = session
    emitted: dict = {}

    async def _success(
        request_id, model, instance, duration, usage_fields, endpoint_id, **kwargs
    ):
        emitted["usage"] = usage_fields

    route_kwargs: dict = {
        "model": ALIAS,
        "endpoint": endpoint,
        "data": {"model": ALIAS},
        "required_endpoint": "/v1/audio/transcriptions",
        "multipart": upload,
    }
    if timeout_s is not None:
        route_kwargs["timeout_s"] = timeout_s

    with (
        patch.object(gateway, "_ensure_session", AsyncMock()),
        patch.object(gateway, "_broadcast_routing_event", AsyncMock()),
        patch.object(
            gateway, "_find_instance_or_retry", AsyncMock(return_value=_entry())
        ),
        patch.object(gateway, "_emit_success", AsyncMock(side_effect=_success)),
        patch.object(gateway, "_emit_reroute", AsyncMock()),
        patch.object(gateway, "_emit_error", AsyncMock()),
        patch.object(
            gateway,
            "_fetch_last_generation_metrics",
            AsyncMock(side_effect=AssertionError("must not run for audio")),
        ),
        patch("app.gateway.host_db.get_host", AsyncMock(return_value=_host())),
        patch("app.gateway.health_store.mark_healthy", AsyncMock()),
        patch("app.gateway.health_store.mark_failed", AsyncMock()),
        patch("app.gateway.routing_store", AsyncMock()),
    ):
        result = await gateway.route_request(**route_kwargs)
    return result, emitted


@pytest.mark.anyio
async def test_multipart_is_posted_as_data_not_json(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFFdata")
    session = _RecordingSession([_Response(200, b'{"text": "hi"}', "application/json")])

    result, emitted = await _route(
        OpenAIGateway(),
        session,
        _upload(str(audio), [("model", ALIAS), ("response_format", "json")]),
    )

    assert isinstance(result, UpstreamResponse)
    assert result.status_code == 200
    assert result.body == b'{"text": "hi"}'
    assert result.content_type == "application/json"
    assert session.posted[0].get("json") is None
    assert session.posted[0]["data"] is session.forms[0]
    assert emitted["usage"] == {}


@pytest.mark.anyio
async def test_the_file_bytes_are_streamed_from_the_temp_file(tmp_path):
    audio = tmp_path / "sample.wav"
    payload = b"RIFF" + b"\x00" * 4096
    audio.write_bytes(payload)
    session = _RecordingSession([_Response(200, b"ok", "text/plain")])

    await _route(
        OpenAIGateway(),
        session,
        _upload(str(audio), [("model", ALIAS)]),
    )

    assert session.sent_bytes[0] == payload


@pytest.mark.anyio
async def test_a_retry_opens_a_fresh_handle(tmp_path):
    """aiohttp closes a file handle after a connection error, so the second
    attempt must open its own handle instead of reusing a closed one."""
    audio = tmp_path / "sample.wav"
    payload = b"RIFF" + b"\x01" * 2048
    audio.write_bytes(payload)
    session = _RecordingSession(
        [
            _Response(503, b"busy", "text/plain"),
            _Response(200, b"ok", "text/plain"),
        ]
    )

    result, _ = await _route(
        OpenAIGateway(),
        session,
        _upload(str(audio), [("model", ALIAS)]),
    )

    assert isinstance(result, UpstreamResponse)
    assert result.status_code == 200
    assert len(session.posted) == 2
    assert session.sent_bytes[0] == payload
    assert session.sent_bytes[1] == payload


@pytest.mark.anyio
async def test_the_model_is_rewritten_to_the_served_name(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFFdata")
    session = _RecordingSession([_Response(200, b"{}", "application/json")])

    await _route(
        OpenAIGateway(),
        session,
        _upload(
            str(audio),
            [("model", "whisper"), ("response_format", "json")],
        ),
    )

    names = [name for name, _ in _form_fields(session.forms[0])]
    assert "model" in names
    # whisper.cpp always serves the alias; a partial name would 404 at the shim.
    model_field = next(
        value for name, value in _form_fields(session.forms[0]) if name == "model"
    )
    assert model_field == ALIAS


@pytest.mark.anyio
async def test_a_non_retryable_status_is_relayed_verbatim(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFFdata")
    body = b'{"error": {"message": "bad format"}}'
    session = _RecordingSession([_Response(400, body, "application/json")])

    result, _ = await _route(
        OpenAIGateway(),
        session,
        _upload(str(audio), [("model", ALIAS)]),
    )

    assert isinstance(result, UpstreamResponse)
    assert result.status_code == 400
    assert result.body == body


@pytest.mark.anyio
async def test_a_retryable_upstream_error_keeps_the_status_and_body(tmp_path):
    """A 504 from the runtime must not become the route's 404 model_not_found."""
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFFdata")
    body = (
        b'{"error": {"message": "word-level timestamps did not finish within '
        b'60s", "type": "server_error", "code": "timeout"}}'
    )
    session = _RecordingSession([_Response(504, body, "application/json")] * 3)

    with pytest.raises(UpstreamServiceError) as excinfo:
        await _route(
            OpenAIGateway(),
            session,
            _upload(str(audio), [("model", ALIAS)]),
        )

    assert excinfo.value.status_code == 504
    assert excinfo.value.body == body.decode()
    assert excinfo.value.content_type == "application/json"


@pytest.mark.anyio
async def test_an_explicit_timeout_covers_the_host_audio_budget(tmp_path):
    """The route's 1800 s budget must replace the gateway's 600 s default, or
    a long recording the host would finish is cut off at the gateway."""
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFFdata")
    session = _RecordingSession([_Response(200, b"ok", "text/plain")])

    await _route(
        OpenAIGateway(),
        session,
        _upload(str(audio), [("model", ALIAS)]),
        timeout_s=1800.0,
    )

    assert session.posted[0]["timeout"].total == 1800.0


@pytest.mark.anyio
async def test_a_gateway_timeout_is_a_504_not_model_not_found(tmp_path):
    """A host that never answers must reach the route as a 504 timeout, not as
    the 404 model_not_found the plain ValueError funnel answers."""
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFFdata")
    session = _TimeoutSession()

    with pytest.raises(UpstreamServiceError) as excinfo:
        await _route(OpenAIGateway(), session, _upload(str(audio), [("model", ALIAS)]))

    assert excinfo.value.status_code == 504
    payload = json.loads(excinfo.value.body)
    assert payload["error"]["code"] == "timeout"
    assert payload["error"]["type"] == "server_error"
    assert "did not answer" in payload["error"]["message"]


@pytest.mark.anyio
async def test_no_instance_still_raises_the_plain_model_not_found_value_error():
    """No instance available must keep the 404 model_not_found funnel."""
    gateway = OpenAIGateway()
    gateway.session = _RecordingSession([])

    with (
        patch.object(gateway, "_ensure_session", AsyncMock()),
        patch.object(gateway, "_broadcast_routing_event", AsyncMock()),
        patch.object(gateway, "_emit_error", AsyncMock()),
        patch.object(gateway, "_find_instance_or_retry", AsyncMock(return_value=None)),
        pytest.raises(ValueError) as excinfo,
    ):
        await gateway.route_request(
            model=ALIAS,
            endpoint="/v1/audio/transcriptions",
            data={"model": ALIAS},
            required_endpoint="/v1/audio/transcriptions",
        )

    assert not isinstance(excinfo.value, UpstreamServiceError)
    assert "no instances available" in str(excinfo.value)


@pytest.mark.anyio
async def test_a_whisper_json_request_posts_json_and_skips_usage_fill():
    """The JSON path is unchanged; an audio backend never reports a cache
    split, so the host usage fallback is never fetched for it."""
    session = _RecordingSession(
        [_Response(200, b'{"usage": {"prompt_tokens": 1}}', "application/json")]
    )
    gateway = OpenAIGateway()
    gateway.session = session
    fetched = {}

    async def _fetch(*args, **kwargs):
        fetched["called"] = True
        return {}

    with (
        patch.object(gateway, "_ensure_session", AsyncMock()),
        patch.object(gateway, "_broadcast_routing_event", AsyncMock()),
        patch.object(
            gateway, "_find_instance_or_retry", AsyncMock(return_value=_entry())
        ),
        patch.object(gateway, "_emit_success", AsyncMock()),
        patch.object(
            gateway, "_fetch_last_generation_metrics", AsyncMock(side_effect=_fetch)
        ),
        patch("app.gateway.host_db.get_host", AsyncMock(return_value=_host())),
        patch("app.gateway.health_store.mark_healthy", AsyncMock()),
        patch("app.gateway.routing_store", AsyncMock()),
    ):
        result = await gateway.route_request(
            model=ALIAS,
            endpoint="/v1/chat/completions",
            data={"model": ALIAS},
        )

    assert result == {"usage": {"prompt_tokens": 1}}
    assert session.posted[0]["json"] == {"model": ALIAS}
    assert fetched == {}
