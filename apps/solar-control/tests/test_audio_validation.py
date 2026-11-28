"""Tests for the transcription request model and the audio backend contract."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.gateway import OpenAIGateway
from app.models.openai import TranscriptionRequest
from app.validation import (
    BACKEND_FIELD_OWNERS,
    VALID_BACKEND_TYPES,
    _validate_backend_field_ownership,
    _validate_backend_model_selection,
    _validate_device,
    _validate_gpu_count,
)

AUDIO_BACKENDS = ("whispercpp", "nemospeech", "huggingface_audio")


# ── TranscriptionRequest ───────────────────────────────────────────


def test_minimal_request_validates():
    request = TranscriptionRequest(model="whisper-tiny")
    assert request.response_format == "json"
    assert request.language is None
    assert request.prompt is None
    assert request.temperature is None
    assert request.timestamp_granularities is None
    assert request.stream is False


def test_every_response_format_is_accepted():
    for value in ("json", "text", "srt", "verbose_json", "vtt", "diarized_json"):
        assert (
            TranscriptionRequest(model="m", response_format=value).response_format
            == value
        )


def test_unknown_response_format_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        TranscriptionRequest(model="m", response_format="mp3")
    message = str(excinfo.value)
    assert "response_format 'mp3' is not supported" in message
    assert "json, text, srt, verbose_json, vtt, diarized_json" in message


def test_stream_is_rejected_with_the_shared_message():
    with pytest.raises(ValidationError) as excinfo:
        TranscriptionRequest(model="m", stream=True)
    assert "streaming transcription is not supported" in str(excinfo.value)


def test_granularities_are_limited_to_word_and_segment():
    request = TranscriptionRequest(
        model="m", timestamp_granularities=["word", "segment"]
    )
    assert request.timestamp_granularities == ["word", "segment"]
    with pytest.raises(ValidationError):
        TranscriptionRequest(model="m", timestamp_granularities=["sentence"])


def test_temperature_must_be_between_zero_and_one():
    assert TranscriptionRequest(model="m", temperature=0.5).temperature == 0.5
    with pytest.raises(ValidationError):
        TranscriptionRequest(model="m", temperature=1.5)


def test_empty_model_is_rejected():
    with pytest.raises(ValidationError):
        TranscriptionRequest(model="  ")


def test_from_form_keeps_repeated_granularities():
    from starlette.datastructures import FormData

    form = FormData(
        [
            ("model", "whisper-tiny"),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
            ("timestamp_granularities[]", "segment"),
        ]
    )
    request = TranscriptionRequest.from_form(form)
    assert request.model == "whisper-tiny"
    assert request.response_format == "verbose_json"
    assert request.timestamp_granularities == ["word", "segment"]


def test_from_form_defaults_an_omitted_format():
    from starlette.datastructures import FormData

    request = TranscriptionRequest.from_form(FormData([("model", "m")]))
    assert request.response_format == "json"


# ── backend tables ───────────────────────────────────────────────


def test_audio_backends_are_valid():
    for backend_type in AUDIO_BACKENDS:
        assert backend_type in VALID_BACKEND_TYPES


def test_backend_field_owners_cover_the_audio_fields():
    assert BACKEND_FIELD_OWNERS["whispercpp"] == frozenset(
        {"model_file", "threads", "language"}
    )
    assert BACKEND_FIELD_OWNERS["nemospeech"] == frozenset(
        {
            "model_file",
            "language",
            "encoder",
            "instruction",
            "translate_to",
            "diar_model",
            "diarize",
        }
    )
    assert BACKEND_FIELD_OWNERS["huggingface_audio"] == frozenset(
        {
            "device",
            "dtype",
            "trust_remote_code",
            "language",
            "chunk_length_s",
            "batch_size",
        }
    )


def test_model_file_is_allowed_on_the_ggml_backends_only():
    for backend_type in ("llamacpp", "whispercpp", "nemospeech"):
        errors = _validate_backend_field_ownership(
            {"backend_type": backend_type, "model_file": "ggml-tiny.bin"}
        )
        assert errors == [], backend_type
    errors = _validate_backend_field_ownership(
        {"backend_type": "huggingface_audio", "model_file": "x"}
    )
    assert errors and errors[0]["field"] == "backend.model_file"


def test_encoder_and_diarizer_are_nemo_only():
    assert (
        _validate_backend_field_ownership(
            {"backend_type": "nemospeech", "encoder": "e.gguf"}
        )
        == []
    )
    assert (
        _validate_backend_field_ownership(
            {"backend_type": "nemospeech", "diar_model": "d.gguf"}
        )
        == []
    )
    for backend_type in ("whispercpp", "huggingface_audio"):
        errors = _validate_backend_field_ownership(
            {"backend_type": backend_type, "encoder": "e.gguf"}
        )
        assert errors and errors[0]["field"] == "backend.encoder"


def test_instruction_and_translation_are_nemo_only():
    """The host's NemoSpeechConfig carries both; whispercpp must not.

    Before round 3 neither field had an owner, so the forward-compatibility
    rule let a whispercpp intent carry them and the host dropped them silently.
    """
    for field, value in (
        ("instruction", "Transcribe verbatim."),
        ("translate_to", "en"),
    ):
        assert (
            _validate_backend_field_ownership(
                {"backend_type": "nemospeech", field: value}
            )
            == []
        ), field
        errors = _validate_backend_field_ownership(
            {"backend_type": "whispercpp", field: value}
        )
        assert errors and errors[0]["field"] == f"backend.{field}", field


def test_audio_device_vocabulary_is_per_backend():
    assert _validate_device({"backend_type": "whispercpp", "device": "cpu"}, {}) == []
    errors = _validate_device({"backend_type": "whispercpp", "device": "cuda"}, {})
    assert errors and "auto or cpu" in errors[0]["message"]

    assert _validate_device({"backend_type": "nemospeech", "device": "cuda"}, {}) == []
    errors = _validate_device({"backend_type": "nemospeech", "device": "mps"}, {})
    assert errors and "auto, cpu or cuda" in errors[0]["message"]

    assert (
        _validate_device({"backend_type": "huggingface_audio", "device": "mps"}, {})
        == []
    )


def test_nemospeech_cuda_contradicts_a_non_nvidia_placement():
    """cuda is in the NeMo vocabulary but still needs an NVIDIA host.

    The nemospeech branch used to return before the placement check, so this
    intent passed the form and failed only when the runtime started.
    """
    errors = _validate_device(
        {"backend_type": "nemospeech", "device": "cuda"},
        {"gpu_type": "apple_mps"},
    )
    assert errors and errors[0]["field"] == "backend.device"
    assert "requires gpu_type 'nvidia_cuda'" in errors[0]["message"]
    assert (
        _validate_device(
            {"backend_type": "nemospeech", "device": "cuda"},
            {"gpu_type": "nvidia_cuda"},
        )
        == []
    )


def test_audio_backends_stay_single_gpu():
    for backend_type in AUDIO_BACKENDS:
        data = {
            "backend": {"backend_type": backend_type},
            "resources": {"gpu_count": 2},
        }
        errors: list[dict[str, str]] = []
        _validate_gpu_count(data, errors)
        assert errors, backend_type
        assert "single-GPU" in errors[0]["message"]


def test_model_file_is_required_when_the_pull_returns_a_directory():
    for backend_type in ("whispercpp", "nemospeech"):
        errors = _validate_backend_model_selection(
            {"backend_type": backend_type}, "huggingface://org/repo"
        )
        assert errors and errors[0]["field"] == "backend.model_file"
        assert "model_file is required" in errors[0]["message"]
        # An explicit local path needs no selector.
        assert (
            _validate_backend_model_selection(
                {"backend_type": backend_type, "model": "/models/x.gguf"}, None
            )
            == []
        )


def test_model_file_is_not_required_for_a_local_file_source():
    """A local:// file already names the model; the pull rule must not fire."""
    for backend_type in ("whispercpp", "nemospeech"):
        assert (
            _validate_backend_model_selection(
                {"backend_type": backend_type},
                "local:///models/x.gguf",
            )
            == []
        )


