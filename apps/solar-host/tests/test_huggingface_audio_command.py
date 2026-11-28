"""The HuggingFace audio mode: command shape and endpoint list."""

import sys
from types import SimpleNamespace

import pytest

from solar_host.backends.huggingface import HuggingFaceRunner
from solar_host.models.huggingface import HuggingFaceAudioConfig


def _instance(**overrides):
    config = HuggingFaceAudioConfig(
        **{
            "model_id": "/models/whisper-hu",
            "alias": "whisper-hu",
            "device": "cpu",
            "dtype": "float32",
            **overrides,
        }
    )
    return SimpleNamespace(config=config, port=3601, id="inst-1")


@pytest.fixture(autouse=True)
def _deps(monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.huggingface.HuggingFaceRunner.check_dependencies",
        staticmethod(lambda: None),
    )


def test_the_audio_mode_uses_the_audio_model_type():
    cmd = HuggingFaceRunner().build_command(_instance())

    assert cmd[:3] == [sys.executable, "-m", "solar_host.servers.hf_server"]
    assert cmd[cmd.index("--model-type") + 1] == "audio"
    assert cmd[cmd.index("--model-id") + 1] == "/models/whisper-hu"
    assert cmd[cmd.index("--alias") + 1] == "whisper-hu"


def test_the_audio_mode_carries_the_chunk_length_and_batch_size():
    cmd = HuggingFaceRunner().build_command(
        _instance(chunk_length_s=15.0, batch_size=4)
    )
    assert cmd[cmd.index("--chunk-length-s") + 1] == "15.0"
    assert cmd[cmd.index("--batch-size") + 1] == "4"


def test_the_audio_mode_omits_the_causal_only_flags():
    cmd = HuggingFaceRunner().build_command(_instance())
    # No max_length (audio is chunked), no flash attention (text-only knob).
    assert "--max-length" not in cmd
    assert "--use-flash-attention" not in cmd


def test_the_audio_mode_passes_the_language_when_set():
    cmd = HuggingFaceRunner().build_command(_instance(language="hu"))
    assert cmd[cmd.index("--language") + 1] == "hu"


def test_the_audio_mode_omits_the_language_when_unset():
    cmd = HuggingFaceRunner().build_command(_instance())
    assert "--language" not in cmd


def test_the_audio_mode_keeps_trust_remote_code():
    cmd = HuggingFaceRunner().build_command(_instance(trust_remote_code=True))
    assert "--trust-remote-code" in cmd


def test_the_audio_endpoint_list_is_the_transcription_set():
    endpoints = HuggingFaceRunner().get_supported_endpoints_for_type(
        "huggingface_audio"
    )
    assert endpoints == [
        "/v1/audio/transcriptions",
        "/v1/models",
        "/health",
    ]


def test_the_causal_endpoint_list_is_unchanged():
    endpoints = HuggingFaceRunner().get_supported_endpoints_for_type(
        "huggingface_causal"
    )
    assert "/v1/audio/transcriptions" not in endpoints
    assert "/v1/chat/completions" in endpoints


def test_the_shared_endpoint_contract_accepts_the_backend_type():
    """The process manager's shared contract is called with a config's type
    identifier; HuggingFace configs declare a backend_type, so the model-type
    spelling must return the same per-backend endpoint set."""
    endpoints = HuggingFaceRunner().get_supported_endpoints_for_model_type(
        "huggingface_audio"
    )
    assert endpoints == [
        "/v1/audio/transcriptions",
        "/v1/models",
        "/health",
    ]
