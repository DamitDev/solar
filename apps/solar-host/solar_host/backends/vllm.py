"""vLLM backend runner implementation.

vLLM is installed into its own virtualenv rather than solar-host's, so the
runner resolves an executable inside ``settings.vllm_venv_path`` and
reproduces what ``source bin/activate`` does through the child's environment
(``VIRTUAL_ENV``, a ``PATH`` prefix, no ``PYTHONHOME``) instead of going
through a shell.

Unlike SGLang, vLLM performs no ``:`` parsing on model names: registry lookup
is exact-match and LoRA adapters resolve by dict lookup rather than a
``base:adapter`` syntax, so the alias is served verbatim and solar-control
needs no request translation.
"""

import logging
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solar_host.backends.base import BackendRunner, RuntimeStateUpdate
from solar_host.backends.prom import parse_prometheus
from solar_host.config import settings
from solar_host.memory_monitor import detect_gpu_type
from solar_host.models.base import (
    GenerationMetrics,
    InstancePhase,
    InstanceUsageSnapshot,
)

logger = logging.getLogger(__name__)

# Readiness contract for the lifecycle status (starting -> running). vLLM
# prints this after the engine is initialized and the CUDA graphs are
# captured, which makes it the first line that proves the model is loaded
# (the "Starting vLLM server on http://..." line comes before that).
_RE_READY = re.compile(r"Application startup complete", re.IGNORECASE)

# Engine stats line, printed roughly every ten seconds while the engine ticks:
#   Engine 000: Avg prompt throughput: 1234.5 tokens/s, Avg generation
#   throughput: 42.3 tokens/s, Running: 2 reqs, Waiting: 0 reqs,
#   GPU KV cache usage: 12.3%, Prefix cache hit rate: 45.6%
_RE_ENGINE_STATS = re.compile(r"Avg generation throughput:", re.IGNORECASE)
_RE_GEN_THROUGHPUT = re.compile(
    r"Avg generation throughput:\s*([0-9.]+)\s*tokens/s", re.IGNORECASE
)
_RE_RUNNING_REQ = re.compile(r"Running:\s*(\d+)\s*reqs", re.IGNORECASE)

# Typed config field -> CLI flag. Value-carrying flags only; booleans are
# listed separately because vLLM takes them as bare flags.
_VALUE_FLAGS: tuple[tuple[str, str], ...] = (
    ("tensor_parallel_size", "--tensor-parallel-size"),
    ("pipeline_parallel_size", "--pipeline-parallel-size"),
    ("max_model_len", "--max-model-len"),
    ("gpu_memory_utilization", "--gpu-memory-utilization"),
    ("max_num_seqs", "--max-num-seqs"),
    ("max_num_batched_tokens", "--max-num-batched-tokens"),
    ("dtype", "--dtype"),
    ("quantization", "--quantization"),
    ("kv_cache_dtype", "--kv-cache-dtype"),
    ("moe_backend", "--moe-backend"),
    ("speculative_config", "--speculative-config"),
    ("tool_call_parser", "--tool-call-parser"),
    ("reasoning_parser", "--reasoning-parser"),
)

_BOOL_FLAGS: tuple[tuple[str, str], ...] = (
    ("trust_remote_code", "--trust-remote-code"),
    ("enforce_eager", "--enforce-eager"),
    ("enable_auto_tool_choice", "--enable-auto-tool-choice"),
)


def resolve_executable() -> list[str] | None:
    """Return the argv prefix that launches vLLM, or None if unavailable.

    Prefers the ``vllm`` console script inside the configured venv, falls
    back to that venv's interpreter running ``vllm.entrypoints.cli.main``,
    and finally to a ``vllm`` on PATH for hosts that installed it into the
    ambient environment. The ``serve`` subcommand and the positional model
    path are appended by :meth:`VllmRunner.build_command`.
    """
    venv = settings.vllm_venv_path.strip()
    if venv:
        venv_path = Path(venv)
        console_script = venv_path / "bin" / "vllm"
        if console_script.is_file():
            return [str(console_script)]
        interpreter = venv_path / "bin" / "python"
        if interpreter.is_file():
            return [str(interpreter), "-m", "vllm.entrypoints.cli.main"]
        return None

    on_path = shutil.which("vllm")
    if on_path:
        return [on_path]
    return None


def is_supported() -> bool:
    """True when this host can actually run the vLLM backend.

    vLLM's kernels are CUDA-only, so the backend is advertised only on
    NVIDIA hosts that also have vLLM installed.
    """
    return detect_gpu_type() == "nvidia_cuda" and resolve_executable() is not None