def test_model_file_is_required_for_a_pull_source_and_a_local_directory_passes(
    tmp_path,
):
    """Only a pull source is knowable on the control plane.

    The control plane's filesystem is not the host's, so a local path that is a
    directory on the host cannot be recognised here; the host's own
    missing-model-file check is the authority for a directory model.
    """
    for backend_type in ("whispercpp", "nemospeech"):
        # (a) A pull source without model_file is still rejected.
        errors = _validate_backend_model_selection(
            {"backend_type": backend_type}, "repo://models/whisper"
        )
        assert errors and errors[0]["field"] == "backend.model_file"
        assert "model_file is required" in errors[0]["message"]

        # (b) A local directory path passes control and is judged by the host.
        assert (
            _validate_backend_model_selection(
                {"backend_type": backend_type, "model": str(tmp_path)}, None
            )
            == []
        )


def test_a_diarized_nemo_intent_requires_a_diarizer_model():
    """The host's NemoSpeechConfig.check_diarizer rejects this combination.

    Without the rule a direct API client could save diarize=true with no
    diar_model and only fail when the instance was created.
    """
    errors = _validate_backend_model_selection(
        {"backend_type": "nemospeech", "diarize": True}, None
    )
    assert errors == [
        {
            "field": "backend.diar_model",
            "message": "a diarizer model is required for diarization",
        }
    ]

    # A blank selector is as unusable as a missing one, like model_file.
    errors = _validate_backend_model_selection(
        {"backend_type": "nemospeech", "diarize": True, "diar_model": "   "}, None
    )
    assert errors and errors[0]["field"] == "backend.diar_model"
    assert errors[0]["message"] == "a diarizer model is required for diarization"

    # A real diarizer passes, and diarization off needs none.
    assert (
        _validate_backend_model_selection(
            {
                "backend_type": "nemospeech",
                "diarize": True,
                "diar_model": "sortformer.gguf",
            },
            None,
        )
        == []
    )
    assert (
        _validate_backend_model_selection(
            {"backend_type": "nemospeech", "diarize": False}, None
        )
        == []
    )
    # The rule is NeMo-Speech's own model: whisper.cpp has no diarizer.
    assert (
        _validate_backend_model_selection(
            {"backend_type": "whispercpp", "diarize": True}, None
        )
        == []
    )


