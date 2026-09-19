"""Tests for vLLM command construction."""

from types import SimpleNamespace

import pytest

from solar_host.backends.vllm import VllmRunner
from solar_host.models.vllm import VllmConfig


@pytest.fixture(autouse=True)
def _vllm_available(tmp_path, monkeypatch):
    """Pretend this host is an NVIDIA box with vLLM in a venv."""
    venv = tmp_path / "vllm-venv"
    (venv / "bin").mkdir(parents=True)
    console_script = venv / "bin" / "vllm"
    console_script.write_text("#!/bin/sh\n")
    console_script.chmod(0o755)

    monkeypatch.setattr("solar_host.config.settings.vllm_venv_path", str(venv))
    monkeypatch.setattr("solar_host.config.settings.api_key", "test-key")
    monkeypatch.setattr(
        "solar_host.backends.vllm.detect_gpu_type", lambda: "nvidia_cuda"
    )
    return venv


def build_command(**config_overrides: object) -> list[str]:
    config = VllmConfig(model_path="/models/test", alias="test", **config_overrides)
    instance = SimpleNamespace(config=config, port=8080, id="inst-1")
    return VllmRunner().build_command(instance)


def flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_host_managed_flags_come_from_the_host_not_the_config() -> None:
    command = build_command()

    assert command[1] == "serve"
    assert command[2] == "/models/test"
    assert flag_value(command, "--served-model-name") == "test"
    assert flag_value(command, "--host") == "0.0.0.0"
    assert flag_value(command, "--port") == "8080"
    assert flag_value(command, "--api-key") == "test-key"


def test_a_colon_alias_is_served_verbatim() -> None:
    """vLLM does no `:` parsing — registry lookup is exact-match, so an alias
    like glm-5.3-flash:320b is served under exactly that name; control's
    translation is a no-op for direct requests."""
    config = VllmConfig(model_path="/models/glm", alias="glm-5.3-flash:320b")
    instance = SimpleNamespace(config=config, port=8080, id="inst-1")
    runner = VllmRunner()

    command = runner.build_command(instance)

    assert flag_value(command, "--served-model-name") == "glm-5.3-flash:320b"
    assert runner.get_served_model_name(config) == "glm-5.3-flash:320b"


def test_the_venv_console_script_is_used(_vllm_available) -> None:
    command = build_command()

    assert command[0] == str(_vllm_available / "bin" / "vllm")


def test_the_venv_interpreter_is_the_fallback_entry_point(
    tmp_path, monkeypatch
) -> None:
    venv = tmp_path / "other-venv"
    (venv / "bin").mkdir(parents=True)
    interpreter = venv / "bin" / "python"
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    monkeypatch.setattr("solar_host.config.settings.vllm_venv_path", str(venv))

    command = build_command()

    assert command[:3] == [str(interpreter), "-m", "vllm.entrypoints.cli.main"]


def test_an_unresolvable_executable_names_the_setting(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "solar_host.config.settings.vllm_venv_path", str(tmp_path / "missing")
    )

    with pytest.raises(RuntimeError, match="VLLM_VENV_PATH"):
        build_command()


def test_a_non_nvidia_host_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setattr("solar_host.backends.vllm.detect_gpu_type", lambda: "cpu")

    with pytest.raises(RuntimeError, match="NVIDIA"):
        build_command()


def test_omitted_optional_fields_add_no_flags() -> None:
    command = build_command()

    for flag in (
        "--tensor-parallel-size",
        "--pipeline-parallel-size",
        "--max-model-len",
        "--gpu-memory-utilization",
        "--max-num-seqs",
        "--max-num-batched-tokens",
        "--dtype",
        "--quantization",
        "--kv-cache-dtype",
        "--moe-backend",
        "--speculative-config",
        "--tool-call-parser",
        "--reasoning-parser",
        "--trust-remote-code",
        "--enforce-eager",
        "--enable-auto-tool-choice",
        "--enable-prefix-caching",
        "--no-enable-prefix-caching",
    ):
        assert flag not in command


def test_booleans_are_bare_flags() -> None:
    command = build_command(
        trust_remote_code=True, enforce_eager=True, enable_auto_tool_choice=True
    )

    for flag in (
        "--trust-remote-code",
        "--enforce-eager",
        "--enable-auto-tool-choice",
    ):
        assert flag in command
        # A bare flag must not be followed by a value.
        assert command[command.index(flag) + 1].startswith("--")


