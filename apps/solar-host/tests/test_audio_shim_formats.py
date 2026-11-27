"""The audio shim's OpenAI contract: formats, refusals and the format matrix."""

import io
import json
import struct
import wave

import pytest
from starlette.testclient import TestClient

import solar_host.servers.audio_server as shim


def _wav_bytes(rate: int = 16000, channels: int = 1, sampwidth: int = 2) -> bytes:
    """A real RIFF/WAVE file with a ``fmt `` chunk, so NeMo's header check passes."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sampwidth)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * (rate // 100) * channels * sampwidth)
    return buffer.getvalue()


WAV = _wav_bytes()


def _extensible_wav_bytes(subformat: int = 1, bits: int = 16) -> bytes:
    """A 16 kHz mono WAV whose ``fmt `` chunk is WAVEFORMATEXTENSIBLE."""
    block_align = bits // 8
    fmt = struct.pack(
        "<HHIIHH", 0xFFFE, 1, 16000, 16000 * block_align, block_align, bits
    )
    # cbSize, wValidBitsPerSample, dwChannelMask, then the SubFormat GUID.
    fmt += struct.pack("<HHI", 22, bits, 0)
    fmt += (
        struct.pack("<IHH", subformat, 0, 0x0010) + b"\x80\x00\x00\xaa\x00\x38\x9b\x71"
    )
    data = b"\x00\x00" * 160
    return (
        b"RIFF"
        + (4 + 8 + len(fmt) + 8 + len(data)).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + len(fmt).to_bytes(4, "little")
        + fmt
        + b"data"
        + len(data).to_bytes(4, "little")
        + data
    )


class _Child:
    """A runtime that is alive; the readiness gate only polls it."""

    returncode = 0

    def poll(self):
        return None


@pytest.fixture
def client(monkeypatch):
    shim.state.alias = "whisper-tiny"
    shim.state.model_type = "whispercpp"
    shim.state.model = "/models/ggml-tiny.bin"
    shim.state.language = "en"
    shim.state.device = "auto"
    shim.state.encoder = None
    shim.state.diar_model = None
    shim.state.diarize = False
    shim.state.api_key = ""
    shim.state.child_ready = True
    shim.state.child_error = None
    shim.state.child = _Child()
    return TestClient(shim.app)


def _forward_stub(monkeypatch, payload=None, content_type="application/json"):
    captured: dict = {}

    async def _forward(upload, fields):
        captured["fields"] = fields
        # The child is asked for its own format; an omitted response_format
        # leaves the runtime's json default, and diarized_json is translated
        # to verbose_json before it reaches the runtime.
        captured["format"] = dict(fields).get("response_format", "json")
        captured["bytes"] = upload.file.read()
        body = payload if payload is not None else b'{"text": "hi"}'
        return 200, body, content_type

    monkeypatch.setattr(shim, "_forward", _forward)
    return captured


def _post(client, **fields):
    files = {"file": ("sample.wav", io.BytesIO(WAV), "audio/wav")}
    data = {"model": shim.state.alias, **fields}
    return client.post("/v1/audio/transcriptions", files=files, data=data)


def test_whisper_json_is_relayed(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, response_format="json")

    assert response.status_code == 200
    assert response.json() == {"text": "hi"}
    assert captured["format"] == "json"
    # The shim validated the model, so it must not forward it: whisper-server
    # ignores it and NeMo treats an unknown id as a 404.
    assert "model" not in [name for name, _ in captured["fields"]]


def test_whisper_text_is_plain_text(client, monkeypatch):
    _forward_stub(monkeypatch, payload=b"hello", content_type="text/html")
    response = _post(client, response_format="text")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "hello"


def test_whisper_srt_and_vtt_keep_their_content_types(client, monkeypatch):
    _forward_stub(monkeypatch, payload=b"1\n", content_type="application/x-subrip")
    srt = _post(client, response_format="srt")
    assert srt.headers["content-type"] == "application/x-subrip"

    _forward_stub(monkeypatch, payload=b"WEBVTT\n", content_type="text/vtt")
    vtt = _post(client, response_format="vtt")
    assert vtt.headers["content-type"] == "text/vtt; charset=utf-8"


def test_whisper_verbose_json_is_relayed(client, monkeypatch):
    body = {
        "text": "hello",
        "task": "transcribe",
        "language": "en",
        "duration": 1.5,
        "segments": [{"id": 0, "start": 0.0, "end": 1.5, "text": "hello"}],
    }
    _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    response = _post(client, response_format="verbose_json")

    assert response.status_code == 200
    assert response.json()["segments"][0]["text"] == "hello"


def test_whisper_word_granularity_keeps_the_per_segment_words(client, monkeypatch):
    """whisper-server always emits per-segment words in verbose_json.

    The granularity is therefore honoured without forwarding the field; this pins
    that the shim relays those words instead of dropping them.
    """
    body = {
        "text": "hello world",
        "task": "transcribe",
        "language": "en",
        "duration": 1.0,
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": 1.0,
                "text": "hello world",
                "words": [
                    {"word": " hello", "start": 0.0, "end": 0.5},
                    {"word": " world", "start": 0.5, "end": 1.0},
                ],
            }
        ],
    }
    captured = _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    response = _post(
        client,
        response_format="verbose_json",
        **{"timestamp_granularities[]": "word"},
    )

    assert response.status_code == 200
    assert response.json()["segments"][0]["words"] == body["segments"][0]["words"]
    # whisper-server has no such field and does not need it: its verbose_json
    # always carries the words, so forwarding it would be a no-op.
    assert "timestamp_granularities[]" not in [name for name, _ in captured["fields"]]


def test_whisper_word_and_segment_granularities_relay_the_same_body(
    client, monkeypatch
):
    body = {
        "text": "hello world",
        "task": "transcribe",
        "language": "en",
        "duration": 1.0,
        "segments": [
            {
                "id": 0,
                "start": 0.0,
                "end": 1.0,
                "text": "hello world",
                "words": [{"word": " hello", "start": 0.0, "end": 1.0}],
            }
        ],
    }
    _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    word = _post(
        client,
        response_format="verbose_json",
        **{"timestamp_granularities[]": "word"},
    )
    segment = _post(
        client,
        response_format="verbose_json",
        **{"timestamp_granularities[]": "segment"},
    )

    assert word.status_code == segment.status_code == 200
    assert word.json() == segment.json()
    assert word.json()["segments"][0]["words"]


def test_whisper_diarized_json_is_refused(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, response_format="diarized_json")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "response_format 'diarized_json' is not supported by the whisper.cpp "
        "audio backend; supported formats are json, text, srt, verbose_json, vtt"
    )
    assert captured == {}


def test_an_unknown_format_is_refused_with_the_supported_list(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, response_format="mp3")

    assert response.status_code == 400
    assert "must be one of: json, text, srt, verbose_json, vtt, diarized_json" in (
        response.json()["error"]["message"]
    )
    assert captured == {}


def test_stream_is_refused(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, stream="true")

    assert response.status_code == 400
    assert (
        "streaming transcription is not supported"
        in response.json()["error"]["message"]
    )
    assert captured == {}


def test_an_unknown_field_is_refused(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, unexpected="1")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "field 'unexpected' is not supported by /v1/audio/transcriptions"
    )
    assert captured == {}


def test_diarize_is_not_an_accepted_request_field(client, monkeypatch):
    """diarize is a backend config knob, not an OpenAI request field, and the
    shim never reads it; forwarding it verbatim would reach the runtime."""
    captured = _forward_stub(monkeypatch)
    response = _post(client, diarize="true")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "field 'diarize' is not supported by /v1/audio/transcriptions"
    )
    assert captured == {}


def test_the_four_openai_optional_fields_are_accepted_and_never_forwarded_by_name(
    client, monkeypatch
):
    captured = _forward_stub(monkeypatch)
    response = _post(
        client,
        keywords="a,b",
        languages="en",
        chunking_strategy="auto",
        **{"include[]": "logprobs"},
    )

    assert response.status_code == 200
    names = [name for name, _ in captured["fields"]]
    for ignored in (
        "keywords",
        "keywords[]",
        "languages",
        "languages[]",
        "chunking_strategy",
        "include[]",
    ):
        assert ignored not in names


# ── OpenAI optional fields: languages and keywords ─────────────────


def test_a_one_element_languages_set_becomes_language(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, languages='["hu"]')

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert forwarded["language"] == "hu"
    names = [name for name, _ in captured["fields"]]
    assert "languages" not in names
    assert "languages[]" not in names


def test_an_explicit_language_wins_over_languages(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, language="de", languages='["hu"]')

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert forwarded["language"] == "de"
    assert [name for name, _ in captured["fields"]].count("language") == 1


def test_a_multi_element_languages_set_is_dropped(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, languages='["hu", "en"]')

    assert response.status_code == 200
    names = [name for name, _ in captured["fields"]]
    assert "language" not in names
    assert "languages" not in names
    assert "languages[]" not in names


def test_a_one_element_languages_set_from_a_repeated_part_becomes_language(
    client, monkeypatch
):
    captured = _forward_stub(monkeypatch)
    response = _post(client, **{"languages[]": "hu"})

    assert response.status_code == 200
    assert dict(captured["fields"])["language"] == "hu"
    assert "languages[]" not in [name for name, _ in captured["fields"]]


def test_keywords_become_the_whisper_prompt(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, keywords='["Kowalczyk", "Iris"]')

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert forwarded["prompt"] == "Kowalczyk, Iris"
    assert "keywords" not in [name for name, _ in captured["fields"]]


def test_an_explicit_prompt_wins_over_keywords(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, prompt="Existing prompt", keywords='["Kowalczyk"]')

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert forwarded["prompt"] == "Existing prompt"
    assert [name for name, _ in captured["fields"]].count("prompt") == 1


def test_nemo_keywords_become_speech_contexts(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(nemo, keywords='["Kowalczyk", "Iris"]')

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert json.loads(forwarded["speech_contexts"]) == [
        {"phrases": ["Kowalczyk", "Iris"], "boost": 10.0}
    ]
    names = [name for name, _ in captured["fields"]]
    assert "keywords" not in names
    assert "prompt" not in names


def test_nemo_an_explicit_speech_contexts_wins_over_keywords(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    explicit = '[{"phrases": ["Iris"], "boost": 3.0}]'
    response = _post(nemo, speech_contexts=explicit, keywords='["Kowalczyk"]')

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert forwarded["speech_contexts"] == explicit
    assert [name for name, _ in captured["fields"]].count("speech_contexts") == 1


def test_nemo_a_one_element_languages_set_becomes_language(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(nemo, languages='["hu"]')

    assert response.status_code == 200
    assert dict(captured["fields"])["language"] == "hu"


def test_nemo_an_explicit_prompt_wins_over_keywords(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(nemo, prompt="Existing prompt", keywords='["Kowalczyk"]')

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert forwarded["prompt"] == "Existing prompt"
    assert "speech_contexts" not in forwarded


def test_a_missing_file_is_a_400(client):
    response = client.post("/v1/audio/transcriptions", data={"model": "whisper-tiny"})
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "file is required"


def test_a_text_part_named_file_is_a_400(client):
    response = client.post(
        "/v1/audio/transcriptions",
        data={"model": "whisper-tiny", "file": "hello"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "file is required"


def test_a_missing_model_is_a_400(client):
    files = {"file": ("sample.wav", io.BytesIO(WAV), "audio/wav")}
    response = client.post("/v1/audio/transcriptions", files=files)
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "model is required"


def test_an_unknown_model_is_a_404(client):
    files = {"file": ("sample.wav", io.BytesIO(WAV), "audio/wav")}
    response = client.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "other"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_a_garbage_multipart_body_is_a_400_envelope(client):
    response = client.post(
        "/v1/audio/transcriptions",
        content=b"garbage",
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "malformed multipart body"


def test_whisper_without_ffmpeg_refuses_a_non_wav_upload(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    monkeypatch.setattr(shim.shutil, "which", lambda name: None)
    files = {"file": ("sample.mp3", io.BytesIO(b"ID3mp3"), "audio/mpeg")}
    response = client.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "whisper-tiny"},
    )
    assert response.status_code == 400
    assert "ffmpeg is not installed on the host" in response.json()["error"]["message"]
    assert captured == {}


def test_whisper_with_ffmpeg_forwards_a_non_wav_upload(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    monkeypatch.setattr(shim.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    files = {"file": ("sample.mp3", io.BytesIO(b"ID3mp3"), "audio/mpeg")}
    response = client.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "whisper-tiny"},
    )
    assert response.status_code == 200
    assert captured["format"] == "json"


def test_whisper_vad_true_is_refused_without_a_vad_model(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, vad="true")

    assert response.status_code == 400
    assert "vad requires a VAD model" in response.json()["error"]["message"]
    assert captured == {}


def test_whisper_vad_false_is_accepted_and_not_forwarded(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(client, vad="false")

    assert response.status_code == 200
    assert "vad" not in [name for name, _ in captured["fields"]]


def test_whisper_a_child_decode_failure_is_a_400_envelope(client, monkeypatch):
    async def _forward(upload, fields):
        return 500, b'{"error":"FFmpeg conversion failed."}', "application/json"

    monkeypatch.setattr(shim, "_forward", _forward)
    response = _post(client)

    assert response.status_code == 400
    payload = response.json()["error"]
    assert payload["type"] == "invalid_request_error"
    assert "could not decode the uploaded audio" in payload["message"]
    assert "FFmpeg conversion failed." in payload["message"]


def test_whisper_a_child_non_decode_failure_is_still_relayed(client, monkeypatch):
    async def _forward(upload, fields):
        return 500, b'{"error":"internal"}', "application/json"

    monkeypatch.setattr(shim, "_forward", _forward)
    response = _post(client)

    assert response.status_code == 500
    assert response.json() == {"error": "internal"}


def test_a_runtime_that_is_not_ready_answers_503(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    shim.state.child_ready = False
    response = _post(client, response_format="json")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_not_ready"
    assert captured == {}


def test_a_failed_runtime_answers_503(client, monkeypatch):
    captured = _forward_stub(monkeypatch)
    shim.state.child_error = "runtime exited with code 3"
    response = _post(client, response_format="json")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "runtime_failed"
    assert captured == {}


# ── NeMo-Speech.cpp ──────────────────────────────────────────────


@pytest.fixture
def nemo(client):
    shim.state.alias = "nemotron-stream-en"
    shim.state.model_type = "nemospeech"
    shim.state.model = "/models/decoder.gguf"
    shim.state.language = "en"
    shim.state.diar_model = "sortformer.gguf"
    shim.state.diarize = False
    return client


def test_nemo_json_is_relayed(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(nemo, response_format="json")

    assert response.status_code == 200
    names = [name for name, _ in captured["fields"]]
    assert "model" not in names
    # diarize is false and json cannot carry speakers: no flag is added.
    assert "diarization" not in names


def test_nemo_verbose_json_gains_segments_and_diarization(nemo, monkeypatch):
    body = {
        "text": "Hello John",
        "task": "transcribe",
        "language": "en",
        "duration": 2.3,
        "words": [
            {"word": "Hello", "start": 0.1, "end": 0.4, "speaker": "spk:0"},
            {"word": "John", "start": 0.42, "end": 0.8, "speaker": "spk:0"},
        ],
    }
    captured = _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    shim.state.diarize = True
    response = _post(nemo, response_format="verbose_json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["segments"][0]["text"] == "Hello John"
    assert payload["segments"][0]["speaker"] == "spk:0"
    assert ("diarization", "true") in captured["fields"]


def test_nemo_keeps_an_explicit_diarization_false(nemo, monkeypatch):
    """An explicit diarization field wins; it must not be duplicated."""
    captured = _forward_stub(monkeypatch)
    shim.state.diarize = True
    response = _post(nemo, response_format="verbose_json", diarization="false")

    assert response.status_code == 200
    values = [value for name, value in captured["fields"] if name == "diarization"]
    assert values == ["false"]


def test_nemo_json_on_a_diarized_instance_skips_the_flag(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    shim.state.diarize = True
    response = _post(nemo, response_format="json")

    assert response.status_code == 200
    assert "diarization" not in [name for name, _ in captured["fields"]]


def test_nemo_diarized_json_is_synthesized(nemo, monkeypatch):
    body = {
        "text": "Hello John",
        "task": "transcribe",
        "language": "en",
        "duration": 2.3,
        "words": [
            {"word": "Hello", "start": 0.1, "end": 0.4, "speaker": "spk:0"},
            {"word": "John", "start": 0.42, "end": 0.8, "speaker": "spk:0"},
            {"word": "how", "start": 1.4, "end": 1.6, "speaker": "spk:1"},
        ],
    }
    captured = _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    shim.state.diarize = True
    response = _post(nemo, response_format="diarized_json")

    assert response.status_code == 200
    payload = response.json()
    assert payload["segments"][0]["type"] == "transcript.text.segment"
    assert payload["segments"][0]["speaker"] == "A"
    assert payload["speakers"] == [
        {"id": "A", "name": "A"},
        {"id": "B", "name": "B"},
    ]
    assert captured["format"] == "verbose_json"
    assert ("diarization", "true") in captured["fields"]


def _diarized(nemo, monkeypatch, speakers: list[str]) -> dict:
    """POST diarized_json over one word per speaker and return the body."""
    body = {
        "text": "one two three",
        "task": "transcribe",
        "language": "en",
        "duration": 3.0,
        "words": [
            {
                "word": word,
                "start": index * 0.1,
                "end": index * 0.1 + 0.05,
                "speaker": speaker,
            }
            for index, (word, speaker) in enumerate(
                zip(("one", "two", "three"), speakers)
            )
        ],
    }
    _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    shim.state.diarize = True
    response = _post(nemo, response_format="diarized_json")
    assert response.status_code == 200
    return response.json()


def test_diarized_speakers_are_capital_letters_in_first_appearance_order(
    nemo, monkeypatch
):
    payload = _diarized(nemo, monkeypatch, ["spk:1", "spk:1", "spk:0"])

    assert [segment["speaker"] for segment in payload["segments"]] == ["A", "B"]
    assert payload["speakers"] == [
        {"id": "A", "name": "A"},
        {"id": "B", "name": "B"},
    ]


def test_one_diarized_speaker_is_a(nemo, monkeypatch):
    payload = _diarized(nemo, monkeypatch, ["spk:0", "spk:0", "spk:0"])

    assert [segment["speaker"] for segment in payload["segments"]] == ["A"]
    assert payload["speakers"] == [{"id": "A", "name": "A"}]


def test_one_speaker_across_many_words_keeps_one_letter(nemo, monkeypatch):
    payload = _diarized(nemo, monkeypatch, ["spk:0", "spk:0", "spk:0"])

    assert len(payload["segments"]) == 1
    assert len(payload["speakers"]) == 1


def test_a_runtime_that_already_emits_letters_is_not_renumbered(nemo, monkeypatch):
    payload = _diarized(nemo, monkeypatch, ["B", "A", "B"])

    assert list(
        dict.fromkeys(segment["speaker"] for segment in payload["segments"])
    ) == [
        "B",
        "A",
    ]
    assert payload["speakers"] == [
        {"id": "B", "name": "B"},
        {"id": "A", "name": "A"},
    ]


def test_nemo_diarized_json_is_forwarded_as_verbose_json(nemo, monkeypatch):
    """nemo-speech has no diarized_json format; the shim synthesizes it from
    verbose_json, so the child must be asked for verbose_json."""
    body = {
        "text": "Hello John",
        "task": "transcribe",
        "language": "en",
        "duration": 2.3,
        "words": [{"word": "Hello", "start": 0.1, "end": 0.4, "speaker": "spk:0"}],
    }
    captured = _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    shim.state.diarize = True
    response = _post(nemo, response_format="diarized_json")

    assert response.status_code == 200
    forwarded = dict(captured["fields"])
    assert forwarded["response_format"] == "verbose_json"
    assert forwarded["diarization"] == "true"


def test_nemo_diarized_json_without_a_diarizer_is_refused(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    shim.state.diar_model = None
    response = _post(nemo, response_format="diarized_json")

    assert response.status_code == 400
    assert "requires a diarizer model" in response.json()["error"]["message"]
    assert captured == {}


def test_nemo_accepts_uncompressed_wav_only(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    files = {"file": ("sample.mp3", io.BytesIO(b"ID3mp3"), "audio/mpeg")}
    response = nemo.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "nemotron-stream-en"},
    )
    assert response.status_code == 400
    assert "accepts uncompressed WAV only" in response.json()["error"]["message"]
    assert captured == {}


def test_nemo_refuses_a_non_zero_temperature(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(nemo, temperature="0.5")
    assert response.status_code == 400
    assert "temperature is not supported" in response.json()["error"]["message"]
    assert captured == {}


def test_nemo_refuses_a_non_numeric_temperature(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(nemo, temperature="warm")
    assert response.status_code == 400
    assert "is not a number" in response.json()["error"]["message"]
    assert captured == {}


def test_nemo_accepts_a_zero_temperature(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    response = _post(nemo, temperature="0")
    assert response.status_code == 200
    assert captured["format"] == "json"


def test_nemo_granularities_reach_only_the_verbose_formats(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    _post(nemo, response_format="verbose_json", **{"timestamp_granularities[]": "word"})
    names = [name for name, _ in captured["fields"]]
    assert "timestamp_granularities[]" in names

    captured = _forward_stub(monkeypatch)
    _post(nemo, response_format="json", **{"timestamp_granularities[]": "word"})
    names = [name for name, _ in captured["fields"]]
    assert "timestamp_granularities[]" not in names


def test_nemo_text_and_srt_are_relayed(nemo, monkeypatch):
    _forward_stub(monkeypatch, payload=b"hello", content_type="text/plain")
    text = _post(nemo, response_format="text")
    assert text.status_code == 200
    assert text.text == "hello"

    _forward_stub(monkeypatch, payload=b"1\n", content_type="application/x-subrip")
    srt = _post(nemo, response_format="srt")
    assert srt.headers["content-type"] == "application/x-subrip"


def test_nemo_srt_and_vtt_use_the_subtitle_media_types(nemo, monkeypatch):
    """nemo-speech answers srt/vtt as text/plain; the shim must serve the
    media types the other two runtimes use, because a client selects a subtitle
    format by its media type."""
    _forward_stub(monkeypatch, payload=b"1\n", content_type="text/plain")
    srt = _post(nemo, response_format="srt")
    assert srt.headers["content-type"] == "application/x-subrip"

    _forward_stub(monkeypatch, payload=b"WEBVTT\n", content_type="text/plain")
    vtt = _post(nemo, response_format="vtt")
    assert vtt.headers["content-type"] == "text/vtt; charset=utf-8"


# ── WAV format validation for NeMo-Speech.cpp ─────────────────────


@pytest.mark.parametrize(
    ("rate", "channels", "sampwidth"),
    [
        (44100, 2, 2),
        (48000, 1, 2),
        (16000, 2, 2),
        (16000, 1, 1),
    ],
)
def test_nemo_refuses_a_wav_that_is_not_16k_mono_pcm16(
    nemo, monkeypatch, rate, channels, sampwidth
):
    """The old magic-only guard accepted these and mis-transcribed them."""
    captured = _forward_stub(monkeypatch)
    files = {
        "file": (
            "sample.wav",
            io.BytesIO(_wav_bytes(rate=rate, channels=channels, sampwidth=sampwidth)),
            "audio/wav",
        )
    }
    response = nemo.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "nemotron-stream-en"},
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "accepts uncompressed WAV only" in message
    assert f"{rate} Hz" in message
    assert f"{channels} channel" in message
    assert f"{sampwidth * 8}-bit" in message
    assert captured == {}


def test_nemo_accepts_a_16k_mono_pcm16_wav(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    files = {"file": ("sample.wav", io.BytesIO(_wav_bytes()), "audio/wav")}
    response = nemo.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "nemotron-stream-en"},
    )

    assert response.status_code == 200
    assert captured["format"] == "json"


def test_nemo_accepts_a_16k_mono_pcm16_extensible_wav(nemo, monkeypatch):
    """WAVE_FORMAT_EXTENSIBLE with a PCM SubFormat is still PCM16."""
    captured = _forward_stub(monkeypatch)
    files = {"file": ("sample.wav", io.BytesIO(_extensible_wav_bytes()), "audio/wav")}
    response = nemo.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "nemotron-stream-en"},
    )

    assert response.status_code == 200
    assert captured["format"] == "json"


def test_nemo_refuses_a_float32_subformat_extensible_wav(nemo, monkeypatch):
    captured = _forward_stub(monkeypatch)
    files = {
        "file": (
            "sample.wav",
            io.BytesIO(_extensible_wav_bytes(subformat=3, bits=32)),
            "audio/wav",
        )
    }
    response = nemo.post(
        "/v1/audio/transcriptions",
        files=files,
        data={"model": "nemotron-stream-en"},
    )

    assert response.status_code == 400
    assert "IEEE float" in response.json()["error"]["message"]
    assert captured == {}


class _RecordingFile(io.BytesIO):
    """A BytesIO that records every size the parser asks it to read."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def test_a_crafted_fmt_chunk_size_never_reads_more_than_40_bytes():
    """A fmt chunk declaring 0xFFFFFFFF must not become a 4 GiB read.

    The parser only uses the first 40 bytes of the chunk (the largest layout it
    understands), so the read is capped; the fake file records every requested
    size.
    """
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    payload = (
        b"RIFF"
        + (36).to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (0xFFFFFFFF).to_bytes(4, "little")
        + fmt
    )
    recording = _RecordingFile(payload)

    class _Upload:
        file = recording
        filename = "sample.wav"
        content_type = "audio/wav"

    assert shim._wave_format(_Upload()) is not None
    assert recording.read_sizes
    assert max(recording.read_sizes) <= 40


