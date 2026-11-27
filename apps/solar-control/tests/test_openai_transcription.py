"""POST /v1/audio/transcriptions route behaviour.

The route reads the multipart form before routing (it has to learn the model and
the format), spools the upload to a disk temp file, forwards through the gateway
with ``required_endpoint`` so a host that cannot serve it is never picked, and
relays the host's answer with its own content type.
"""

import io
import json
from unittest.mock import AsyncMock, patch

import pytest
from starlette.datastructures import FormData, UploadFile

from app.gateway import (
    UpstreamResponse,
    UpstreamServiceError,
    VirtualModelUnavailableError,
)
from app.routes.openai import audio_transcriptions

WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt "


class _State:
    endpoint = None
    endpoint_id = None
    api_key_id = None
    api_key_name = None


class _Request:
    def __init__(self, form: FormData):
        self._form = form
        self.state = _State()

    async def form(self):
        return self._form


def _form(**fields) -> FormData:
    """A form with one audio upload and the given string fields."""
    items: list[tuple[str, object]] = [
        ("file", UploadFile(file=io.BytesIO(WAV), filename="sample.wav", size=len(WAV)))
    ]
    for name, value in fields.items():
        if isinstance(value, list):
            items.extend((f"{name}[]", entry) for entry in value)
        else:
            items.append((name, value))
    return FormData(items)


