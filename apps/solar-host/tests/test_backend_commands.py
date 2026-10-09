"""Launch-flag tests for the metrics-enabled backend commands.

The token-accounting rework relies on the backends exposing /metrics:
llama.cpp gets ``--metrics --slots``, SGLang gets ``--enable-metrics``.
Both flags are host-managed — SGLang's extra_args must reject them the way
``--port`` and ``--api-key`` already are.
"""

import queue
from types import SimpleNamespace

import pytest

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.backends.sglang import SglangRunner
from solar_host.backends.vllm import VllmRunner
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.models.sglang import SglangConfig
from solar_host.models.vllm import VllmConfig


def _llamacpp_command(**overrides) -> list[str]:
    config = LlamaCppConfig(model="/models/test.gguf", alias="test", **overrides)
    return LlamaCppRunner().build_command(SimpleNamespace(config=config, port=8080))


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
    monkeypatch.setattr(
        "solar_host.backends.sglang.detect_gpu_type", lambda: "nvidia_cuda"
    )
    return venv


def _sglang_command(**overrides) -> list[str]:
    config = SglangConfig(model_path="/models/test", alias="test", **overrides)
    return SglangRunner().build_command(
        SimpleNamespace(config=config, port=8080, id="inst-1")
    )


@pytest.fixture(autouse=True)
def _vllm_available(tmp_path, monkeypatch):
    """Pretend this host is an NVIDIA box with vLLM in a venv."""
    venv = tmp_path / "vllm-venv"
    (venv / "bin").mkdir(parents=True)
    console_script = venv / "bin" / "vllm"
    console_script.write_text("#!/bin/sh\n")
    console_script.chmod(0o755)
    monkeypatch.setattr("solar_host.config.settings.vllm_venv_path", str(venv))
    monkeypatch.setattr(
        "solar_host.backends.vllm.detect_gpu_type", lambda: "nvidia_cuda"
    )
    return venv


def _vllm_command(**overrides) -> list[str]:
    config = VllmConfig(model_path="/models/test", alias="test", **overrides)
    return VllmRunner().build_command(
        SimpleNamespace(config=config, port=8080, id="inst-1")
    )


class TestLlamaCppMetricsFlags:
    def test_metrics_and_slots_are_always_added(self):
        command = _llamacpp_command()

        assert command[-2:] == ["--metrics", "--slots"]

    def test_metrics_flags_survive_speculative_decoding(self):
        command = _llamacpp_command(
            spec_type="draft-dspark", spec_draft_model="/models/draft.gguf"
        )

        assert command[-2:] == ["--metrics", "--slots"]

    def test_metrics_flags_survive_embedding_servers(self):
        command = _llamacpp_command(model_type="embedding")

        assert command[-2:] == ["--metrics", "--slots"]


class TestSglangMetricsFlags:
    def test_enable_metrics_is_added(self):
        command = _sglang_command()

        assert "--enable-metrics" in command

    def test_enable_metrics_cannot_be_duplicated_via_extra_args(self):
        with pytest.raises(ValueError, match="managed by solar-host"):
            _sglang_command(extra_args=["--enable-metrics"])

    def test_extra_args_still_come_last_for_raw_overrides(self):
        command = _sglang_command(extra_args=["--schedule-policy"])

        assert command[-1] == "--schedule-policy"
        # --config (the 0600 key config file) lands after --enable-metrics
        # and before extra_args.
        assert command[command.index("--config") - 1] == "--enable-metrics"


class TestVllmTelemetryFlags:
    def test_prompt_token_details_are_added(self):
        command = _vllm_command()

        assert command.count("--enable-prompt-tokens-details") == 1

    def test_prompt_token_details_cannot_be_duplicated_via_extra_args(self):
        with pytest.raises(ValueError, match="managed by solar-host"):
            _vllm_command(extra_args=["--enable-prompt-tokens-details"])

    def test_the_stats_log_switch_is_host_managed(self):
        """--disable-log-stats would remove the live decode throughput source."""
        with pytest.raises(ValueError, match="managed by solar-host"):
            _vllm_command(extra_args=["--disable-log-stats"])

    def test_extra_args_still_come_last_for_raw_overrides(self):
        command = _vllm_command(extra_args=["--max-num-partial-prefills", "4"])

        assert command[-3:] == [
            "--enable-prompt-tokens-details",
            "--max-num-partial-prefills",
            "4",
        ]


