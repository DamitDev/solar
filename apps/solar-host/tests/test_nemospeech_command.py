"""Tests for the NeMo-Speech.cpp runner command, readiness and endpoint list."""

import sys
from types import SimpleNamespace

import pytest

from solar_host.backends.nemospeech import NemoSpeechRunner
from solar_host.models.base import InstancePhase
from solar_host.models.nemospeech import NemoSpeechConfig


def _instance(**overrides):
    config = NemoSpeechConfig(
        **{
            "model": "/models/decoder.gguf",
            "alias": "nemotron-stream-en",
            "language": "en",
            "device": "cpu",
            **overrides,
        }
    )
    return SimpleNamespace(config=config, port=3502, id="inst-1")


@pytest.fixture(autouse=True)
def _binary(monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.nemospeech.settings.nemo_speech_binary", ""
    )
    monkeypatch.setattr(
        "solar_host.backends.nemospeech.shutil.which",
        lambda name: "/usr/local/bin/nemo-speech" if name == "nemo-speech" else None,
    )


def test_the_command_spawns_the_shim_with_the_verified_flags():
    cmd = NemoSpeechRunner().build_command(_instance())

    assert cmd[:3] == [
        sys.executable,
        "-m",
        "solar_host.servers.audio_server",
    ]
    assert cmd[3:] == [
        "--model-type",
        "nemospeech",
        "--runtime-binary",
        "/usr/local/bin/nemo-speech",
        "--model",
        "/models/decoder.gguf",
        "--alias",
        "nemotron-stream-en",
        "--host",
        "0.0.0.0",
        "--port",
        "3502",
        "--device",
        "cpu",
        "--runtime-timeout-s",
        "600.0",
        "--request-timeout-s",
        "1800.0",
        "--language",
        "en",
    ]
    assert "--gpu" not in cmd


def test_the_sidecars_are_passed_only_when_set():
    cmd = NemoSpeechRunner().build_command(
        _instance(
            encoder="/models/encoder.gguf",
            diar_model="/models/sortformer.gguf",
            diarize=True,
        )
    )
    assert cmd[cmd.index("--salm-encoder") + 1] == "/models/encoder.gguf"
    assert cmd[cmd.index("--diar-model") + 1] == "/models/sortformer.gguf"
    assert "--diarize" in cmd


def test_the_language_is_omitted_when_unset():
    cmd = NemoSpeechRunner().build_command(_instance(language=None))
    assert "--language" not in cmd


def test_the_runner_raises_when_the_binary_is_missing(monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.nemospeech.shutil.which", lambda name: None
    )
    with pytest.raises(RuntimeError) as excinfo:
        NemoSpeechRunner().build_command(_instance())
    assert "nemo-speech not found" in str(excinfo.value)


def test_the_ready_line_is_the_shims_own_signal():
    runner = NemoSpeechRunner()
    assert runner.is_ready_line("[READY] runtime ready") is True
    assert runner.is_ready_line("Uvicorn running on http://127.0.0.1:9000") is False


def test_a_complete_line_clears_the_busy_state():
    runner = NemoSpeechRunner()
    context = runner.initialize_context()
    started = runner.parse_log_line(
        "inst-1",
        "[REQUEST] model=nemotron-stream-en "
        "endpoint=/v1/audio/transcriptions format=json",
        context,
    )
    assert started is not None and started.busy is True

    finished = runner.parse_log_line(
        "inst-1",
        "[COMPLETE] model=nemotron-stream-en duration_s=11.2 time_ms=850",
        context,
    )
    assert finished is not None and finished.busy is False
    assert finished.phase is InstancePhase.IDLE


def test_the_endpoint_list_is_the_transcription_set():
    runner = NemoSpeechRunner()
    assert runner.get_supported_endpoints_for_model_type("nemospeech") == [
        "/v1/audio/transcriptions",
        "/v1/models",
        "/health",
    ]