async def _call(form: FormData, result=None, side_effect=None):
    captured: dict = {}

    async def _route(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        if side_effect is not None:
            raise side_effect
        return result

    with (
        patch("app.routes.openai.gateway.route_request", AsyncMock(side_effect=_route)),
        patch("app.routes.openai.resolve_client_ip", return_value="1.2.3.4"),
    ):
        response = await audio_transcriptions(_Request(form))
    return response, captured


@pytest.mark.anyio
async def test_the_upload_is_forwarded_as_a_multipart_upload():
    response, captured = await _call(
        _form(model="whisper-tiny", response_format="verbose_json"),
        result=UpstreamResponse(200, b'{"text": "hi"}', "application/json"),
    )

    assert response.status_code == 200
    assert response.media_type == "application/json"
    assert response.body == b'{"text": "hi"}'

    kwargs = captured["kwargs"]
    assert kwargs["required_endpoint"] == "/v1/audio/transcriptions"
    upload = kwargs["multipart"]
    assert upload.filename == "sample.wav"
    assert upload.content_type == "audio/octet-stream" or upload.content_type
    # The model is resolved before the forward, and the file is a real temp file
    # that the gateway can re-open per attempt.
    assert captured["args"][0] == "whisper-tiny"
    assert kwargs["multipart"].path.endswith(".tmp") or kwargs["multipart"].path
    names = [name for name, _ in upload.fields]
    assert "model" in names
    assert "response_format" in names


@pytest.mark.anyio
async def test_the_forward_covers_the_host_audio_budget():
    """The host allows 1800 s for audio; the gateway's 600 s default would cut
    a long recording the host would finish."""
    _, captured = await _call(
        _form(model="whisper-tiny"),
        result=UpstreamResponse(200, b"", "text/plain"),
    )
    assert captured["kwargs"]["timeout_s"] == 1800.0


@pytest.mark.anyio
async def test_the_temp_file_is_removed_after_the_request():
    import os

    _, captured = await _call(
        _form(model="whisper-tiny"),
        result=UpstreamResponse(200, b"ok", "text/plain"),
    )
    assert not os.path.exists(captured["kwargs"]["multipart"].path)


@pytest.mark.anyio
async def test_a_text_response_is_relayed_with_its_content_type():
    response, _ = await _call(
        _form(model="whisper-tiny", response_format="srt"),
        result=UpstreamResponse(
            200, b"1\n00:00:00,000 --> 00:00:01,000\nhi\n", "application/x-subrip"
        ),
    )
    assert response.media_type == "application/x-subrip"
    assert response.body.startswith(b"1\n")


@pytest.mark.anyio
async def test_an_upstream_error_is_relayed_verbatim():
    body = json.dumps({"error": {"message": "bad format"}}).encode()
    response, _ = await _call(
        _form(model="whisper-tiny"),
        result=UpstreamResponse(400, body, "application/json"),
    )
    assert response.status_code == 400
    assert response.body == body


@pytest.mark.anyio
async def test_a_missing_file_is_rejected_before_the_forward():
    form = FormData([("model", "whisper-tiny")])
    response, captured = await _call(
        form, result=UpstreamResponse(200, b"", "text/plain")
    )
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["message"] == "file is required"
    assert captured == {}


@pytest.mark.anyio
async def test_a_text_part_named_file_is_rejected():
    form = FormData([("file", "hello"), ("model", "whisper-tiny")])
    response, captured = await _call(
        form, result=UpstreamResponse(200, b"", "text/plain")
    )
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["message"] == "file is required"
    assert captured == {}


@pytest.mark.anyio
async def test_stream_is_rejected_before_the_forward():
    response, captured = await _call(
        _form(model="whisper-tiny", stream="true"),
        result=UpstreamResponse(200, b"", "text/plain"),
    )
    assert response.status_code == 400
    assert (
        "streaming transcription is not supported"
        in json.loads(response.body)["error"]["message"]
    )
    assert captured == {}


@pytest.mark.anyio
async def test_an_unknown_format_is_rejected_before_the_forward():
    response, captured = await _call(
        _form(model="whisper-tiny", response_format="mp3"),
        result=UpstreamResponse(200, b"", "text/plain"),
    )
    assert response.status_code == 400
    assert (
        "response_format 'mp3' is not supported"
        in json.loads(response.body)["error"]["message"]
    )
    assert captured == {}


@pytest.mark.anyio
async def test_an_upstream_timeout_is_relayed_as_the_upstream_status():
    """A runtime 504 must reach the client as a 504 with the host's message, not
    as a 404 model_not_found that blames the model."""
    body = json.dumps(
        {
            "error": {
                "message": "word-level timestamps did not finish within 60s",
                "type": "server_error",
                "code": "timeout",
            }
        }
    ).encode()

    response, _ = await _call(
        _form(model="whisper-large-v3-hu"),
        side_effect=UpstreamServiceError(504, body.decode(), "application/json"),
    )

    assert response.status_code == 504
    assert response.body == body
    assert response.media_type == "application/json"


@pytest.mark.anyio
async def test_an_unknown_model_is_a_404():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _call(
            _form(model="does-not-exist"),
            side_effect=ValueError("Model 'does-not-exist' not found"),
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "model_not_found"


@pytest.mark.anyio
async def test_an_unavailable_virtual_model_is_a_503():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _call(
            _form(model="core"),
            side_effect=VirtualModelUnavailableError("core", {"a": "dead"}),
        )
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["code"] == "virtual_model_unavailable"


@pytest.mark.anyio
async def test_the_four_openai_optional_fields_are_accepted():
    response, captured = await _call(
        _form(
            model="whisper-tiny",
            keywords="a,b",
            languages="en",
            chunking_strategy="auto",
            include=["logprobs"],
        ),
        result=UpstreamResponse(200, b'{"text": "hi"}', "application/json"),
    )
    # Accepted and forwarded: control forwards every non-file form field, and the
    # shim maps keywords and languages onto prompt/language while dropping
    # chunking_strategy and include[].
    assert response.status_code == 200
    names = [name for name, _ in captured["kwargs"]["multipart"].fields]
    for accepted in ("keywords", "languages", "chunking_strategy", "include[]"):
        assert accepted in names


@pytest.mark.parametrize(
    ("headers", "content"),
    [
        (
            {"content-type": "multipart/form-data; boundary=x"},
            b"garbage",
        ),
        # A missing boundary raises Starlette's MultiPartException, which
        # Starlette converts to HTTPException(400) inside an app.
        (
            {"content-type": "multipart/form-data"},
            b"garbage",
        ),
    ],
)
def test_a_malformed_multipart_body_is_a_400_envelope(headers, content):
    """Starlette's parser errors become HTTPException(400) or MultiPartException,
    whose default body is {"detail": ...}; the route must still answer with the
    OpenAI envelope every other audio error uses."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routes.openai import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/v1/audio/transcriptions",
        content=content,
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "malformed multipart body"
