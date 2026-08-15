"""Launch-flag tests for the metrics-enabled backend commands.

The token-accounting rework relies on the backends exposing /metrics:
llama.cpp gets ``--metrics --slots``, SGLang gets ``--enable-metrics``.
Both flags are host-managed — SGLang's extra_args must reject them the way
``--port`` and ``--api-key`` already are.
"""

from types import SimpleNamespace

import pytest

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.backends.sglang import SglangRunner
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.models.sglang import SglangConfig


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

        assert command[-2:] == ["--enable-metrics", "--schedule-policy"]


class TestMetricsEndpoints:
    def test_llamacpp_serves_metrics_at_the_canonical_path(self):
        assert LlamaCppRunner().get_metrics_path() == "/metrics"

    def test_sglang_serves_metrics_at_the_canonical_path(self):
        assert SglangRunner().get_metrics_path() == "/metrics"

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
