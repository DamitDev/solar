"""Tests for SGLang command construction."""

from types import SimpleNamespace

import pytest

from solar_host.backends.sglang import SglangRunner
from solar_host.models.sglang import SglangConfig


@pytest.fixture(autouse=True)
def _sglang_available(tmp_path, monkeypatch):
    """Pretend this host is an NVIDIA box with SGLang in a venv."""
    venv = tmp_path / "sglang-venv"
    (venv / "bin").mkdir(parents=True)
    console_script = venv / "bin" / "sglang"
    console_script.write_text("#!/bin/sh\n")
    console_script.chmod(0o755)

    monkeypatch.setattr("solar_host.config.settings.sglang_venv_path", str(venv))
    monkeypatch.setattr("solar_host.config.settings.sglang_prompt_cache_dir", "")
    monkeypatch.setattr("solar_host.config.settings.api_key", "test-key")
    monkeypatch.setattr(
        "solar_host.backends.sglang.detect_gpu_type", lambda: "nvidia_cuda"
    )
    return venv


def build_command(**config_overrides: object) -> list[str]:
    config = SglangConfig(model_path="/models/test", alias="test", **config_overrides)
    instance = SimpleNamespace(config=config, port=8080, id="inst-1")
    return SglangRunner().build_command(instance)


def flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_host_managed_flags_come_from_the_host_not_the_config() -> None:
    command = build_command()

    assert command[1] == "serve"
    assert flag_value(command, "--model-path") == "/models/test"
    assert flag_value(command, "--served-model-name") == "test"
    assert flag_value(command, "--port") == "8080"
    assert flag_value(command, "--api-key") == "test-key"


def test_a_colon_in_the_alias_is_translated_for_sglang() -> None:
    """SGLang reads `a:b` as base model `a` plus LoRA adapter `b`, so an alias
    like deepseek-v4-flash:284b cannot be served verbatim."""
    config = SglangConfig(model_path="/models/dsv4", alias="deepseek-v4-flash:284b")
    instance = SimpleNamespace(config=config, port=8080, id="inst-1")
    runner = SglangRunner()

    command = runner.build_command(instance)

    assert flag_value(command, "--served-model-name") == "deepseek-v4-flash-284b"
    # Reported to solar-control, which rewrites the request's model field to it.
    assert runner.get_served_model_name(config) == "deepseek-v4-flash-284b"


def test_the_venv_console_script_is_used(_sglang_available) -> None:
    command = build_command()

    assert command[0] == str(_sglang_available / "bin" / "sglang")


def test_the_venv_interpreter_is_the_fallback_entry_point(
    tmp_path, monkeypatch
) -> None:
    venv = tmp_path / "other-venv"
    (venv / "bin").mkdir(parents=True)
    interpreter = venv / "bin" / "python"
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    monkeypatch.setattr("solar_host.config.settings.sglang_venv_path", str(venv))

    command = build_command()

    assert command[:3] == [str(interpreter), "-m", "sglang.launch_server"]


def test_an_unresolvable_executable_names_the_setting(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "solar_host.config.settings.sglang_venv_path", str(tmp_path / "missing")
    )

    with pytest.raises(RuntimeError, match="SGLANG_VENV_PATH"):
        build_command()


def test_a_non_nvidia_host_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setattr("solar_host.backends.sglang.detect_gpu_type", lambda: "cpu")

    with pytest.raises(RuntimeError, match="NVIDIA"):
        build_command()


def test_omitted_optional_fields_add_no_flags() -> None:
    command = build_command()

    for flag in (
        "--tp-size",
        "--dtype",
        "--quantization",
        "--enable-hierarchical-cache",
        "--trust-remote-code",
    ):
        assert flag not in command


def test_booleans_are_bare_flags() -> None:
    command = build_command(trust_remote_code=True, enable_hierarchical_cache=True)

    assert "--trust-remote-code" in command
    assert "--enable-hierarchical-cache" in command
    # A bare flag must not be followed by a value.
    assert command[command.index("--trust-remote-code") + 1].startswith("--")