class TestMetricsEndpoints:
    def test_llamacpp_serves_metrics_at_the_canonical_path(self):
        assert LlamaCppRunner().get_metrics_path() == "/metrics"

    def test_sglang_serves_metrics_at_the_canonical_path(self):
        assert SglangRunner().get_metrics_path() == "/metrics"

    def test_vllm_serves_metrics_at_the_canonical_path(self):
        assert VllmRunner().get_metrics_path() == "/metrics"

    def test_the_base_runner_has_no_metrics_path(self):
        from solar_host.backends.base import BackendRunner

        class _Concrete(BackendRunner):
            def build_command(self, instance):
                return ["echo"]

            def parse_log_line(self, instance_id, line, context):
                return None

            def get_health_endpoint(self):
                return "/health"

            def get_supported_endpoints(self):
                return []

            def get_backend_type(self):
                return "concrete"

        assert _Concrete().get_metrics_path() is None
        assert _Concrete().parse_metrics("llamacpp:prompt_tokens_total 1\n") is None


class TestApiKeyNeverOnTheCommandLine:
    """IT Sec #97: the API key must reach the backend without ever appearing
    in the argument list — `ps` reads argv in the clear.

    Delivery per backend: llama.cpp reads LLAMA_API_KEY natively
    (arg.cpp .set_env), vLLM reads VLLM_API_KEY (vllm/envs.py), SGLang gets
    a 0600 YAML config file passed via --config (the file is deleted once
    the backend is ready), and hf_server.py — solar's own code — reads
    SOLAR_API_KEY with the --api-key flag as an optional override.
    """

    def test_llamacpp_command_carries_no_api_key_material(self):
        command = _llamacpp_command()
        assert "--api-key" not in command
        assert "test-key" not in command

    def test_sglang_command_carries_no_api_key_material(self):
        command = _sglang_command()
        assert "--api-key" not in command
        assert "test-key" not in command

    def test_vllm_command_carries_no_api_key_material(self):
        command = _vllm_command()
        assert "--api-key" not in command
        assert "test-key" not in command

    def test_huggingface_command_carries_no_api_key_material(self, monkeypatch):
        from solar_host.backends.huggingface import HuggingFaceRunner
        from solar_host.models.huggingface import HuggingFaceCausalConfig

        monkeypatch.setattr(
            "solar_host.backends.huggingface.HuggingFaceRunner.check_dependencies",
            lambda self: None,
        )
        config = HuggingFaceCausalConfig(
            model_id="/models/test",
            alias="test",
        )
        cmd = HuggingFaceRunner().build_command(
            SimpleNamespace(config=config, port=8080)
        )
        assert "--api-key" not in cmd
        assert "test-key" not in cmd

    def test_env_var_carries_the_host_key_per_backend(self, monkeypatch):
        """One env name per backend: build_env delivers the host key via the
        environment, mirroring the CUDA_VISIBLE_DEVICES path. SGLang is the
        exception — its key rides the 0600 YAML config file (IT Sec #97)."""
        monkeypatch.setattr("solar_host.config.settings.api_key", "test-key")
        from solar_host.backends.huggingface import HuggingFaceRunner
        from solar_host.backends.llamacpp import LlamaCppRunner
        from solar_host.backends.sglang import SglangRunner
        from solar_host.backends.vllm import VllmRunner
        from solar_host.models.llamacpp import LlamaCppConfig
        from solar_host.models.sglang import SglangConfig
        from solar_host.models.vllm import VllmConfig

        llama_instance = SimpleNamespace(
            config=LlamaCppConfig(model="/models/test.gguf", alias="test"),
            port=8080,
            id="inst-1",
        )
        sglang_instance = SimpleNamespace(
            config=SglangConfig(model_path="/models/test", alias="test"),
            port=8080,
            id="inst-1",
        )
        vllm_instance = SimpleNamespace(
            config=VllmConfig(model_path="/models/test", alias="test"),
            port=8080,
            id="inst-1",
        )
        plain_instance = SimpleNamespace(config=None, port=8080, id="inst-1")

        llama_env = LlamaCppRunner().build_env(llama_instance)
        assert llama_env["LLAMA_API_KEY"] == "test-key"

        vllm_env = VllmRunner().build_env(vllm_instance)
        assert vllm_env["VLLM_API_KEY"] == "test-key"

        hf_env = HuggingFaceRunner().build_env(plain_instance)
        assert hf_env["SOLAR_API_KEY"] == "test-key"

        sglang_env = SglangRunner().build_env(sglang_instance)
        assert "SGLANG_API_KEY" not in sglang_env

    def test_hf_server_prefers_the_cli_flag_over_the_env(self):
        """IT Sec #97 precedence shape: --api-key wins when both are set;
        the env var is used when the flag is absent.

        The helper is torch-free so this exercises the real code path
        without pulling the huggingface extra.
        """
        from solar_host.servers.api_key import resolve_api_key

        assert resolve_api_key("flag-key", "env-key") == "flag-key"
        assert resolve_api_key("", "env-key") == "env-key"
        assert resolve_api_key("", "") == ""

    def test_extra_env_still_wins_last_over_the_backend_key(self, monkeypatch):
        """IT Sec #97: the per-backend key env is set BEFORE
        config.extra_env, so an operator's per-instance override beats it —
        the existing 'extra_env wins last' design."""
        monkeypatch.setattr("solar_host.config.settings.api_key", "test-key")
        from solar_host.backends.vllm import VllmRunner
        from solar_host.models.vllm import VllmConfig

        instance = SimpleNamespace(
            config=VllmConfig(
                model_path="/models/test",
                alias="test",
                extra_env={"VLLM_API_KEY": "operator-key"},
            ),
            port=8080,
            id="inst-1",
        )
        env = VllmRunner().build_env(instance)
        assert env["VLLM_API_KEY"] == "operator-key"


