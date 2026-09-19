"""Usage accounting tests for the vLLM backend (S-061).

The engine's Prometheus counters are the authoritative token source: prompt,
cached and generated tokens all come from counter deltas between the 0→1 and
1→0 transitions of ``vllm:num_requests_running``; the engine-stats log line
only contributes the live decode throughput.
"""

from datetime import UTC, datetime

import pytest

from solar_host.backends.vllm import VllmRunner
from solar_host.models.base import InstancePhase, InstanceUsageSnapshot

EXPOSITION = """\
# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{model_name="test:8b"} 1026.0
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{model_name="test:8b"} 100.0
vllm:prompt_tokens_cached_total{model_name="test:8b"} 58.0
vllm:num_requests_running{model_name="test:8b"} 1.0
vllm:num_requests_waiting{model_name="test:8b"} 0.0
vllm:kv_cache_usage_perc{model_name="test:8b"} 0.5
"""


def snapshot(
    *,
    running: int | None = None,
    waiting: int | None = None,
    prompt: int | None = None,
    generated: int | None = None,
    cached: int | None = None,
    kv: float | None = None,
) -> InstanceUsageSnapshot:
    return InstanceUsageSnapshot(
        requests_processing=running,
        requests_deferred=waiting,
        prompt_tokens_total=prompt,
        generated_tokens_total=generated,
        cached_tokens_total=cached,
        kv_cache_usage_ratio=kv,
    )


@pytest.fixture
def runner() -> VllmRunner:
    return VllmRunner()


class TestParseMetrics:
    def test_maps_every_counter_and_gauge(self, runner) -> None:
        parsed = runner.parse_metrics(EXPOSITION)

        assert parsed is not None
        assert parsed.prompt_tokens_total == 1026
        assert parsed.generated_tokens_total == 100
        assert parsed.cached_tokens_total == 58
        assert parsed.requests_processing == 1
        assert parsed.requests_deferred == 0
        assert parsed.kv_cache_usage_ratio == 0.5

    def test_foreign_exposition_returns_none(self, runner) -> None:
        assert runner.parse_metrics("process_cpu_seconds_total 1.0\n") is None

    def test_the_metrics_path_is_the_canonical_one(self, runner) -> None:
        assert runner.get_metrics_path() == "/metrics"


class TestReadiness:
    def test_the_startup_complete_line_is_the_ready_signal(self, runner) -> None:
        assert runner.is_ready_line("INFO: Application startup complete.")

    def test_the_serving_banner_is_not_enough(self, runner) -> None:
        # Printed before the engine is loaded and the graphs are captured.
        assert not runner.is_ready_line("Starting vLLM server on http://0.0.0.0:3501")