def test_a_diarized_nemo_intent_is_grandfathered_when_diarize_is_unchanged():
    """A stored intent must stay editable after the diarizer rule tightens.

    Every update replays the full spec, and a stored diarize=true with no
    diar_model has no diar_model value to compare, so only the flag can
    identify the intent that predates the rule.
    """
    backend = {"backend_type": "nemospeech", "diarize": True}
    errors = _validate_backend_model_selection(backend, None)
    assert [error["field"] for error in errors] == ["backend.diar_model"]
    # An exemption on another field must not hide the missing diarizer.
    assert _validate_backend_model_selection(
        backend, None, exempt_fields=frozenset({"model_file"})
    )
    assert (
        _validate_backend_model_selection(
            backend, None, exempt_fields=frozenset({"diarize"})
        )
        == []
    )


def test_fallback_capabilities_advertise_transcription():
    for backend_type in AUDIO_BACKENDS:
        assert OpenAIGateway._fallback_capabilities(backend_type) == ["transcription"]
    assert OpenAIGateway._fallback_capabilities("huggingface_causal") == ["completion"]


# ── the format list is shared with the host ─────────────────────────


def _tuple_assignment(path: Path, name: str) -> tuple[str, ...]:
    """The literal tuple assigned to ``name`` in a Python source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            assert node.value is not None, name
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple), name
            return value
    raise AssertionError(f"{name} is not assigned in {path}")


def test_the_audio_response_formats_match_the_host():
    """A format added on the host must not be rejected by control first.

    The control venv cannot import the host package, so both source files are
    read: the two lists must stay identical entry for entry.
    """
    control_root = Path(__file__).resolve().parents[1]
    host_root = control_root.parent / "solar-host"

    control_formats = _tuple_assignment(
        control_root / "app" / "models" / "openai.py", "AUDIO_RESPONSE_FORMATS"
    )
    host_formats = _tuple_assignment(
        host_root / "solar_host" / "servers" / "audio_formats.py",
        "RESPONSE_FORMATS",
    )

    assert control_formats == host_formats