class VllmRunner(BackendRunner):
    """Backend runner for vLLM server instances."""

    def get_backend_type(self) -> str:
        return "vllm"

    def get_served_model_name(self, config: Any) -> str:
        """vLLM answers to the alias verbatim.

        Unlike SGLang (which reads ``:`` as its LoRA separator), vLLM's
        request path does no colon parsing — the registry lookup is
        exact-match — so there is nothing to translate. solar-control keys
        its translation machinery on ``served != alias`` and becomes a
        no-op for this backend.
        """
        return config.alias

    async def probe_context_size(self, instance: Any) -> int | None:
        """Ask the running vLLM server for its max context length.

        ``GET /v1/models`` carries ``max_model_len`` per model entry. The
        endpoint sits behind the auth middleware whenever a key is set, so
        the host's key is sent. Failures leave the instance's
        ``context_size`` as None — unverifiable, never violated (S-060).
        """
        import aiohttp

        if not instance.port:
            return None
        url = f"http://127.0.0.1:{instance.port}/v1/models"
        headers = (
            {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        )
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(
                    url, timeout=aiohttp.ClientTimeout(total=3), headers=headers
                ) as resp,
            ):
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception:
            logger.warning(
                "vLLM context probe failed for %s (port %s)",
                instance.id,
                instance.port,
                exc_info=True,
            )
            return None

        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = entry.get("max_model_len")
            if isinstance(value, int) and value > 0:
                return value
        return None

    def is_ready_line(self, line: str) -> bool:
        """True when the line proves vLLM finished loading and is serving."""
        return _RE_READY.search(line) is not None

    @staticmethod
    def check_dependencies() -> list[str]:
        """Return the argv prefix, raising when vLLM cannot run here."""
        if detect_gpu_type() != "nvidia_cuda":
            raise RuntimeError(
                "vLLM backend requires an NVIDIA CUDA host "
                f"(this host reports gpu_type={detect_gpu_type()!r})"
            )
        executable = resolve_executable()
        if executable is None:
            raise RuntimeError(
                "vLLM executable not found. Set VLLM_VENV_PATH to the "
                "virtualenv vLLM is installed into, or install vllm so "
                "that it is on PATH."
            )
        return executable

    def build_command(self, instance: Any) -> list[str]:
        """Build the `vllm serve` command from the instance config."""
        cmd = list(self.check_dependencies())
        config = instance.config

        if not config.model_path:
            raise RuntimeError(
                "vLLM instance has no model_path — the model source must be "
                "resolved before the instance is started"
            )

        cmd += [
            "serve",
            config.model_path,
            "--served-model-name",
            config.alias,
            "--host",
            config.host,
            "--port",
            str(instance.port),
            "--api-key",
            settings.api_key,
        ]

        for field, flag in _VALUE_FLAGS:
            value = getattr(config, field, None)
            if value is not None:
                cmd += [flag, str(value)]

        for field, flag in _BOOL_FLAGS:
            if getattr(config, field, False):
                cmd.append(flag)

        # Tri-state: None leaves the engine default (on for most builds),
        # so only an explicit choice is expressed on the command line.
        if config.enable_prefix_caching is not None:
            cmd.append(
                "--enable-prefix-caching"
                if config.enable_prefix_caching
                else "--no-enable-prefix-caching"
            )

        # Host-managed telemetry flags (see RESERVED_VLLM_ARGS): the metrics
        # counters drive the authoritative busy signal and the per-request
        # token accounting, and --disable-log-stats would remove the stats
        # line the live decode throughput comes from. Prompt-token details
        # make the served responses carry the cached split.
        cmd.append("--enable-prompt-tokens-details")

        # Last, so a raw override beats the typed flag above it.
        if config.extra_args:
            cmd += list(config.extra_args)

        return cmd

    def build_env(self, instance: Any) -> dict[str, str]:
        """Activate the vLLM venv; ``config.extra_env`` is applied last.

        Starts from the base GPU visibility block (S-058). vLLM keeps its
        prefix cache in memory, so unlike SGLang there is no per-instance
        cache directory to point at.
        """
        env: dict[str, str] = super().build_env(instance)
        config = instance.config

        venv = settings.vllm_venv_path.strip()
        if venv:
            venv_path = Path(venv)
            env["VIRTUAL_ENV"] = str(venv_path)
            env["PATH"] = f"{venv_path / 'bin'}:{os.environ.get('PATH', '')}"
            # An inherited PYTHONHOME would send the interpreter looking for
            # its stdlib outside the venv, which is why `activate` unsets it.
            # CPython reads an empty value as unset, and build_env can only
            # add variables, so empty is how the removal is expressed.
            env["PYTHONHOME"] = ""

        if config.extra_env:
            env.update(config.extra_env)

        return env

    def get_health_endpoint(self) -> str:
        return "/health"

    def get_supported_endpoints(self) -> list[str]:
        return [
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/models",
            "/health",
        ]

    def initialize_context(self) -> dict[str, Any]:
        """Initialize parsing context for vLLM engine-stats log parsing."""
        return {
            "recent_generations": [],
            "last_state": {
                "busy": False,
                "phase": InstancePhase.IDLE.value,
            },
            # The in-flight request's timing fields, created and cleared by
            # the /metrics poll (the request's lifetime owner), finalized
            # into GenerationMetrics when the running-request count drops
            # back to 0.
            "pending_request": None,
            # The latest engine-stats throughput sample, published by the
            # log-reader thread under its own key so neither thread ever
            # read-modify-writes the other's dict.
            "decode_sample": None,
            # 0→1 counter snapshot for the /metrics per-request delta.
            "usage_state": {"open_counters": None},
        }

    def parse_log_line(
        self, instance_id: str, line: str, context: dict[str, Any]
    ) -> RuntimeStateUpdate | None:
        """Derive busy/phase, decode throughput and active slots.

        vLLM logs one engine-stats line roughly every ten seconds while the
        engine ticks, carrying the live decode throughput and the running /
        waiting request counts. v1 logs no per-step prefill lines, so the
        phase is GENERATING for as long as requests are running; the exact
        per-request token counts come from the /metrics counter deltas in
        :meth:`apply_usage_snapshot`.
        """
        if _RE_ENGINE_STATS.search(line) is None:
            return None

        running_match = _RE_RUNNING_REQ.search(line)
        running = int(running_match.group(1)) if running_match else None

        decode_tps: float | None = None
        tps_match = _RE_GEN_THROUGHPUT.search(line)
        if tps_match:
            try:
                decode_tps = float(tps_match.group(1))
            except ValueError:
                decode_tps = None

        # A single-key assignment: the poll thread only ever reads this key,
        # so a poll landing between two log lines cannot resurrect a dict the
        # poll just cleared (which could attribute a sample to the next
        # request).
        if decode_tps is not None:
            context["decode_sample"] = {
                "decode_tps": decode_tps,
                "decode_ms_per_token": (
                    (1000.0 / decode_tps) if decode_tps > 0 else None
                ),
            }

        busy = running is None or running > 0
        phase = InstancePhase.GENERATING if busy else InstancePhase.IDLE
        active_slots = running if running is not None else (1 if busy else 0)

        return self._state_update(context, busy, phase, active_slots, decode_tps)

    def _state_update(
        self,
        context: dict[str, Any],
        busy: bool,
        phase: InstancePhase,
        active_slots: int,
        decode_tps: float | None,
    ) -> RuntimeStateUpdate | None:
        """Build a RuntimeStateUpdate only when the state actually changed."""
        last_state = context.get("last_state", {})
        state = {
            "busy": busy,
            "phase": phase.value,
            "active_slots": active_slots,
            "decode_tps": decode_tps,
        }
        if last_state == state:
            return None
        context["last_state"] = state
        return RuntimeStateUpdate(
            busy=busy,
            phase=phase,
            active_slots=active_slots,
            decode_tps=decode_tps,
        )

    def get_last_generation(self, context: dict[str, Any]) -> GenerationMetrics | None:
        """Get the last generation metrics from context."""
        recent = context.get("recent_generations", [])
        if not recent:
            return None
        return recent[-1]

    # vLLM's counters live under the `vllm:` colon namespace. The prompt
    # counter counts the full request input (cached tokens included) and the
    # cached counter its cached subset, both exact per request on the v1
    # engine; the gauges mirror the scheduler's running / waiting queues.
    _METRICS_MAP: tuple[tuple[str, str, type], ...] = (
        ("vllm:prompt_tokens_total", "prompt_tokens_total", int),
        ("vllm:generation_tokens_total", "generated_tokens_total", int),
        ("vllm:prompt_tokens_cached_total", "cached_tokens_total", int),
        ("vllm:num_requests_running", "requests_processing", int),
        ("vllm:num_requests_waiting", "requests_deferred", int),
        ("vllm:kv_cache_usage_perc", "kv_cache_usage_ratio", float),
    )

    def get_metrics_path(self) -> str | None:
        """Prometheus endpoint of the vLLM server (always enabled)."""
        return "/metrics"

    def parse_metrics(self, text: str) -> InstanceUsageSnapshot | None:
        """Parse the vLLM exposition into an usage snapshot."""
        values = parse_prometheus(text)
        kwargs: dict[str, Any] = {}
        for prom_name, attr, cast in self._METRICS_MAP:
            if prom_name in values:
                kwargs[attr] = cast(values[prom_name])
        if not kwargs:
            return None
        return InstanceUsageSnapshot(**kwargs)

    def apply_usage_snapshot(
        self,
        instance_id: str,
        context: dict[str, Any],
        snapshot: InstanceUsageSnapshot,
    ) -> RuntimeStateUpdate | None:
        """Finalize per-request metrics from counter deltas and drive busy.

        The 1→0 running-requests transition is the request-end signal: the
        engine-stats line only prints every ~10 s, so the gauges are the
        tighter clock. Prompt, cached and generated tokens all come from
        counter deltas — vLLM's prompt counter is request-level (the full
        input, cached portion included), so no log accumulation is needed.

        Busy is authoritative from ``num_requests_running``, but only when
        it disagrees with the log-derived state.
        """
        if snapshot.requests_processing is None:
            return None
        running = snapshot.requests_processing > 0
        usage_state = context.setdefault("usage_state", {"open_counters": None})

        if running:
            if usage_state["open_counters"] is None:
                usage_state["open_counters"] = {
                    "prompt": snapshot.prompt_tokens_total,
                    "generated": snapshot.generated_tokens_total,
                    "cached": snapshot.cached_tokens_total,
                }
                context["pending_request"] = {
                    "started_at": datetime.now(UTC).isoformat(),
                }
                # A stats line the engine printed for the previous request's
                # tail must not be attributed to this one; the request only
                # ever consumes a sample published after it began.
                context["decode_sample"] = None
            last_state = context.get("last_state", {})
            if not last_state.get("busy", False):
                # The log missed the request start (e.g. mid-stream log);
                # the counter is the only evidence of activity.
                return self._state_update(
                    context,
                    busy=True,
                    phase=InstancePhase.GENERATING,
                    active_slots=snapshot.requests_processing,
                    decode_tps=last_state.get("decode_tps"),
                )
            return None

        if usage_state["open_counters"] is not None:
            metrics = self._finalize_from_counters(
                instance_id,
                context,
                usage_state["open_counters"],
                snapshot,
            )
            if metrics is not None:
                recent = context.get("recent_generations", [])
                recent.append(metrics)
                if len(recent) > 100:
                    recent = recent[-100:]
                context["recent_generations"] = recent
            usage_state["open_counters"] = None
            context["pending_request"] = None

        last_state = context.get("last_state", {})
        if not last_state.get("busy", False):
            return None
        # The request drained: emit the idle transition if the stats line
        # has not already printed one.
        return self._state_update(
            context,
            busy=False,
            phase=InstancePhase.IDLE,
            active_slots=0,
            decode_tps=None,
        )

    @staticmethod
    def _delta(current: int | None, opening: int | None) -> int | None:
        """Counter movement between the 0→1 snapshot and now; None when unknown.

        A negative movement can only come from a counter reset (the server
        restarted mid-snapshot), which is not this request's traffic — the
        measurement is dropped rather than recorded wrong.
        """
        if current is None or opening is None or current < opening:
            return None
        return current - opening

    def _finalize_from_counters(
        self,
        instance_id: str,
        context: dict[str, Any],
        open_counters: dict[str, Any],
        snapshot: InstanceUsageSnapshot,
    ) -> GenerationMetrics | None:
        """Build the per-request GenerationMetrics from the 0→1 counter
        snapshot and the current snapshot (the 1→0 transition).

        ``decode_tps`` is the engine's last stats-window average seen while
        the request ran (a best-effort live number, not an exact per-request
        timing); the token counts are exact.
        """
        pending: dict[str, Any] | None = context.get("pending_request")
        # The log thread's throughput sample, published after this request
        # began (the open transition clears any earlier one) and cleared here
        # so it cannot outlive the record it belongs to.
        sample = context.get("decode_sample") or {}
        context["decode_sample"] = None

        prompt_tokens = self._delta(
            snapshot.prompt_tokens_total, open_counters.get("prompt")
        )
        generated_tokens = self._delta(
            snapshot.generated_tokens_total, open_counters.get("generated")
        )
        cached_tokens = self._delta(
            snapshot.cached_tokens_total, open_counters.get("cached")
        )

        prompt_eval_tokens = None
        if prompt_tokens is not None and cached_tokens is not None:
            prompt_eval_tokens = max(0, prompt_tokens - cached_tokens)
        total_tokens = None
        if prompt_tokens is not None and generated_tokens is not None:
            total_tokens = prompt_tokens + generated_tokens

        if prompt_tokens is None and generated_tokens is None:
            context["pending_request"] = None
            return None

        metrics = GenerationMetrics(
            instance_id=instance_id,
            prompt_tokens=prompt_tokens,
            generated_tokens=generated_tokens,
            cached_tokens=cached_tokens,
            prompt_eval_tokens=prompt_eval_tokens,
            total_tokens=total_tokens,
            decode_tps=sample.get("decode_tps"),
            decode_ms_per_token=sample.get("decode_ms_per_token"),
            started_at=(pending or {}).get("started_at"),
            finished_at=datetime.now(UTC).isoformat(),
            source="metrics",
        )
        context["pending_request"] = None
        return metrics