# ── language is the ISO-639-1 code ───────────────────────────────


def test_whisper_verbose_json_language_is_normalised_to_the_iso_code(
    client, monkeypatch
):
    body = {
        "text": "hello",
        "task": "transcribe",
        "language": "hungarian",
        "duration": 1.0,
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello"}],
    }
    _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    response = _post(client, response_format="verbose_json")

    assert response.status_code == 200
    assert response.json()["language"] == "hu"


def test_whisper_verbose_json_language_is_und_when_none_was_detected(
    client, monkeypatch
):
    body = {
        "text": "hello",
        "task": "transcribe",
        "language": "auto",
        "duration": 1.0,
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello"}],
    }
    _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    response = _post(client, response_format="verbose_json")

    assert response.status_code == 200
    assert response.json()["language"] == "und"


def test_nemo_verbose_json_with_a_non_json_body_is_relayed(nemo, monkeypatch):
    """A 200 whose body is not JSON must reach the client, not become a 500.

    The whisper branch already relays such a body with the runtime's content
    type; the NeMo verbose_json/diarized_json path parsed it unguarded.
    """
    _forward_stub(monkeypatch, payload=b"not json", content_type="text/plain")
    response = _post(nemo, response_format="verbose_json")

    assert response.status_code == 200
    assert response.text == "not json"
    assert response.headers["content-type"].startswith("text/plain")


