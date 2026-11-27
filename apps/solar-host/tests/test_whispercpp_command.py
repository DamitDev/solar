"""Tests for the whisper.cpp runner command, readiness and endpoint list."""

import sys
from types import SimpleNamespace

import pytest

from solar_host.backends import whispercpp
from solar_host.backends.whispercpp import WhisperCppRunner
from solar_host.models.base import InstancePhase
from solar_host.models.whispercpp import WhisperCppConfig


def _instance(device: str = "auto", threads: int = 8):
    config = WhisperCppConfig(
        model="/models/ggml-tiny.bin",
        alias="whisper-tiny",
        language="en",
        threads=threads,
        device=device,
    )
    return SimpleNamespace(config=config, port=3501, id="inst-1")


@pytest.fixture(autouse=True)
def _binary(monkeypatch):
    """Pretend the binary resolves, without bypassing the resolver itself."""
    monkeypatch.setattr("solar_host.backends.whispercpp.settings.whispercpp_binary", "")
    monkeypatch.setattr(
        "solar_host.backends.whispercpp.shutil.which",
        lambda name: (
            "/opt/whisper.cpp/build/bin/whisper-server"
            if name == "whisper-server"
            else None
        ),
    )


def test_the_command_spawns_the_shim_with_the_verified_flags():
    cmd = WhisperCppRunner().build_command(_instance())

    assert cmd[:3] == [
        sys.executable,
        "-m",
        "solar_host.servers.audio_server",
    ]
    assert cmd[3:] == [
        "--model-type",
        "whispercpp",
        "--runtime-binary",
        "/opt/whisper.cpp/build/bin/whisper-server",
        "--model",
        "/models/ggml-tiny.bin",
        "--alias",
        "whisper-tiny",
        "--host",
        "0.0.0.0",
        "--port",
        "3501",
        "--language",
        "en",
        "--threads",
        "8",
        "--device",
        "auto",
        "--runtime-timeout-s",
        "600.0",
        "--request-timeout-s",
        "1800.0",
    ]
    assert "--api-key" not in cmd


def test_the_command_carries_the_cpu_device_choice():
    cmd = WhisperCppRunner().build_command(_instance(device="cpu"))
    assert cmd[cmd.index("--device") + 1] == "cpu"


def test_the_runner_raises_when_the_binary_is_missing(monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.whispercpp.shutil.which", lambda name: None
    )
    with pytest.raises(RuntimeError) as excinfo:
        WhisperCppRunner().build_command(_instance())
    assert "whisper-server not found" in str(excinfo.value)


def test_the_configured_binary_wins(monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.whispercpp.settings.whispercpp_binary",
        "/custom/whisper-server",
    )
    monkeypatch.setattr(
        "solar_host.backends.whispercpp.shutil.which",
        lambda path: path if path == "/custom/whisper-server" else None,
    )
    assert whispercpp.resolve_executable() == "/custom/whisper-server"


def test_the_ready_line_is_the_shims_own_signal():
    runner = WhisperCppRunner()
    assert runner.is_ready_line("[READY] runtime ready") is True
    assert runner.is_ready_line("Uvicorn running on http://127.0.0.1:9000") is False
    assert (
        runner.is_ready_line("whisper server listening at http://127.0.0.1:1") is False
    )


def test_a_complete_line_clears_the_busy_state():
    runner = WhisperCppRunner()
    context = runner.initialize_context()
    started = runner.parse_log_line(
        "inst-1",
        "[REQUEST] model=whisper-tiny endpoint=/v1/audio/transcriptions "
        "format=verbose_json",
        context,
    )
    assert started is not None and started.busy is True
    assert started.phase is InstancePhase.GENERATING

    finished = runner.parse_log_line(
        "inst-1",
        "[COMPLETE] model=whisper-tiny duration_s=11.2 time_ms=850",
        context,
    )
    assert finished is not None and finished.busy is False
    assert finished.phase is InstancePhase.IDLE


def test_the_hf_token_regex_is_not_reused():
    runner = WhisperCppRunner()
    context = runner.initialize_context()
    assert (
        runner.parse_log_line(
            "inst-1", "[COMPLETE] model=x tokens=12 time_ms=8", context
        )
        is None
    )


def test_the_endpoint_list_is_the_transcription_set():
    runner = WhisperCppRunner()
    assert runner.get_supported_endpoints_for_model_type("whispercpp") == [
        "/v1/audio/transcriptions",
        "/v1/models",
        "/health",
    ]
    assert runner.get_supported_endpoints() == [
        "/v1/audio/transcriptions",
        "/v1/models",
        "/health",
    ]
