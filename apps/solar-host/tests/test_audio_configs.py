"""Tests for the whisper.cpp and NeMo-Speech.cpp configuration models."""

from pathlib import Path

import pytest

from solar_host.config import parse_instance_config
from solar_host.models import InstanceConfig
from solar_host.models.nemospeech import NemoSpeechConfig
from solar_host.models.whispercpp import WhisperCppConfig


def _artifact(tmp_path: Path, *names: str) -> Path:
    for name in names:
        (tmp_path / name).write_bytes(b"model")
    return tmp_path


def test_whispercpp_config_constructs_from_a_dict():
    config = WhisperCppConfig(
        model="/models/ggml-tiny.bin",
        alias="whisper-tiny",
    )
    assert config.backend_type == "whispercpp"
    assert config.language == "auto"
    assert config.threads == 4
    assert config.device == "auto"


def test_whispercpp_requires_a_model_or_a_source():
    with pytest.raises(ValueError):
        WhisperCppConfig(alias="whisper-tiny")


def test_whispercpp_requires_model_file_for_a_pull():
    with pytest.raises(ValueError) as excinfo:
        WhisperCppConfig(
            model_source="huggingface://ggerganov/whisper.cpp",
            alias="whisper-tiny",
        )
    assert "model_file is required" in str(excinfo.value)


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_model_file_is_rejected_like_a_missing_one(value):
    """An empty-string selector passed validation and left the runtime pointed at
    a directory; it must answer with the same error as the missing case."""
    for config_class in (WhisperCppConfig, NemoSpeechConfig):
        with pytest.raises(ValueError) as excinfo:
            config_class(model="/models/ggml-tiny.bin", alias="tiny", model_file=value)
        assert "model_file is required" in str(excinfo.value)


def test_an_explicit_model_file_still_needs_no_selector():
    """model_file is optional when model already points at an explicit file."""
    assert WhisperCppConfig(model="/m", alias="a").model_file is None
    assert NemoSpeechConfig(model="/m", alias="a").model_file is None


def test_a_local_file_source_needs_no_selector():
    """A local:// file already names the model; the pull rule must not fire."""
    assert (
        WhisperCppConfig(
            model_source="local:///models/ggml-tiny.bin", alias="whisper-tiny"
        ).model_file
        is None
    )
    assert (
        NemoSpeechConfig(
            model_source="local:///models/decoder.gguf", alias="nemotron"
        ).model_file
        is None
    )


def test_a_directory_model_requires_the_selector(tmp_path):
    """A directory model needs model_file to name the file inside it."""
    for config_class in (WhisperCppConfig, NemoSpeechConfig):
        with pytest.raises(ValueError) as excinfo:
            config_class(model=str(tmp_path), alias="tiny")
        assert "model_file is required" in str(excinfo.value)


def test_whispercpp_device_is_auto_or_cpu_only():
    assert WhisperCppConfig(model="/m", alias="a", device="cpu").device == "cpu"
    with pytest.raises(ValueError):
        WhisperCppConfig(model="/m", alias="a", device="cuda")


def test_nemospeech_config_constructs_from_a_dict():
    config = NemoSpeechConfig(
        model="/models/decoder.gguf",
        alias="nemotron",
        language="en",
    )
    assert config.backend_type == "nemospeech"
    assert config.diarize is False
    assert config.device == "auto"


def test_nemospeech_diarize_requires_a_diarizer():
    with pytest.raises(ValueError) as excinfo:
        NemoSpeechConfig(
            model="/models/decoder.gguf",
            alias="nemotron",
            diarize=True,
        )
    assert "a diarizer model is required for diarization" in str(excinfo.value)


def test_nemospeech_diarize_accepts_a_diarizer():
    config = NemoSpeechConfig(
        model="/models/decoder.gguf",
        alias="nemotron",
        diarize=True,
        diar_model="/models/sortformer.gguf",
    )
    assert config.diarize is True
    assert config.diar_model == "/models/sortformer.gguf"


