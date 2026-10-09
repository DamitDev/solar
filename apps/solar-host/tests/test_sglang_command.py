"""Tests for SGLang command construction."""

import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from solar_host.backends.sglang import SglangRunner, write_key_config
from solar_host.config import config_manager
from solar_host.models.base import Instance, InstanceStatus
from solar_host.models.sglang import SglangConfig
from solar_host.process_manager import ProcessManager


class _SleepingSglangRunner(SglangRunner):
    """SGLang runner whose command never reports readiness.

    The command is a real subprocess that sleeps, so a start parks until
    the readiness timeout fires with a live process — the timeout path
    then runs against a real key config file. ``build_command`` mirrors
    the production sequence: ``write_key_config`` writes the file and the
    command carries its path.
    """

    def build_command(self, instance) -> list[str]:
        key_config = write_key_config(instance.id)
        cmd = [sys.executable, "-u", "-c", "import time; time.sleep(60)"]
        if key_config is not None:
            cmd += ["--config", str(key_config)]
        return cmd


def _make_instance(
    instance_id: str = "inst-1", status=InstanceStatus.STOPPED
) -> Instance:
    instance = Instance(
        id=instance_id,
        config=SglangConfig(model_path="/models/test", alias="test"),
        status=status,
    )
    config_manager.add_instance(instance)
    return instance


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
    # IT Sec #97: the key travels via the environment, never argv.
    assert "--api-key" not in command


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
        tp_size=2, extra_args=["--tp-size", "4", "--schedule-policy"]
    )

    assert command[-3:] == ["--tp-size", "4", "--schedule-policy"]
    assert command.count("--tp-size") == 2
    # --enable-metrics is host-managed; see RESERVED_SGLANG_ARGS.
    assert command.count("--enable-metrics") == 1


def test_extra_args_reject_host_managed_flags() -> None:
    with pytest.raises(ValueError, match="managed by solar-host"):
        SglangConfig(
            model_path="/models/test", alias="test", extra_args=["--port", "9999"]
        )

    with pytest.raises(ValueError, match="managed by solar-host"):
        SglangConfig(
            model_path="/models/test", alias="test", extra_args=["--api-key=leaked"]
        )

    with pytest.raises(ValueError, match="managed by solar-host"):
        SglangConfig(
            model_path="/models/test", alias="test", extra_args=["--config=x.yaml"]
        )


class TestKeyConfigFile:
    """IT Sec #97: the key reaches SGLang through a 0600 YAML config file.

    SGLang never reads SGLANG_API_KEY, so the file is the only argv-free
    delivery path; ``--config`` merges the file's values into an in-memory
    argument list, so ``ps`` only shows the path.
    """

    def test_the_command_carries_the_config_path_and_no_key_material(
        self, _sglang_available
    ) -> None:
        command = build_command()

        config_path = flag_value(command, "--config")
        assert config_path.endswith("sglang-inst-1.yaml")
        assert "test-key" not in command
        assert "--api-key" not in command

    def test_the_file_has_0600_mode_in_a_0700_dir_and_yaml_loads_to_the_key(
        self, _sglang_available, monkeypatch
    ) -> None:
        import yaml

        from solar_host.backends.sglang import key_config_path

        command = build_command()
        path = Path(flag_value(command, "--config"))
        assert path == key_config_path("inst-1")

        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.parent.name.startswith("solar-host-")

        loaded = yaml.safe_load(path.read_text())
        assert loaded == {"api-key": "test-key"}

    def test_on_process_ready_and_stopped_delete_the_file(
        self, _sglang_available, monkeypatch
    ) -> None:
        from solar_host.backends.sglang import key_config_path

        build_command()
        path = key_config_path("inst-1")
        assert path.exists()

        runner = SglangRunner()
        runner.on_process_ready("inst-1")
        assert not path.exists()

        # Rebuild the file and go through the stop path.
        SglangRunner().build_command(
            SimpleNamespace(
                config=SglangConfig(model_path="/models/test", alias="test"),
                port=8080,
                id="inst-1",
            )
        )
        assert key_config_path("inst-1").exists()
        runner.on_process_stopped("inst-1", {})
        assert not key_config_path("inst-1").exists()

    def test_the_boot_sweep_removes_leftovers(self, monkeypatch) -> None:
        from solar_host.backends.sglang import key_config_path, sweep_key_configs

        stale = key_config_path("inst-gone")
        stale.write_text("api-key: leaked\n")
        unrelated = stale.parent / "unrelated.yaml"
        unrelated.write_text("api-key: leaked\n")

        sweep_key_configs()

        assert not stale.exists()
        assert unrelated.exists()

    def test_nothing_is_written_when_api_key_is_empty(
        self, _sglang_available, monkeypatch
    ) -> None:
        monkeypatch.setattr("solar_host.config.settings.api_key", "")

        command = build_command()

        assert "--config" not in command

    def test_a_second_write_replaces_the_leftover(self, _sglang_available) -> None:
        from solar_host.backends.sglang import key_config_path

        build_command()
        build_command()

        import yaml

        loaded = yaml.safe_load(key_config_path("inst-1").read_text())
        assert loaded == {"api-key": "test-key"}

    def test_a_symlink_at_the_dir_path_raises(self, _sglang_available) -> None:
        from solar_host.backends.sglang import key_config_dir

        key_config_dir().rmdir()  # drop the real dir
        # Rebuild the path directly — key_config_dir() would re-create it.
        path = Path(tempfile.gettempdir()) / f"solar-host-{os.getuid()}"
        path.symlink_to(_sglang_available.parent / "elsewhere")

        with pytest.raises(RuntimeError, match="not a directory"):
            key_config_dir()

    def test_a_0755_dir_is_tightened_to_0700(self, _sglang_available) -> None:
        import os

        from solar_host.backends.sglang import key_config_dir

        key_config_dir().rmdir()
        path = key_config_dir()
        os.chmod(path, 0o755)
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o755

        resolved = key_config_dir()

        assert resolved == path
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o700

    def test_a_dir_owned_by_another_uid_raises(
        self, _sglang_available, monkeypatch
    ) -> None:
        import os

        from solar_host.backends.sglang import key_config_dir

        monkeypatch.setattr(os, "getuid", lambda: 12345)

        with pytest.raises(RuntimeError, match="owned by uid"):
            key_config_dir()


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


