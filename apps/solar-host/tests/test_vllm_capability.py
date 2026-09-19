"""Backend advertisement: vLLM only when this host can actually run it."""

import pytest

from solar_host.backends import supported_backend_types


@pytest.fixture
def vllm_installed(tmp_path, monkeypatch):
    venv = tmp_path / "vllm-venv"
    (venv / "bin").mkdir(parents=True)
    console_script = venv / "bin" / "vllm"
    console_script.write_text("#!/bin/sh\n")
    console_script.chmod(0o755)
    monkeypatch.setattr("solar_host.config.settings.vllm_venv_path", str(venv))
    return venv


def test_advertised_on_an_nvidia_host_with_vllm(vllm_installed, monkeypatch):
    monkeypatch.setattr(
        "solar_host.backends.vllm.detect_gpu_type", lambda: "nvidia_cuda"
    )

    assert "vllm" in supported_backend_types()


def test_not_advertised_on_a_non_nvidia_host(vllm_installed, monkeypatch):
    monkeypatch.setattr("solar_host.backends.vllm.detect_gpu_type", lambda: "apple_mps")

    assert "vllm" not in supported_backend_types()


def test_not_advertised_without_the_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "solar_host.config.settings.vllm_venv_path", str(tmp_path / "missing")
    )
    monkeypatch.setattr(
        "solar_host.backends.vllm.detect_gpu_type", lambda: "nvidia_cuda"
    )

    assert "vllm" not in supported_backend_types()


def test_the_bundled_backends_are_always_advertised(monkeypatch):
    monkeypatch.setattr("solar_host.config.settings.vllm_venv_path", "")
    monkeypatch.setattr("solar_host.backends.vllm.detect_gpu_type", lambda: "cpu")

    backends = supported_backend_types()

    assert "llamacpp" in backends
    assert "huggingface_causal" in backends