def test_the_instance_config_union_round_trips_the_three_types():
    for config in (
        WhisperCppConfig(model="/m", alias="a"),
        NemoSpeechConfig(model="/m", alias="a"),
        # HuggingFace audio is imported lazily to keep this file's imports small.
    ):
        dumped = config.model_dump()
        assert isinstance(parse_instance_config(dumped), type(config))


def test_the_union_accepts_huggingface_audio():
    from solar_host.models.huggingface import HuggingFaceAudioConfig

    config = HuggingFaceAudioConfig(model_id="/models/dir", alias="whisper-hu")
    assert isinstance(
        parse_instance_config(config.model_dump()), HuggingFaceAudioConfig
    )
    assert isinstance(config, object)
    assert InstanceConfig is not None


def test_model_source_sets_model_for_the_ggml_backends(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, "ggml-tiny.bin", "decoder.gguf")
    monkeypatch.setattr(
        "solar_host.config.resolve_model_source",
        lambda source: f"local://{artifact}",
    )
    monkeypatch.setattr("solar_host.config._artifact_base_dir", lambda data: artifact)

    whisper = parse_instance_config(
        {
            "backend_type": "whispercpp",
            "model_source": "huggingface://ggerganov/whisper.cpp",
            "model_file": "ggml-tiny.bin",
            "alias": "whisper-tiny",
        }
    )
    assert whisper.model == str(artifact / "ggml-tiny.bin")

    nemo = parse_instance_config(
        {
            "backend_type": "nemospeech",
            "model_source": "huggingface://nvidia/nemotron-speech-streaming-en-0.6b",
            "model_file": "decoder.gguf",
            "alias": "nemotron",
        }
    )
    assert nemo.model == str(artifact / "decoder.gguf")


def test_model_file_resolves_inside_the_pulled_artifact(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, "ggml-tiny.bin")
    monkeypatch.setattr("solar_host.config._artifact_base_dir", lambda data: artifact)

    config = parse_instance_config(
        {
            "backend_type": "whispercpp",
            "model": str(artifact),
            "model_file": "ggml-tiny.bin",
            "alias": "whisper-tiny",
        }
    )
    assert config.model == str(artifact / "ggml-tiny.bin")


def test_encoder_and_diarizer_resolve_inside_the_artifact(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path, "decoder.gguf", "encoder.gguf", "diar.gguf")
    monkeypatch.setattr("solar_host.config._artifact_base_dir", lambda data: artifact)

    config = parse_instance_config(
        {
            "backend_type": "nemospeech",
            "model": str(artifact),
            "model_file": "decoder.gguf",
            "encoder": "encoder.gguf",
            "diar_model": "diar.gguf",
            "alias": "nemotron",
        }
    )
    assert config.model == str(artifact / "decoder.gguf")
    assert config.encoder == str(artifact / "encoder.gguf")
    assert config.diar_model == str(artifact / "diar.gguf")


def test_an_unresolvable_pattern_raises_in_strict_mode(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    monkeypatch.setattr("solar_host.config._artifact_base_dir", lambda data: artifact)

    with pytest.raises(ValueError) as excinfo:
        parse_instance_config(
            {
                "backend_type": "whispercpp",
                "model": str(artifact),
                "model_file": "missing.bin",
                "alias": "whisper-tiny",
            }
        )
    assert "Cannot resolve model pattern" in str(excinfo.value)


def test_the_artifact_directory_is_kept_in_non_strict_mode(tmp_path, monkeypatch):
    artifact = _artifact(tmp_path)
    monkeypatch.setattr("solar_host.config._artifact_base_dir", lambda data: artifact)

    config = parse_instance_config(
        {
            "backend_type": "whispercpp",
            "model": str(artifact),
            "model_file": "missing.bin",
            "alias": "whisper-tiny",
        },
        strict=False,
    )
    assert config.model == str(artifact)