class TestApplyUsageSnapshot:
    def test_the_full_request_lifecycle(self, runner) -> None:
        context = runner.initialize_context()

        # 0→1: counters open at their current values.
        first = runner.apply_usage_snapshot(
            "inst-1",
            context,
            snapshot(running=1, waiting=0, prompt=1000, generated=50, cached=0),
        )
        assert first is not None
        assert first.busy is True
        assert first.phase == InstancePhase.GENERATING
        assert first.active_slots == 1

        # Still running: no state change, no record.
        assert (
            runner.apply_usage_snapshot(
                "inst-1",
                context,
                snapshot(running=1, waiting=0, prompt=1000, generated=50, cached=0),
            )
            is None
        )

        # 1→0: the deltas are the request's exact usage.
        last = runner.apply_usage_snapshot(
            "inst-1",
            context,
            snapshot(running=0, waiting=0, prompt=2026, generated=150, cached=58),
        )
        assert last is not None
        assert last.busy is False
        assert last.phase == InstancePhase.IDLE

        metrics = runner.get_last_generation(context)
        assert metrics is not None
        assert metrics.instance_id == "inst-1"
        assert metrics.prompt_tokens == 1026
        assert metrics.generated_tokens == 100
        assert metrics.cached_tokens == 58
        assert metrics.prompt_eval_tokens == 968
        assert metrics.total_tokens == 1126
        assert metrics.source == "metrics"
        assert metrics.started_at is not None
        assert metrics.finished_at is not None

    def test_a_second_running_request_does_not_reset_the_baseline(self, runner) -> None:
        context = runner.initialize_context()
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=1, prompt=1000, generated=0, cached=0)
        )
        # A second request joins while the first is still in flight.
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=2, prompt=1000, generated=0, cached=0)
        )
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=0, prompt=1500, generated=200, cached=0)
        )

        metrics = runner.get_last_generation(context)
        assert metrics is not None
        # The delta spans from the first 0→1 snapshot, not the second request.
        assert metrics.prompt_tokens == 500
        assert metrics.generated_tokens == 200

    def test_a_counter_reset_is_dropped_rather_than_recorded_wrong(
        self, runner
    ) -> None:
        context = runner.initialize_context()
        runner.apply_usage_snapshot(
            "inst-1",
            context,
            snapshot(running=1, prompt=1000, generated=500, cached=100),
        )
        runner.apply_usage_snapshot(
            "inst-1",
            context,
            snapshot(running=0, prompt=400, generated=600, cached=200),
        )

        metrics = runner.get_last_generation(context)
        assert metrics is not None
        # The prompt counter went backwards (server restart): no measurement.
        assert metrics.prompt_tokens is None
        assert metrics.prompt_eval_tokens is None
        assert metrics.total_tokens is None
        assert metrics.generated_tokens == 100
        assert metrics.cached_tokens == 100

    def test_decode_tps_from_the_stats_line_lands_in_the_record(self, runner) -> None:
        context = runner.initialize_context()
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=1, prompt=1000, generated=0, cached=0)
        )
        line = (
            "Engine 000: Avg prompt throughput: 512.0 tokens/s, "
            "Avg generation throughput: 42.3 tokens/s, Running: 1 reqs, "
            "Waiting: 0 reqs, GPU KV cache usage: 12.3%, "
            "Prefix cache hit rate: 0.0%, MM cache hit rate: 0.0%"
        )
        runner.parse_log_line("inst-1", line, context)
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=0, prompt=1500, generated=100, cached=0)
        )

        metrics = runner.get_last_generation(context)
        assert metrics is not None
        assert metrics.decode_tps == 42.3
        assert metrics.decode_ms_per_token == pytest.approx(23.64, rel=1e-2)

    def test_a_decode_sample_does_not_leak_into_the_next_request(self, runner) -> None:
        """The log thread publishes samples under its own key; the open
        transition and the finalization both clear it, so a request never
        inherits a number printed for the previous one's window."""
        context = runner.initialize_context()
        line = (
            "Engine 000: Avg generation throughput: 42.3 tokens/s, "
            "Running: 1 reqs, Waiting: 0 reqs"
        )

        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=1, prompt=0, generated=0, cached=0)
        )
        runner.parse_log_line("inst-1", line, context)
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=0, prompt=1000, generated=50, cached=0)
        )
        first = runner.get_last_generation(context)
        assert first is not None
        assert first.decode_tps == 42.3

        # A line covering the first request's tail lands after it finalized.
        stale = (
            "Engine 000: Avg generation throughput: 99.9 tokens/s, "
            "Running: 0 reqs, Waiting: 0 reqs"
        )
        runner.parse_log_line("inst-1", stale, context)

        # The second request runs and ends without a stats line of its own.
        runner.apply_usage_snapshot(
            "inst-1",
            context,
            snapshot(running=1, prompt=1000, generated=50, cached=0),
        )
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=0, prompt=2000, generated=120, cached=0)
        )
        second = runner.get_last_generation(context)
        assert second is not None
        assert second.decode_tps is None
        assert second.decode_ms_per_token is None

    def test_a_missing_running_gauge_is_ignored(self, runner) -> None:
        context = runner.initialize_context()

        assert (
            runner.apply_usage_snapshot(
                "inst-1", context, snapshot(prompt=1000, generated=0, cached=0)
            )
            is None
        )
        assert runner.get_last_generation(context) is None

    def test_the_ring_buffer_keeps_the_last_hundred(self, runner) -> None:
        context = runner.initialize_context()
        context["recent_generations"] = [object()] * 100

        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=1, prompt=0, generated=0, cached=0)
        )
        runner.apply_usage_snapshot(
            "inst-1", context, snapshot(running=0, prompt=10, generated=5, cached=0)
        )

        recent = context["recent_generations"]
        assert len(recent) == 100
        assert recent[-1].source == "metrics"

    def test_get_last_generation_is_none_without_records(self, runner) -> None:
        assert runner.get_last_generation(runner.initialize_context()) is None