def test_the_example_config_maps_to_the_expected_flags(monkeypatch, tmp_path) -> None:
    """The config the SGLang backend was designed against, flag for flag."""
    cache_root = tmp_path / "prompt-cache"
    monkeypatch.setattr(
        "solar_host.config.settings.sglang_prompt_cache_dir", str(cache_root)
    )

    command = build_command(
        tp_size=8,
        context_length=163840,
        mem_fraction_static=0.83,
        chunked_prefill_size=16384,
        max_running_requests=32,
        cuda_graph_max_bs=32,
        dtype="bfloat16",
        quantization="fp8",
        kv_cache_dtype="fp8_e4m3",
        moe_runner_backend="flashinfer_mxfp4",
        trust_remote_code=True,
        enable_hierarchical_cache=True,
        hicache_ratio=30,
        hicache_mem_layout="page_first_direct",
        hicache_io_backend="direct",
        hicache_storage_backend="file",
        hicache_storage_backend_extra_config='{"max_size": "256G"}',
        hicache_storage_prefetch_policy="wait_complete",
    )

    assert flag_value(command, "--tp-size") == "8"
    assert flag_value(command, "--context-length") == "163840"
    assert flag_value(command, "--mem-fraction-static") == "0.83"
    assert flag_value(command, "--chunked-prefill-size") == "16384"
    assert flag_value(command, "--max-running-requests") == "32"
    assert flag_value(command, "--cuda-graph-max-bs") == "32"
    assert flag_value(command, "--dtype") == "bfloat16"
    assert flag_value(command, "--quantization") == "fp8"
    assert flag_value(command, "--kv-cache-dtype") == "fp8_e4m3"
    assert flag_value(command, "--moe-runner-backend") == "flashinfer_mxfp4"
    assert flag_value(command, "--hicache-ratio") == "30.0"
    assert flag_value(command, "--hicache-mem-layout") == "page_first_direct"
    assert flag_value(command, "--hicache-io-backend") == "direct"
    assert flag_value(command, "--hicache-storage-backend") == "file"
    assert (
        flag_value(command, "--hicache-storage-backend-extra-config")
        == '{"max_size":"256G"}'
    )
    assert flag_value(command, "--hicache-storage-prefetch-policy") == "wait_complete"
    assert "--trust-remote-code" in command
    assert "--enable-hierarchical-cache" in command


def test_storage_flags_are_dropped_without_a_host_cache_root() -> None:
    command = build_command(
        enable_hierarchical_cache=True,
        hicache_ratio=30,
        hicache_storage_backend="file",
        hicache_storage_prefetch_policy="wait_complete",
    )

    # In-memory hierarchical caching still works, only the persistence goes.
    assert "--enable-hierarchical-cache" in command
    assert flag_value(command, "--hicache-ratio") == "30.0"
    assert "--hicache-storage-backend" not in command
    assert "--hicache-storage-prefetch-policy" not in command


def test_extra_args_come_last_so_an_override_wins() -> None:
    command = build_command(
        tp_size=2, extra_args=["--tp-size", "4", "--enable-metrics"]
    )

    assert command[-3:] == ["--tp-size", "4", "--enable-metrics"]
    assert command.count("--tp-size") == 2


def test_extra_args_reject_host_managed_flags() -> None:
    with pytest.raises(ValueError, match="managed by solar-host"):
        SglangConfig(
            model_path="/models/test", alias="test", extra_args=["--port", "9999"]
        )

    with pytest.raises(ValueError, match="managed by solar-host"):
        SglangConfig(
            model_path="/models/test", alias="test", extra_args=["--api-key=leaked"]
        )


def test_storage_extra_config_is_stored_as_canonical_json() -> None:
    config = SglangConfig(
        model_path="/models/test",
        alias="test",
        hicache_storage_backend_extra_config='{ "max_size": "256G", "eviction_ratio": 0.9 }',
    )

    assert (
        config.hicache_storage_backend_extra_config
        == '{"max_size":"256G","eviction_ratio":0.9}'
    )


def test_storage_extra_config_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        SglangConfig(
            model_path="/models/test",
            alias="test",
            hicache_storage_backend_extra_config="{not json}",
        )


def test_a_config_needs_a_model_path_or_source() -> None:
    with pytest.raises(ValueError, match="model_path"):
        SglangConfig(alias="test")
