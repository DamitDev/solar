"""Tests for the SGLang process environment (venv activation, prompt cache)."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from solar_host.backends.sglang import HICACHE_STORAGE_DIR_ENV, SglangRunner
from solar_host.models.sglang import SglangConfig


@pytest.fixture
def venv(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "sglang-venv"
    (path / "bin").mkdir(parents=True)
    monkeypatch.setattr("solar_host.config.settings.sglang_venv_path", str(path))
    monkeypatch.setattr("solar_host.config.settings.sglang_prompt_cache_dir", "")
    return path


def build_env(
    instance_id: str = "inst-1", **config_overrides: object
) -> dict[str, str]:
    config = SglangConfig(
        model_path="/models/test", alias="deepseek:flash", **config_overrides
    )
    instance = SimpleNamespace(config=config, port=8080, id=instance_id)
    return SglangRunner().build_env(instance)


def test_the_venv_is_activated_through_the_environment(venv) -> None:
    env = build_env()

    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"].startswith(f"{venv / 'bin'}:")
    assert env["PATH"].endswith(os.environ.get("PATH", ""))
    # CPython reads an empty PYTHONHOME as unset, which is what `activate` does.
    assert env["PYTHONHOME"] == ""


def test_no_venv_leaves_the_environment_alone(monkeypatch) -> None:
    monkeypatch.setattr("solar_host.config.settings.sglang_venv_path", "")
    monkeypatch.setattr("solar_host.config.settings.sglang_prompt_cache_dir", "")

    env = build_env()

    assert "VIRTUAL_ENV" not in env
    assert "PATH" not in env


def test_each_instance_gets_its_own_prompt_cache_directory(
    venv, tmp_path, monkeypatch
) -> None:
    cache_root = tmp_path / "prompt-cache"
    monkeypatch.setattr(
        "solar_host.config.settings.sglang_prompt_cache_dir", str(cache_root)
    )

    first = build_env("inst-1")
    second = build_env("inst-2")

    # The alias is part of the name for legibility, with ':' made path-safe.
    assert first[HICACHE_STORAGE_DIR_ENV] == str(cache_root / "deepseek-flash-inst-1")
    assert second[HICACHE_STORAGE_DIR_ENV] == str(cache_root / "deepseek-flash-inst-2")
    assert Path(first[HICACHE_STORAGE_DIR_ENV]).is_dir()
    assert Path(second[HICACHE_STORAGE_DIR_ENV]).is_dir()


def test_no_cache_root_means_no_cache_variable(venv) -> None:
    assert HICACHE_STORAGE_DIR_ENV not in build_env()


def test_extra_env_is_passed_through(venv) -> None:
    env = build_env(extra_env={"SGLANG_DSV4_COMPRESS_STATE_DTYPE": "bf16"})

    assert env["SGLANG_DSV4_COMPRESS_STATE_DTYPE"] == "bf16"


def test_other_runners_contribute_no_environment() -> None:
    """The base hook stays empty so existing backends are untouched."""
    from solar_host.backends.llamacpp import LlamaCppRunner

    instance = SimpleNamespace(config=None, port=8080, id="inst-1")
    assert LlamaCppRunner().build_env(instance) == {}