class TestParseLogLine:
    def test_the_engine_stats_line_drives_busy_and_decode_tps(self, runner) -> None:
        context = runner.initialize_context()
        line = (
            "Engine 000: Avg prompt throughput: 1234.5 tokens/s, "
            "Avg generation throughput: 87.6 tokens/s, Running: 3 reqs, "
            "Waiting: 2 reqs, GPU KV cache usage: 55.5%, "
            "Prefix cache hit rate: 12.5%, MM cache hit rate: 0.0%"
        )

        update = runner.parse_log_line("inst-1", line, context)

        assert update is not None
        assert update.busy is True
        assert update.phase == InstancePhase.GENERATING
        assert update.active_slots == 3
        assert update.decode_tps == 87.6

    def test_an_idle_stats_line_closes_the_phase(self, runner) -> None:
        context = runner.initialize_context()
        line = (
            "Engine 000: Avg prompt throughput: 0.0 tokens/s, "
            "Avg generation throughput: 0.0 tokens/s, Running: 0 reqs, "
            "Waiting: 0 reqs, GPU KV cache usage: 0.1%"
        )

        update = runner.parse_log_line("inst-1", line, context)

        assert update is not None
        assert update.busy is False
        assert update.phase == InstancePhase.IDLE
        assert update.active_slots == 0

    def test_no_prefill_phase_is_ever_reported(self, runner) -> None:
        """vLLM v1 logs no per-step prefill lines; prefill reads as generating."""
        context = runner.initialize_context()
        line = (
            "Engine 000: Avg prompt throughput: 5000.0 tokens/s, "
            "Avg generation throughput: 0.0 tokens/s, Running: 1 reqs, "
            "Waiting: 0 reqs, GPU KV cache usage: 4.0%"
        )

        update = runner.parse_log_line("inst-1", line, context)

        assert update is not None
        assert update.phase == InstancePhase.GENERATING

    def test_unrelated_lines_are_ignored(self, runner) -> None:
        context = runner.initialize_context()

        assert (
            runner.parse_log_line(
                "inst-1", "INFO: 127.0.0.1 - GET /health 200 OK", context
            )
            is None
        )
        assert (
            runner.parse_log_line("inst-1", "Application startup complete.", context)
            is None
        )

    def test_an_unchanged_stats_line_emits_nothing(self, runner) -> None:
        context = runner.initialize_context()
        line = (
            "Engine 000: Avg generation throughput: 0.0 tokens/s, "
            "Running: 0 reqs, Waiting: 0 reqs, GPU KV cache usage: 0.0%"
        )

        assert runner.parse_log_line("inst-1", line, context) is not None
        assert runner.parse_log_line("inst-1", line, context) is None


def test_the_fresh_context_shape() -> None:
    context = VllmRunner().initialize_context()

    assert context["recent_generations"] == []
    assert context["last_state"] == {"busy": False, "phase": "idle"}
    assert context["pending_request"] is None
    assert context["decode_sample"] is None
    assert context["usage_state"] == {"open_counters": None}
    # No SGLang-style chunked-prefill accumulator: vLLM's prompt counter is
    # request-level, so there is nothing to accumulate across log lines.
    assert "active_prefill" not in context


def test_timestamps_are_iso_utc(runner) -> None:
    context = runner.initialize_context()
    runner.apply_usage_snapshot(
        "inst-1", context, snapshot(running=1, prompt=0, generated=0, cached=0)
    )
    runner.apply_usage_snapshot(
        "inst-1", context, snapshot(running=0, prompt=10, generated=1, cached=0)
    )

    metrics = runner.get_last_generation(context)
    assert metrics is not None
    parsed = datetime.fromisoformat(metrics.finished_at)
    assert parsed.tzinfo == UTC