class _FakeStdout:
    """Minimal stdout stand-in for _read_logs: one line then EOF."""

    def __init__(self, line: bytes) -> None:
        self._line = line
        self.stdout = self

    def readline(self) -> bytes:
        if self._line:
            line, self._line = self._line, b""
            return line
        return b""


class TestLogRedaction:
    """IT Sec #97: SGLang logs server_args=<resolved_dict()> at startup,
    which carries the key in plain text. The redaction happens in
    _read_logs before the file write, the buffer append, the pushed event
    and the parsers, so every consumer sees the same scrubbed line."""

    def test_read_logs_redacts_the_key_from_file_buffer_and_event(
        self, tmp_path, monkeypatch
    ) -> None:
        from solar_host.process_manager import ProcessManager

        secret = "super-secret-host-key"
        monkeypatch.setattr("solar_host.config.settings.api_key", secret)

        manager = ProcessManager()
        manager.log_dir = tmp_path
        log_file = tmp_path / "test.log"

        fake_process = _FakeStdout(b"api_key=super-secret-host-key\n")
        runner = SglangRunner()
        manager._read_logs("inst-1", fake_process, log_file, runner)

        expected = "api_key=***"
        buffer_lines = [msg.line for msg in manager.log_buffers["inst-1"]]
        assert buffer_lines == [expected]

        pushed = []
        while True:
            try:
                pushed.append(manager._log_queue.get_nowait()["line"])
            except queue.Empty:
                break
        assert pushed == [expected]

        assert log_file.read_text().splitlines() == [expected]
