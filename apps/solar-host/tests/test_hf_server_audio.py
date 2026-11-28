"""The HuggingFace audio path: endpoint formats, refusals and the pipeline call.

The endpoint is exercised directly with a fake ``Request`` and a fake pipeline so the
format matrix is proven without downloading a model.
"""

import io
import json
import logging
import os
import tempfile
import threading
import time
import wave

import numpy as np
import pytest
from fastapi.responses import JSONResponse
from starlette.datastructures import FormData, UploadFile

import solar_host.servers.hf_server as hf
from solar_host.models.base import InstancePhase


def _wav_bytes(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """A real 16 kHz mono WAV, so ffmpeg has something to decode."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = np.zeros(int(seconds * rate), dtype=np.int16).tobytes()
        handle.writeframes(frames)
    return buffer.getvalue()


class _Request:
    def __init__(self, form: FormData):
        self._form = form

    async def form(self):
        return self._form


def _form(**fields) -> FormData:
    payload = _wav_bytes()
    items: list[tuple[str, object]] = [
        (
            "file",
            UploadFile(
                file=io.BytesIO(payload), filename="sample.wav", size=len(payload)
            ),
        )
    ]
    for name, value in fields.items():
        if isinstance(value, list):
            items.extend((name, entry) for entry in value)
        else:
            items.append((name, value))
    return FormData(items)


class _Pipeline:
    """Records how the pipeline was called and returns canned output."""

    # The ASR pipeline sets this from the model; Whisper is the only type that
    # accepts return_language.
    type = "seq2seq_whisper"

    def __init__(
        self, text: str = "hello world", chunks=None, language: str | None = "hungarian"
    ):
        self.text = text
        self.chunks = chunks
        self.language = language
        self.calls: list[dict] = []

    def __call__(self, samples, **kwargs):
        self.calls.append({"samples": samples, **kwargs})
        result: dict = {"text": self.text}
        if self.chunks is not None:
            result["chunks"] = self.chunks
        if kwargs.get("return_language") and self.language is not None:
            result["language"] = self.language
        return result


class _FailingPipeline:
    """Raises on every call, so the route answers its failure envelope."""

    type = "seq2seq_whisper"

    def __call__(self, samples, **kwargs):
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _state(monkeypatch, fake_ffmpeg):
    monkeypatch.setattr(hf.state, "alias", "whisper-hu")
    monkeypatch.setattr(hf.state, "model_type", "audio")
    monkeypatch.setattr(hf.state, "language", None)
    monkeypatch.setattr(hf.state, "chunk_length_s", 30.0)
    monkeypatch.setattr(hf.state, "batch_size", 8)
    monkeypatch.setattr(hf.state, "tokenizer", None)
    monkeypatch.setattr(hf.state, "pipeline", _Pipeline())


async def _call(form: FormData):
    return await hf.audio_transcriptions(_Request(form))


@pytest.mark.anyio
async def test_json_returns_only_the_text():
    response = await _call(_form(model="whisper-hu"))
    assert response == {"text": "hello world"}


@pytest.mark.anyio
async def test_text_returns_plain_text():
    response = await _call(_form(model="whisper-hu", response_format="text"))
    assert response.body == b"hello world"
    assert response.media_type == "text/plain"


@pytest.mark.anyio
async def test_srt_is_rendered_from_the_chunks():
    hf.state.pipeline = _Pipeline(
        chunks=[
            {"text": " hello", "timestamp": (0.0, 1.0)},
            {"text": " world", "timestamp": (1.0, 2.0)},
        ]
    )
    response = await _call(_form(model="whisper-hu", response_format="srt"))
    assert response.body.decode() == (
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nworld\n"
    )


@pytest.mark.anyio
async def test_vtt_starts_with_the_webvtt_header():
    hf.state.pipeline = _Pipeline(chunks=[{"text": " hello", "timestamp": (0.0, 1.5)}])
    response = await _call(_form(model="whisper-hu", response_format="vtt"))
    body = response.body.decode()
    assert body.startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.500" in body


@pytest.mark.anyio
async def test_a_transcript_without_chunks_is_one_text_segment():
    """A pipeline result with only text (no chunks) must not render empty cues.

    The fallback segment used to carry an empty string while the top-level text was
    non-empty, so SRT/VTT had a blank cue and verbose_json a blank segment.
    """
    hf.state.pipeline = _Pipeline(text="hello world")

    payload = await _call(_form(model="whisper-hu", response_format="verbose_json"))
    assert [segment["text"] for segment in payload["segments"]] == ["hello world"]

    response = await _call(_form(model="whisper-hu", response_format="srt"))
    assert response.body.decode() == ("1\n00:00:00,000 --> 00:00:01,000\nhello world\n")


@pytest.mark.anyio
async def test_verbose_json_carries_segments_and_the_real_duration():
    hf.state.pipeline = _Pipeline(
        chunks=[
            {"text": " hello", "timestamp": (0.0, 1.0)},
            {"text": " world", "timestamp": (1.0, 2.0)},
        ]
    )
    # verbose_json returns a dict, which FastAPI serialises itself.
    payload = await _call(_form(model="whisper-hu", response_format="verbose_json"))
    assert payload["task"] == "transcribe"
    assert payload["duration"] == pytest.approx(1.0, abs=0.01)
    assert [s["text"] for s in payload["segments"]] == ["hello", "world"]
    # Not produced by any runtime, so never invented.
    for absent in ("compression_ratio",):
        assert absent not in payload["segments"][0]


@pytest.mark.anyio
async def test_diarized_json_is_refused():
    response = await _call(_form(model="whisper-hu", response_format="diarized_json"))
    assert response.status_code == 400
    assert (
        "not supported by the HuggingFace audio backend"
        in json.loads(response.body)["error"]["message"]
    )


class _TrackingUpload(UploadFile):
    """An upload that counts every close."""

    def __init__(self, data: bytes):
        super().__init__(file=io.BytesIO(data), filename="sample.wav", size=len(data))
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        await super().close()


@pytest.mark.anyio
async def test_a_refused_upload_is_closed():
    """A validation refusal must not leak the spooled upload until GC."""
    upload = _TrackingUpload(_wav_bytes())
    form = FormData(
        [
            ("file", upload),
            ("model", "whisper-hu"),
            ("response_format", "diarized_json"),
        ]
    )

    response = await hf.audio_transcriptions(_Request(form))

    assert response.status_code == 400
    assert upload.close_calls == 1


@pytest.mark.anyio
async def test_every_file_part_is_closed():
    """A multipart body may carry several ``file`` parts, and Starlette's form
    keeps only the last one: the close must cover every part, not just it."""
    first = _TrackingUpload(_wav_bytes())
    second = _TrackingUpload(_wav_bytes())
    form = FormData(
        [
            ("file", first),
            ("file", second),
            ("model", "whisper-hu"),
            ("response_format", "diarized_json"),
        ]
    )

    response = await hf.audio_transcriptions(_Request(form))

    assert response.status_code == 400
    assert first.close_calls == 1
    assert second.close_calls == 1


@pytest.mark.anyio
async def test_a_text_file_part_still_closes_every_upload():
    """A text ``file`` part returns 'file is required' before the values loop;
    that return must still close the form, or an upload under another name keeps
    its spool until GC."""
    other = _TrackingUpload(_wav_bytes())
    form = FormData(
        [
            ("file", "not an upload"),
            ("other", other),
            ("model", "whisper-hu"),
        ]
    )

    response = await hf.audio_transcriptions(_Request(form))

    assert response.status_code == 400
    assert other.close_calls == 1


@pytest.mark.anyio
async def test_a_failed_copy_does_not_leak_the_temp_file(monkeypatch):
    """A seek/copy failure happens before the decode try/finally that unlinks the
    temp copy, so the failed copy itself has to remove it."""
    created: list[str] = []
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def _recording_named_temporary_file(*args, **kwargs):
        handle = real_named_temporary_file(*args, **kwargs)
        created.append(handle.name)
        return handle

    def _failing_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        hf.tempfile, "NamedTemporaryFile", _recording_named_temporary_file
    )
    monkeypatch.setattr(hf.shutil, "copyfileobj", _failing_copy)

    with pytest.raises(OSError, match="disk full"):
        await _call(_form(model="whisper-hu"))

    assert created
    assert not os.path.exists(created[0])


@pytest.mark.anyio
async def test_an_unknown_format_is_refused():
    response = await _call(_form(model="whisper-hu", response_format="mp3"))
    assert response.status_code == 400
    assert "must be one of" in json.loads(response.body)["error"]["message"]


@pytest.mark.anyio
async def test_stream_is_refused():
    response = await _call(_form(model="whisper-hu", stream="true"))
    assert response.status_code == 400
    assert (
        "streaming transcription is not supported"
        in json.loads(response.body)["error"]["message"]
    )


@pytest.mark.anyio
async def test_a_missing_file_is_refused():
    response = await _call(
        _form(model="whisper-hu").__class__([("model", "whisper-hu")])
    )
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["message"] == "file is required"


@pytest.mark.anyio
async def test_a_model_mismatch_is_a_404():
    response = await _call(_form(model="other"))
    assert response.status_code == 404
    assert json.loads(response.body)["error"]["code"] == "model_not_found"


@pytest.mark.anyio
async def test_the_instance_language_is_used_when_the_request_omits_it():
    hf.state.language = "hu"
    await _call(_form(model="whisper-hu"))
    kwargs = hf.state.pipeline.calls[0]["generate_kwargs"]
    assert kwargs["language"] == "hu"
    assert kwargs["task"] == "transcribe"


@pytest.mark.anyio
async def test_a_word_granularity_with_json_does_not_run_the_word_pass():
    """json cannot carry words, so the expensive alignment pass must not run."""
    await _call(_form(model="whisper-hu", **{"timestamp_granularities[]": "word"}))
    assert hf.state.pipeline.calls[0]["return_timestamps"] is False
    assert len(hf.state.pipeline.calls) == 1


@pytest.mark.anyio
async def test_a_segment_granularity_with_json_does_not_pay_for_timestamps():
    """json drops the segments, so asking for them must not slow the pass."""
    response = await _call(
        _form(model="whisper-hu", **{"timestamp_granularities[]": "segment"})
    )
    assert response == {"text": "hello world"}
    assert hf.state.pipeline.calls[0]["return_timestamps"] is False
    assert len(hf.state.pipeline.calls) == 1


@pytest.mark.anyio
async def test_a_word_granularity_with_verbose_json_runs_the_word_pass():
    hf.state.pipeline = _TwoPassPipeline()

    await _call(
        _form(
            model="whisper-hu",
            response_format="verbose_json",
            **{"timestamp_granularities[]": "word"},
        )
    )

    assert [call["return_timestamps"] for call in hf.state.pipeline.calls] == [
        "word",
        True,
    ]


@pytest.mark.anyio
async def test_a_word_granularity_with_srt_does_not_run_the_word_pass():
    """srt carries cues, not words, so the extra pass would be wasted."""
    await _call(
        _form(
            model="whisper-hu",
            response_format="srt",
            **{"timestamp_granularities[]": "word"},
        )
    )
    assert hf.state.pipeline.calls[0]["return_timestamps"] is True
    assert len(hf.state.pipeline.calls) == 1


class _TwoPassPipeline:
    """Word chunks for ``"word"`` and real segments for ``True``."""

    type = "seq2seq_whisper"

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, samples, **kwargs):
        self.calls.append(kwargs)
        if kwargs["return_timestamps"] == "word":
            return {
                "text": " hello world",
                "chunks": [
                    {"text": " hello", "timestamp": (0.0, 0.4)},
                    {"text": " world", "timestamp": (0.4, 1.0)},
                ],
            }
        return {
            "text": "hello world",
            "chunks": [{"text": " hello world", "timestamp": (0.0, 1.0)}],
        }


@pytest.mark.anyio
async def test_a_word_granularity_returns_real_segments_with_the_words_attached():
    hf.state.pipeline = _TwoPassPipeline()

    payload = await _call(
        _form(
            model="whisper-hu",
            response_format="verbose_json",
            **{"timestamp_granularities[]": "word"},
        )
    )

    # The word pass runs first, so its dedicated timeout still answers with the
    # word-specific 504; the second pass supplies the model's own segments.
    assert [call["return_timestamps"] for call in hf.state.pipeline.calls] == [
        "word",
        True,
    ]
    assert len(payload["segments"]) == 1
    assert payload["segments"][0]["text"] == "hello world"
    assert payload["segments"][0]["words"] == [
        {"word": " hello", "start": 0.0, "end": 0.4},
        {"word": " world", "start": 0.4, "end": 1.0},
    ]
    assert payload["words"] == [
        {"word": " hello", "start": 0.0, "end": 0.4},
        {"word": " world", "start": 0.4, "end": 1.0},
    ]


@pytest.mark.anyio
async def test_words_are_attached_to_the_segment_that_contains_them():
    class _Pipeline:
        def __init__(self):
            self.calls: list[dict] = []

        def __call__(self, samples, **kwargs):
            self.calls.append(kwargs)
            if kwargs["return_timestamps"] == "word":
                return {
                    "text": " one two three four",
                    "chunks": [
                        {"text": " one", "timestamp": (0.0, 0.4)},
                        {"text": " two", "timestamp": (0.6, 0.9)},
                        {"text": " three", "timestamp": (1.1, 1.4)},
                        {"text": " four", "timestamp": (1.6, 1.9)},
                    ],
                }
            return {
                "text": "one two three four",
                "chunks": [
                    {"text": " one two", "timestamp": (0.0, 1.0)},
                    {"text": " three four", "timestamp": (1.0, 2.0)},
                ],
            }

    hf.state.pipeline = _Pipeline()

    payload = await _call(
        _form(
            model="whisper-hu",
            response_format="verbose_json",
            **{"timestamp_granularities[]": "word"},
        )
    )

    assert [word["word"] for word in payload["segments"][0]["words"]] == [
        " one",
        " two",
    ]
    assert [word["word"] for word in payload["segments"][1]["words"]] == [
        " three",
        " four",
    ]


@pytest.mark.anyio
async def test_words_before_and_after_every_segment_stay_on_the_edge_segments():
    class _Pipeline:
        def __call__(self, samples, **kwargs):
            if kwargs["return_timestamps"] == "word":
                return {
                    "text": " before after",
                    "chunks": [
                        {"text": " before", "timestamp": (0.0, 0.1)},
                        {"text": " after", "timestamp": (3.0, 3.1)},
                    ],
                }
            return {
                "text": "before after",
                "chunks": [{"text": " before after", "timestamp": (0.5, 2.0)}],
            }

    hf.state.pipeline = _Pipeline()

    payload = await _call(
        _form(
            model="whisper-hu",
            response_format="verbose_json",
            **{"timestamp_granularities[]": "word"},
        )
    )

    # No word may be dropped because the two passes disagree by a few
    # milliseconds; the edge segment takes it.
    assert [word["word"] for word in payload["segments"][0]["words"]] == [
        " before",
        " after",
    ]
    assert payload["words"] == [
        {"word": " before", "start": 0.0, "end": 0.1},
        {"word": " after", "start": 3.0, "end": 3.1},
    ]


@pytest.mark.anyio
async def test_segment_granularity_does_not_add_words_or_a_second_pass():
    hf.state.pipeline = _Pipeline(chunks=[{"text": " hello", "timestamp": (0.0, 1.0)}])

    payload = await _call(
        _form(
            model="whisper-hu",
            response_format="verbose_json",
            **{"timestamp_granularities[]": "segment"},
        )
    )

    assert len(hf.state.pipeline.calls) == 1
    assert "words" not in payload
    assert "words" not in payload["segments"][0]


@pytest.mark.anyio
async def test_words_without_a_timestamp_are_skipped_not_given_a_made_up_time():
    from solar_host.servers.audio_formats import words_from_chunks

    assert words_from_chunks(
        [
            {"text": " hello", "timestamp": (0.0, 0.5)},
            {"text": " lost"},
            {"text": " ", "timestamp": (0.5, 0.6)},
        ]
    ) == [{"word": " hello", "start": 0.0, "end": 0.5}]


@pytest.mark.anyio
async def test_a_slow_word_pass_on_verbose_json_times_out_before_the_segment_pass(
    monkeypatch,
):
    release = threading.Event()

    class _Pipeline:
        def __call__(self, samples, **kwargs):
            release.wait(5)
            return {"text": "late"}

    monkeypatch.setattr(hf.state, "pipeline", _Pipeline())
    monkeypatch.setattr(hf, "WORD_TIMESTAMP_TIMEOUT_S", 0.05)
    try:
        response = await _call(
            _form(
                model="whisper-hu",
                response_format="verbose_json",
                **{"timestamp_granularities[]": "word"},
            )
        )
    finally:
        release.set()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 504
    payload = json.loads(bytes(response.body))["error"]
    assert payload["code"] == "timeout"
    assert "word-level timestamps did not finish" in payload["message"]


@pytest.mark.anyio
async def test_a_slow_segment_pass_after_a_word_pass_times_out_with_an_envelope(
    monkeypatch,
):
    release = threading.Event()

    class _Pipeline:
        def __call__(self, samples, **kwargs):
            if kwargs["return_timestamps"] == "word":
                return {
                    "text": " hello",
                    "chunks": [{"text": " hello", "timestamp": (0.0, 0.5)}],
                }
            release.wait(5)
            return {"text": "late"}

    monkeypatch.setattr(hf.state, "pipeline", _Pipeline())
    monkeypatch.setattr(hf, "TRANSCRIPTION_TIMEOUT_S", 0.05)
    try:
        response = await _call(
            _form(
                model="whisper-hu",
                response_format="verbose_json",
                **{"timestamp_granularities[]": "word"},
            )
        )
    finally:
        release.set()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 504
    payload = json.loads(bytes(response.body))["error"]
    assert payload["code"] == "timeout"
    assert "transcription did not finish" in payload["message"]


@pytest.mark.anyio
async def test_a_slow_word_timestamp_pipeline_times_out_with_an_envelope(monkeypatch):
    """The pipeline thread cannot be cancelled; the request must still return."""
    release = threading.Event()

    class _SlowPipeline:
        def __call__(self, samples, **kwargs):
            release.wait(5)
            return {"text": "late"}

    monkeypatch.setattr(hf.state, "pipeline", _SlowPipeline())
    monkeypatch.setattr(hf, "WORD_TIMESTAMP_TIMEOUT_S", 0.05)
    try:
        response = await _call(
            _form(
                model="whisper-hu",
                response_format="verbose_json",
                **{"timestamp_granularities[]": "word"},
            )
        )
    finally:
        release.set()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 504
    payload = json.loads(bytes(response.body))["error"]
    assert payload["code"] == "timeout"
    assert "word-level timestamps did not finish" in payload["message"]
    assert "timestamp_granularities[]=segment" in payload["message"]


@pytest.mark.anyio
async def test_a_slow_plain_transcription_times_out_with_an_envelope(monkeypatch):
    release = threading.Event()

    class _SlowPipeline:
        def __call__(self, samples, **kwargs):
            release.wait(5)
            return {"text": "late"}

    monkeypatch.setattr(hf.state, "pipeline", _SlowPipeline())
    monkeypatch.setattr(hf, "TRANSCRIPTION_TIMEOUT_S", 0.05)
    try:
        response = await _call(_form(model="whisper-hu"))
    finally:
        release.set()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 504
    payload = json.loads(bytes(response.body))["error"]
    assert payload["code"] == "timeout"
    assert "transcription did not finish" in payload["message"]


@pytest.mark.anyio
async def test_verbose_json_asks_for_segment_timestamps():
    await _call(_form(model="whisper-hu", response_format="verbose_json"))
    assert hf.state.pipeline.calls[0]["return_timestamps"] is True


@pytest.mark.anyio
async def test_plain_json_does_not_pay_for_timestamps():
    await _call(_form(model="whisper-hu"))
    assert hf.state.pipeline.calls[0]["return_timestamps"] is False


class _PromptIds:
    """A torch.Tensor stand-in that records the device it is moved to."""

    def __init__(self, ids: list[list[int]]):
        self.ids = ids
        self.devices: list[str] = []

    def to(self, device: str) -> "_PromptIds":
        self.devices.append(device)
        return self

    def __eq__(self, other: object) -> bool:
        return self.ids == other


@pytest.mark.anyio
async def test_a_prompt_uses_the_tokenizer_prompt_ids():
    class _Tokenizer:
        def get_prompt_ids(self, prompt, return_tensors):
            assert return_tensors == "pt"
            self.prompt_ids = _PromptIds([[1, 2, 3]])
            return self.prompt_ids

    tokenizer = _Tokenizer()
    hf.state.tokenizer = tokenizer
    await _call(_form(model="whisper-hu", prompt="Hello there"))
    assert hf.state.pipeline.calls[0]["generate_kwargs"]["prompt_ids"] == [[1, 2, 3]]


@pytest.mark.anyio
async def test_prompt_ids_move_to_the_pipeline_device(monkeypatch):
    """Whisper concatenates prompt_ids with on-device decoder inputs, so a
    CPU tensor fails with a device mismatch on a cuda/mps instance."""

    class _Tokenizer:
        def get_prompt_ids(self, prompt, return_tensors):
            self.prompt_ids = _PromptIds([[1, 2, 3]])
            return self.prompt_ids

    tokenizer = _Tokenizer()
    hf.state.tokenizer = tokenizer
    monkeypatch.setattr(hf.state, "device", "cuda")

    await _call(_form(model="whisper-hu", prompt="Hello there"))

    assert tokenizer.prompt_ids.devices == ["cuda"]


@pytest.mark.anyio
async def test_a_prompt_without_tokenizer_support_is_refused():
    response = await _call(_form(model="whisper-hu", prompt="Hello"))
    assert response.status_code == 400
    assert "prompt is not supported" in json.loads(response.body)["error"]["message"]


class _RecordingTokenizer:
    """A tokenizer that records the prompt it was asked to encode."""

    def __init__(self):
        self.prompts: list[str] = []

    def get_prompt_ids(self, prompt, return_tensors):
        self.prompts.append(prompt)
        return _PromptIds([[1, 2, 3]])


@pytest.mark.anyio
async def test_keywords_become_the_prompt_ids():
    tokenizer = _RecordingTokenizer()
    hf.state.tokenizer = tokenizer

    await _call(_form(model="whisper-hu", keywords=["Kowalczyk", "Iris"]))

    assert tokenizer.prompts == ["Kowalczyk, Iris"]
    assert hf.state.pipeline.calls[0]["generate_kwargs"]["prompt_ids"] == [[1, 2, 3]]


@pytest.mark.anyio
async def test_an_explicit_prompt_wins_over_keywords():
    tokenizer = _RecordingTokenizer()
    hf.state.tokenizer = tokenizer

    await _call(
        _form(model="whisper-hu", prompt="Existing prompt", keywords=["Kowalczyk"])
    )

    assert tokenizer.prompts == ["Existing prompt"]


@pytest.mark.anyio
async def test_a_one_element_languages_set_becomes_language():
    await _call(_form(model="whisper-hu", languages=["hu"]))

    assert hf.state.pipeline.calls[0]["generate_kwargs"]["language"] == "hu"


@pytest.mark.anyio
async def test_an_explicit_language_wins_over_languages():
    await _call(_form(model="whisper-hu", language="de", languages='["hu"]'))

    assert hf.state.pipeline.calls[0]["generate_kwargs"]["language"] == "de"


@pytest.mark.anyio
async def test_language_auto_is_treated_as_omitted():
    """Whisper's lang_to_id has no 'auto' token; omitting the language asks
    the pipeline to detect it instead of raising Unsupported language."""
    await _call(_form(model="whisper-hu", language="auto"))

    assert hf.state.pipeline is not None
    assert "language" not in hf.state.pipeline.calls[0]["generate_kwargs"]


@pytest.mark.anyio
async def test_a_multi_element_languages_set_is_dropped():
    await _call(_form(model="whisper-hu", languages='["hu", "en"]'))

    assert "language" not in hf.state.pipeline.calls[0]["generate_kwargs"]


@pytest.mark.anyio
async def test_the_chunk_length_and_batch_size_reach_the_pipeline():
    await _call(_form(model="whisper-hu"))
    call = hf.state.pipeline.calls[0]
    assert call["chunk_length_s"] == 30.0
    assert call["batch_size"] == 8


@pytest.mark.anyio
async def test_a_temperature_reaches_the_generate_kwargs():
    await _call(_form(model="whisper-hu", temperature="0.4"))
    kwargs = hf.state.pipeline.calls[0]["generate_kwargs"]
    assert kwargs["temperature"] == 0.4
    assert kwargs["do_sample"] is True


@pytest.mark.anyio
async def test_a_zero_temperature_is_not_forwarded():
    await _call(_form(model="whisper-hu", temperature="0"))
    assert "temperature" not in hf.state.pipeline.calls[0]["generate_kwargs"]


@pytest.mark.anyio
async def test_a_broken_upload_is_a_400():
    payload = b"not a wav file"
    items = [
        (
            "file",
            UploadFile(file=io.BytesIO(payload), filename="x.bin", size=len(payload)),
        ),
        ("model", "whisper-hu"),
    ]
    response = await _call(FormData(items))
    assert response.status_code == 400
    assert (
        "could not decode the uploaded audio"
        in json.loads(response.body)["error"]["message"]
    )


@pytest.mark.anyio
async def test_a_hung_ffmpeg_is_killed_and_answered_with_the_decode_envelope(
    monkeypatch, tmp_path
):
    """A crafted upload must not hold the request open: the decode is bounded,
    the decoder is killed, and the client gets the decode-failure envelope."""
    bin_dir = tmp_path / "hung-bin"
    bin_dir.mkdir()
    launcher = bin_dir / "ffmpeg"
    launcher.write_text("#!/bin/sh\nexec sleep 300\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(hf, "DECODE_TIMEOUT_S", 0.1)

    started = time.monotonic()
    response = await _call(_form(model="whisper-hu"))
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    payload = json.loads(response.body)["error"]
    assert payload["param"] == "file"
    assert "could not decode the uploaded audio" in payload["message"]
    assert "did not finish" in payload["message"]
    # The hung decoder never reached the pipeline.
    assert hf.state.pipeline.calls == []


@pytest.mark.anyio
async def test_a_non_numeric_temperature_is_a_400_envelope():
    """The shim answers this with the envelope; the HuggingFace route must not
    turn a malformed temperature into an unhandled ValueError (a 500)."""
    response = await _call(_form(model="whisper-hu", temperature="warm"))
    assert response.status_code == 400
    assert "is not a number" in json.loads(response.body)["error"]["message"]


def test_a_malformed_multipart_body_is_a_400_envelope(monkeypatch):
    """Starlette converts the parser error to HTTPException(400), whose default
    body is {"detail": ...}; the route must answer with the OpenAI envelope."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(hf.state, "api_key", "")
    app = FastAPI()
    app.post("/v1/audio/transcriptions")(hf.audio_transcriptions)
    client = TestClient(app)

    response = client.post(
        "/v1/audio/transcriptions",
        content=b"garbage",
        headers={"content-type": "multipart/form-data; boundary=x"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "malformed multipart body"


# ── language is the ISO-639-1 code ───────────────────────────────


@pytest.mark.anyio
async def test_verbose_json_reports_the_detected_language_when_the_request_omits_it():
    payload = await _call(_form(model="whisper-hu", response_format="verbose_json"))

    assert payload["language"] == "hu"
    assert hf.state.pipeline.calls[0]["return_language"] is True


@pytest.mark.anyio
async def test_verbose_json_language_is_und_when_the_pipeline_returns_none():
    hf.state.pipeline = _Pipeline(language=None)
    payload = await _call(_form(model="whisper-hu", response_format="verbose_json"))
    assert payload["language"] == "und"


@pytest.mark.anyio
async def test_verbose_json_language_is_normalised_to_the_iso_code():
    payload = await _call(
        _form(model="whisper-hu", response_format="verbose_json", language="hungarian")
    )
    assert payload["language"] == "hu"


class _NonWhisperPipeline:
    """A seq2seq ASR pipeline that is not Whisper.

    transformers raises ValueError when ``return_language`` is passed to anything
    but a Whisper pipeline, so this fake does the same.
    """

    type = "seq2seq"

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, samples, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("return_language") is not None:
            raise ValueError("Only Whisper can return language for now.")
        return {"text": "hello world"}


@pytest.mark.anyio
async def test_a_non_whisper_pipeline_is_not_asked_for_the_language():
    """return_language is Whisper-only; a seq2seq pipeline must not receive it
    and verbose_json must report the language as und."""
    pipeline = _NonWhisperPipeline()
    hf.state.pipeline = pipeline

    payload = await _call(_form(model="whisper-hu", response_format="verbose_json"))

    assert isinstance(payload, dict)
    assert payload["language"] == "und"
    assert "return_language" not in pipeline.calls[0]


@pytest.mark.anyio
async def test_a_non_whisper_pipeline_gets_no_whisper_only_generate_kwargs():
    """`task`, `language` and `prompt_ids` are Whisper-only: another seq2seq
    checkpoint raises when they reach model.generate()."""
    pipeline = _NonWhisperPipeline()
    hf.state.pipeline = pipeline
    hf.state.language = "hu"

    await _call(_form(model="whisper-hu"))

    assert pipeline.calls[0]["generate_kwargs"] == {}


# ── the field set is the same as the shim's ──────────────────────


@pytest.mark.anyio
async def test_an_unknown_field_is_refused_like_the_shim():
    """The shim and NeMo refuse an unknown field; HuggingFace used to return
    200 and ignore it, so the same request behaved differently per backend."""
    response = await _call(_form(model="whisper-hu", unexpected="1"))

    assert response.status_code == 400
    assert json.loads(response.body)["error"]["message"] == (
        "field 'unexpected' is not supported by /v1/audio/transcriptions"
    )


@pytest.mark.anyio
async def test_a_backend_specific_field_is_accepted_and_dropped():
    """diarization belongs to NeMo; HuggingFace accepts and ignores it so one
    client request works on all three backends."""
    response = await _call(_form(model="whisper-hu", diarization="true"))

    assert response == {"text": "hello world"}


@pytest.mark.anyio
async def test_an_unknown_granularity_is_refused_before_the_pipeline():
    """The shim and the control plane answer 400 for this request, so a direct
    host request must not silently accept the value the route cannot honour."""
    response = await _call(
        _form(
            model="whisper-hu",
            response_format="verbose_json",
            **{"timestamp_granularities[]": ["word", "sentence"]},
        )
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    payload = json.loads(response.body)["error"]
    assert payload["message"] == (
        "timestamp_granularities must contain only 'word' or 'segment'"
    )
    assert payload["param"] == "timestamp_granularities"
    # Refused before any pipeline work.
    assert hf.state.pipeline.calls == []


@pytest.mark.anyio
async def test_the_non_array_granularity_spelling_is_validated_too():
    """A client may send the field without the [] suffix; the shim validates both
    spellings, so this route must too."""
    response = await _call(
        _form(
            model="whisper-hu",
            response_format="verbose_json",
            timestamp_granularities="sentence",
        )
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["message"] == (
        "timestamp_granularities must contain only 'word' or 'segment'"
    )
    assert hf.state.pipeline.calls == []


# ── a non-audio instance refuses the route ─────────────────────────


def test_a_non_audio_instance_is_refused_with_a_400(monkeypatch):
    """A direct request to a causal instance must be a 400, like every sibling
    route, not a bare 500 from the pipeline assertion."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(hf.state, "model_type", "causal")
    monkeypatch.setattr(hf.state, "pipeline", None)
    monkeypatch.setattr(hf.state, "api_key", "")
    app = FastAPI()
    app.post("/v1/audio/transcriptions")(hf.audio_transcriptions)
    client = TestClient(app)

    response = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("sample.wav", io.BytesIO(_wav_bytes()), "audio/wav")},
        data={"model": "whisper-hu"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "message": "Transcriptions only available for audio models",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_request_error",
    }


# ── the runner sees the audio request as busy ─────────────────────


@pytest.mark.anyio
async def test_the_route_logs_the_request_and_complete_lines_the_runner_parses(caplog):
    """The HuggingFace runner derives `busy` from these two lines.

    Without them an audio instance reports idle for the whole transcription, so
    both lines must be present and shaped so the runner's regexes match.
    """
    from solar_host.backends.huggingface import HuggingFaceRunner

    caplog.set_level(logging.INFO, logger=hf.logger.name)
    await _call(_form(model="whisper-hu"))

    messages = [record.getMessage() for record in caplog.records]
    request_lines = [line for line in messages if line.startswith("[REQUEST]")]
    complete_lines = [line for line in messages if line.startswith("[COMPLETE]")]
    assert len(request_lines) == 1
    assert len(complete_lines) == 1

    runner = HuggingFaceRunner()
    context = runner.initialize_context()
    busy = runner.parse_log_line("inst-1", request_lines[0], context)
    idle = runner.parse_log_line("inst-1", complete_lines[0], context)

    assert busy is not None and busy.busy is True
    assert busy.phase is InstancePhase.GENERATING
    assert idle is not None and idle.busy is False
    assert idle.phase is InstancePhase.IDLE
    # An audio request has no token count, so the runner records none.
    assert idle.generated_tokens == 0

    # A failed pass answers an error envelope; the runner clears `busy` only on
    # an `[ERROR]` line, so the route must log one or the instance stays
    # `generating` until the next successful request.
    caplog.clear()
    hf.state.pipeline = _FailingPipeline()
    response = await _call(_form(model="whisper-hu"))
    assert isinstance(response, JSONResponse)
    assert response.status_code == 500

    messages = [record.getMessage() for record in caplog.records]
    request_lines = [line for line in messages if line.startswith("[REQUEST]")]
    error_lines = [line for line in messages if line.startswith("[ERROR]")]
    assert len(request_lines) == 1
    assert len(error_lines) == 1
    assert "boom" in error_lines[0]

    failed_context = runner.initialize_context()
    failed_busy = runner.parse_log_line("inst-1", request_lines[0], failed_context)
    failed_idle = runner.parse_log_line("inst-1", error_lines[0], failed_context)
    assert failed_busy is not None and failed_busy.busy is True
    assert failed_idle is not None and failed_idle.busy is False
    assert failed_idle.phase is InstancePhase.IDLE


@pytest.mark.anyio
async def test_a_timed_out_request_logs_an_error_line_the_runner_parses(
    monkeypatch, caplog
):
    """A 504 must leave the instance idle too: a bare TimeoutError stringifies
    to "", so the route logs the timeout text the runner's regex matches."""
    from solar_host.backends.huggingface import HuggingFaceRunner

    release = threading.Event()

    class _SlowPipeline:
        def __call__(self, samples, **kwargs):
            release.wait(5)
            return {"text": "late"}

    monkeypatch.setattr(hf.state, "pipeline", _SlowPipeline())
    monkeypatch.setattr(hf, "TRANSCRIPTION_TIMEOUT_S", 0.05)
    caplog.set_level(logging.INFO, logger=hf.logger.name)
    try:
        response = await _call(_form(model="whisper-hu"))
    finally:
        release.set()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 504
    messages = [record.getMessage() for record in caplog.records]
    request_lines = [line for line in messages if line.startswith("[REQUEST]")]
    error_lines = [line for line in messages if line.startswith("[ERROR]")]
    assert len(request_lines) == 1
    assert len(error_lines) == 1
    assert "transcription did not finish" in error_lines[0]

    runner = HuggingFaceRunner()
    context = runner.initialize_context()
    busy = runner.parse_log_line("inst-1", request_lines[0], context)
    idle = runner.parse_log_line("inst-1", error_lines[0], context)
    assert busy is not None and busy.busy is True
    assert idle is not None and idle.busy is False
    assert idle.phase is InstancePhase.IDLE
