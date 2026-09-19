"""Tests for the vLLM process environment (venv activation, extra env)."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from solar_host.backends.vllm import VllmRunner
from solar_host.models.vllm import VllmConfig


@pytest.fixture
def venv(_hermetic_settings, tmp_path, monkeypatch) -> Path:
    path = tmp_path / "vllm-venv"
    (path / "bin").mkdir(parents=True)
    monkeypatch.setattr("solar_host.config.settings.vllm_venv_path", str(path))
    return path


def build_env(
    instance_id: str = "inst-1", **config_overrides: object
) -> dict[str, str]:
    overrides = dict(config_overrides)
    overrides.setdefault("alias", "glm-5.3-flash:320b")
    config = VllmConfig(model_path="/models/test", **overrides)
    instance = SimpleNamespace(config=config, port=8080, id=instance_id)
    return VllmRunner().build_env(instance)


def test_the_venv_is_activated_through_the_environment(venv) -> None:
    env = build_env()

    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"].startswith(f"{venv / 'bin'}:")
    assert env["PATH"].endswith(os.environ.get("PATH", ""))
    # CPython reads an empty PYTHONHOME as unset, which is what `activate` does.
    assert env["PYTHONHOME"] == ""


def test_no_venv_leaves_the_environment_alone(monkeypatch) -> None:
    monkeypatch.setattr("solar_host.config.settings.vllm_venv_path", "")

    env = build_env()

    assert "VIRTUAL_ENV" not in env
    assert "PATH" not in env


def test_no_per_instance_cache_directory_is_invented(venv) -> None:
    """vLLM keeps its prefix cache in memory — no SGLang-style cache dir."""
    env = build_env()

    assert "SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR" not in env


def test_extra_env_is_passed_through_and_wins_last(venv) -> None:
    env = build_env(
        extra_env={
            "VLLM_USE_FLASHINFER_MOE_FP4": "1",
            "PYTHONHOME": "/somewhere/else",
        }
    )

    assert env["VLLM_USE_FLASHINFER_MOE_FP4"] == "1"
    # An operator's per-instance override beats the automatic block.
    assert env["PYTHONHOME"] == "/somewhere/else"


# ── S-058: CUDA_VISIBLE_DEVICES enforcement ─────────────────────


def _gpu_instance(gpu_ids: list[int] | None) -> SimpleNamespace:
    """An instance carrying a device assignment (or none)."""
    return SimpleNamespace(
        gpu_ids=gpu_ids,
        port=8080,
        id="inst-1",
        config=SimpleNamespace(alias="test-model", extra_env=None),
    )


def test_cuda_visibility_is_emitted_in_the_given_order(venv) -> None:
    env = VllmRunner().build_env(_gpu_instance(gpu_ids=[2, 0]))

    assert env["CUDA_VISIBLE_DEVICES"] == "2,0"
    # F1: the index space is pinned to PCI-bus enumeration.
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"


def test_no_gpu_ids_means_no_visibility_block(venv) -> None:
    env = VllmRunner().build_env(_gpu_instance(gpu_ids=None))

    assert "CUDA_VISIBLE_DEVICES" not in env
    assert "CUDA_DEVICE_ORDER" not in env


def test_extra_env_still_wins_over_the_visibility_block(venv) -> None:
    instance = SimpleNamespace(
        gpu_ids=[0],
        port=8080,
        id="inst-1",
        config=SimpleNamespace(
            alias="test-model",
            extra_env={"CUDA_VISIBLE_DEVICES": "3"},
        ),
    )

    env = VllmRunner().build_env(instance)

    assert env["CUDA_VISIBLE_DEVICES"] == "3"