def test_nemo_verbose_json_language_is_und_when_the_runtime_reports_empty(
    nemo, monkeypatch
):
    body = {
        "text": "hello",
        "task": "transcribe",
        "language": "",
        "duration": 1.0,
        "words": [],
    }
    _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    response = _post(nemo, response_format="verbose_json")

    assert response.status_code == 200
    assert response.json()["language"] == "und"


def test_nemo_verbose_json_language_is_kept_as_its_iso_code(nemo, monkeypatch):
    body = {
        "text": "hello",
        "task": "transcribe",
        "language": "en",
        "duration": 1.0,
        "words": [],
    }
    _forward_stub(monkeypatch, payload=json.dumps(body).encode())
    response = _post(nemo, response_format="verbose_json")

    assert response.status_code == 200
    assert response.json()["language"] == "en"


def test_javanese_is_normalised_to_jv():
    """The ISO-639-1 code is jv; whisper.cpp reports the name."""
    assert shim.normalize_language("javanese") == "jv"
    assert shim.normalize_language("jw") == "jv"


# ── word segments never shrink ────────────────────────────────────


def test_an_out_of_order_word_does_not_shrink_a_segment_end():
    """A runtime word whose end is earlier than the segment's must not move the
    segment end backwards."""
    segments = shim._segments_from_words(
        [
            {"word": "hello", "start": 0.0, "end": 1.0},
            {"word": "world", "start": 0.2, "end": 0.3},
        ]
    )

    assert len(segments) == 1
    assert segments[0]["end"] == 1.0