class TestLifecycleKeyConfigCleanup:
    """IT Sec #97 follow-up: the key config file must not outlive a failed
    or deleted start.

    Both tests drive the real ProcessManager lifecycle against a real
    subprocess (never a fake process object) so the assertion covers the
    exact production cleanup path, monkeypatching only
    ``get_runner_for_config`` and the ready-timeout clock.
    """

    @pytest.mark.anyio
    async def test_a_readiness_timeout_removes_the_key_file(
        self, _sglang_available, monkeypatch
    ) -> None:
        """A timed-out start of an SGLang instance leaves no key config file.

        The instance is set up with a tiny ``instance_ready_timeout_s`` so
        the spawn parks and the timeout fires while the process is still
        alive — the same condition the production timeout branch handles.
        """
        from solar_host.backends.sglang import key_config_path

        monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 0.5)
        monkeypatch.setattr("solar_host.config.settings.max_retries", 0)
        _make_instance("inst-1")
        runner = _SleepingSglangRunner()
        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
        )

        manager = ProcessManager()
        result = await manager.start_instance("inst-1")

        assert result is False
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.FAILED
        assert "readiness" in (instance.error_message or "")
        assert "inst-1" not in manager.processes
        # The key config file must not outlive the timed-out start.
        assert not key_config_path("inst-1").exists()

    @pytest.mark.anyio
    async def test_deleting_while_starting_removes_the_key_file(
        self, _sglang_available, monkeypatch
    ) -> None:
        """Deleting an instance that is starting removes its key config file.

        The delete path skips the full stop hook
        (``call_runner_on_stop=False``) so other runners keep their "no
        stop hook on delete" semantics — but ``remove_key_config`` is
        called directly before the purge, so the file goes regardless.
        """
        from solar_host.backends.sglang import key_config_path

        monkeypatch.setattr("solar_host.config.settings.max_retries", 0)
        _make_instance("inst-1")
        runner = _SleepingSglangRunner()
        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
        )

        manager = ProcessManager()
        task = asyncio.create_task(manager._try_start_instance("inst-1", attempt=0))

        # Mid-flight: the spawn has happened and build_command wrote the key
        # config file, but the backend has not reported readiness yet.
        await asyncio.sleep(0.3)
        assert config_manager.get_instance("inst-1").status == InstanceStatus.STARTING
        assert key_config_path("inst-1").exists()

        deleted = manager.delete_instance("inst-1")
        assert deleted is True

        assert not key_config_path("inst-1").exists()
        assert config_manager.get_instance("inst-1") is None

        result = await task
        assert result is False