def test_prefix_caching_is_tri_state() -> None:
    assert "--enable-prefix-caching" not in build_command()
    assert "--no-enable-prefix-caching" not in build_command()

    enabled = build_command(enable_prefix_caching=True)
    assert "--enable-prefix-caching" in enabled
    assert "--no-enable-prefix-caching" not in enabled

    disabled = build_command(enable_prefix_caching=False)
    assert "--no-enable-prefix-caching" in disabled
    assert "--enable-prefix-caching" not in disabled


def test_the_example_config_maps_to_the_expected_flags() -> None:
    """The config the vLLM backend was designed against, flag for flag."""
    command = build_command(
        tensor_parallel_size=4,
        pipeline_parallel_size=2,
        max_model_len=1048576,
        gpu_memory_utilization=0.9,
        max_num_seqs=64,
        max_num_batched_tokens=8192,
        dtype="bfloat16",
        quantization="fp8",
        kv_cache_dtype="fp8",
        moe_backend="cutlass",
        speculative_config='{"method": "mtp", "num_speculative_tokens": 5}',
        tool_call_parser="hermes",
        reasoning_parser="deepseek_r1",
        trust_remote_code=True,
        enforce_eager=True,
        enable_auto_tool_choice=True,
        enable_prefix_caching=False,
    )

    assert flag_value(command, "--tensor-parallel-size") == "4"
    assert flag_value(command, "--pipeline-parallel-size") == "2"
    assert flag_value(command, "--max-model-len") == "1048576"
    assert flag_value(command, "--gpu-memory-utilization") == "0.9"
    assert flag_value(command, "--max-num-seqs") == "64"
    assert flag_value(command, "--max-num-batched-tokens") == "8192"
    assert flag_value(command, "--dtype") == "bfloat16"
    assert flag_value(command, "--quantization") == "fp8"
    assert flag_value(command, "--kv-cache-dtype") == "fp8"
    assert flag_value(command, "--moe-backend") == "cutlass"
    assert (
        flag_value(command, "--speculative-config")
        == '{"method":"mtp","num_speculative_tokens":5}'
    )
    assert flag_value(command, "--tool-call-parser") == "hermes"
    assert flag_value(command, "--reasoning-parser") == "deepseek_r1"
    assert "--trust-remote-code" in command
    assert "--enforce-eager" in command
    assert "--enable-auto-tool-choice" in command
    assert "--no-enable-prefix-caching" in command


def test_prompt_token_details_are_always_requested_exactly_once() -> None:
    """The usage accounting needs the cached split in served responses."""
    command = build_command()

    assert command.count("--enable-prompt-tokens-details") == 1


def test_extra_args_come_last_so_an_override_wins() -> None:
    command = build_command(
        tensor_parallel_size=2, extra_args=["--tensor-parallel-size", "4"]
    )

    assert command[-2:] == ["--tensor-parallel-size", "4"]
    assert command.count("--tensor-parallel-size") == 2


def test_extra_args_reject_host_managed_flags() -> None:
    for reserved in (
        ["--port", "9999"],
        ["--api-key=leaked"],
        ["--served-model-name", "other"],
        ["--model", "/elsewhere"],
        ["--host", "1.2.3.4"],
        ["--enable-prompt-tokens-details"],
        ["--no-enable-prompt-tokens-details"],
        ["--disable-log-stats"],
    ):
        with pytest.raises(ValueError, match="managed by solar-host"):
            VllmConfig(model_path="/models/test", alias="test", extra_args=reserved)


def test_speculative_config_is_stored_as_canonical_json() -> None:
    config = VllmConfig(
        model_path="/models/test",
        alias="test",
        speculative_config='{ "method": "mtp", "num_speculative_tokens": 5 }',
    )

    assert config.speculative_config == '{"method":"mtp","num_speculative_tokens":5}'


def test_speculative_config_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        VllmConfig(
            model_path="/models/test",
            alias="test",
            speculative_config="{not json}",
        )


def test_speculative_config_rejects_a_non_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        VllmConfig(model_path="/models/test", alias="test", speculative_config="[1, 2]")


def test_a_config_needs_a_model_path_or_source() -> None:
    with pytest.raises(ValueError, match="model_path"):
        VllmConfig(alias="test")
