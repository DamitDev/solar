"""Availability and runner factory coverage for the audio backends."""

from solar_host.backends import supported_backend_types
from solar_host.backends.nemospeech import NemoSpeechRunner
from solar_host.backends.whispercpp import WhisperCppRunner
from solar_host.models.huggingface import HuggingFaceAudioConfig
from solar_host.models.nemospeech import NemoSpeechConfig
from solar_host.models.whispercpp import WhisperCppConfig
from solar_host.process_manager import get_runner_for_config


def test_a_missing_binary_hides_the_backend(monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.whispercpp.resolve_executable", lambda: None
    )
    monkeypatch.setattr(
        "solar_host.backends.nemospeech.resolve_executable", lambda: None
    )

    advertised = supported_backend_types()

    assert "whispercpp" not in advertised
    assert "nemospeech" not in advertised
    # The always-shipped backends stay advertised.
    assert "llamacpp" in advertised
    assert "huggingface_causal" in advertised


def test_a_present_binary_advertises_the_backend(monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.whispercpp.resolve_executable",
        lambda: "/opt/whisper-server",
    )
    monkeypatch.setattr(
        "solar_host.backends.nemospeech.resolve_executable",
        lambda: "/usr/local/bin/nemo-speech",
    )

    advertised = supported_backend_types()

    assert "whispercpp" in advertised
    assert "nemospeech" in advertised


def test_the_runner_factory_covers_the_three_audio_types():
    whisper = WhisperCppConfig(model="/m", alias="a")
    nemo = NemoSpeechConfig(model="/m", alias="a")
    audio = HuggingFaceAudioConfig(model_id="/m", alias="a")

    assert isinstance(get_runner_for_config(whisper), WhisperCppRunner)
    assert isinstance(get_runner_for_config(nemo), NemoSpeechRunner)
    # HuggingFace audio keeps the shared HF runner.
    assert type(get_runner_for_config(audio)).__name__ == "HuggingFaceRunner"


def test_the_backend_types_are_enum_members():
    from solar_host.models.base import BackendType

    assert BackendType.WHISPERCPP.value == "whispercpp"
    assert BackendType.NEMOSPEECH.value == "nemospeech"
    assert BackendType.HUGGINGFACE_AUDIO.value == "huggingface_audio"
