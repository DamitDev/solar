"""SGLang backend runner implementation.

SGLang is installed into its own virtualenv rather than solar-host's, so the
runner resolves an executable inside ``settings.sglang_venv_path`` and
reproduces what ``source bin/activate`` does through the child's environment
(``VIRTUAL_ENV``, a ``PATH`` prefix, no ``PYTHONHOME``) instead of going
through a shell.
"""

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from solar_host.backends.base import BackendRunner, RuntimeStateUpdate
from solar_host.config import settings
from solar_host.memory_monitor import detect_gpu_type
from solar_host.models.base import GenerationMetrics, InstancePhase

logger = logging.getLogger(__name__)

# Readiness contract for the lifecycle status (starting -> running). SGLang
# prints its own banner after the warmup run completes, which is later (and
# therefore more truthful) than the uvicorn banner; the uvicorn line is kept
# as a fallback for builds that do not print the banner.
_RE_READY = re.compile(
    r"fired up and ready to roll|Uvicorn running on https?://", re.IGNORECASE
)

# Scheduler progress lines, e.g.
#   [2026-08-14 12:00:00] Prefill batch. #new-seq: 1, #new-token: 512, ...
#   [2026-08-14 12:00:01] Decode batch. #running-req: 2, gen throughput (token/s): 84.21, ...
_RE_PREFILL_BATCH = re.compile(r"Prefill batch\.")
_RE_DECODE_BATCH = re.compile(r"Decode batch\.")
_RE_RUNNING_REQ = re.compile(r"#running-req:\s*(\d+)")
_RE_GEN_THROUGHPUT = re.compile(r"gen throughput \(token/s\):\s*([0-9.]+)")

# Typed config field -> CLI flag. Value-carrying flags only; booleans are
# listed separately because SGLang takes them as bare flags.
_VALUE_FLAGS: tuple[tuple[str, str], ...] = (
    ("tp_size", "--tp-size"),
    ("dp_size", "--dp-size"),
    ("context_length", "--context-length"),
    ("mem_fraction_static", "--mem-fraction-static"),
    ("chunked_prefill_size", "--chunked-prefill-size"),
    ("max_running_requests", "--max-running-requests"),
    ("cuda_graph_max_bs", "--cuda-graph-max-bs"),
    ("cuda_graph_max_bs_decode", "--cuda-graph-max-bs-decode"),
    ("swa_full_tokens_ratio", "--swa-full-tokens-ratio"),
    ("dtype", "--dtype"),
    ("quantization", "--quantization"),
    ("kv_cache_dtype", "--kv-cache-dtype"),
    ("moe_runner_backend", "--moe-runner-backend"),
    ("speculative_algorithm", "--speculative-algorithm"),
    ("hicache_ratio", "--hicache-ratio"),
    ("hicache_mem_layout", "--hicache-mem-layout"),
    ("hicache_io_backend", "--hicache-io-backend"),
)

_BOOL_FLAGS: tuple[tuple[str, str], ...] = (
    ("trust_remote_code", "--trust-remote-code"),
    ("enable_hierarchical_cache", "--enable-hierarchical-cache"),
)

# The persistent cache flags only make sense with a storage directory, which
# is host configuration rather than part of the instance config.
_STORAGE_FLAGS: tuple[tuple[str, str], ...] = (
    ("hicache_storage_backend", "--hicache-storage-backend"),
    (
        "hicache_storage_backend_extra_config",
        "--hicache-storage-backend-extra-config",
    ),
    ("hicache_storage_prefetch_policy", "--hicache-storage-prefetch-policy"),
)

# Environment variable SGLang's file storage backend reads its directory from.
HICACHE_STORAGE_DIR_ENV = "SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR"


def served_model_name(alias: str) -> str:
    """The name SGLang can actually be served under for *alias*.

    SGLang reads ``:`` in a model name as the ``base-model:adapter`` LoRA
    separator (sgl-project/sglang#12745): newer builds refuse to start with a
    colon in ``--served-model-name``, and older ones accept the flag but then
    answer requests for ``a:b`` with "LoRA adapter b is not loaded". Solar's
    aliases are ``name:tag`` by convention, so the colon is translated here and
    solar-control translates the request's ``model`` field to match. The alias
    itself stays untouched everywhere else — it is what the gateway routes on.
    """
    return alias.replace(":", "-")


def resolve_executable() -> list[str] | None:
    """Return the argv prefix that launches SGLang, or None if unavailable.

    Prefers the ``sglang`` console script inside the configured venv, falls
    back to that venv's interpreter running ``sglang.launch_server`` (the
    older entry point, still supported), and finally to a ``sglang`` on PATH
    for hosts that installed it into the ambient environment.
    """
    venv = settings.sglang_venv_path.strip()
    if venv:
        venv_path = Path(venv)
        console_script = venv_path / "bin" / "sglang"
        if console_script.is_file():
            return [str(console_script), "serve"]
        interpreter = venv_path / "bin" / "python"
        if interpreter.is_file():
            return [str(interpreter), "-m", "sglang.launch_server"]
        return None

    on_path = shutil.which("sglang")
    if on_path:
        return [on_path, "serve"]
    return None


def is_supported() -> bool:
    """True when this host can actually run the SGLang backend.

    SGLang's kernels are CUDA-only, so the backend is advertised only on
    NVIDIA hosts that also have SGLang installed.
    """
    return detect_gpu_type() == "nvidia_cuda" and resolve_executable() is not None


class SglangRunner(BackendRunner):
    """Backend runner for SGLang server instances."""

    def get_backend_type(self) -> str:
        return "sglang"

    def get_served_model_name(self, config: Any) -> str:
        """SGLang answers to the colon-free form of the alias."""
        return served_model_name(config.alias)

    def is_ready_line(self, line: str) -> bool:
        """True when the line proves SGLang finished warmup and is serving."""
        return _RE_READY.search(line) is not None

    @staticmethod
    def check_dependencies() -> list[str]:
        """Return the argv prefix, raising when SGLang cannot run here."""
        if detect_gpu_type() != "nvidia_cuda":
            raise RuntimeError(
                "SGLang backend requires an NVIDIA CUDA host "
                f"(this host reports gpu_type={detect_gpu_type()!r})"
            )
        executable = resolve_executable()
        if executable is None:
            raise RuntimeError(
                "SGLang executable not found. Set SGLANG_VENV_PATH to the "
                "virtualenv SGLang is installed into, or install sglang so "
                "that it is on PATH."
            )
        return executable

    def build_command(self, instance: Any) -> list[str]:
        """Build the `sglang serve` command from the instance config."""
        cmd = list(self.check_dependencies())
        config = instance.config

        if not config.model_path:
            raise RuntimeError(
                "SGLang instance has no model_path — the model source must be "
                "resolved before the instance is started"
            )

        cmd += [
            "--model-path",
            config.model_path,
            "--served-model-name",
            served_model_name(config.alias),
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

        if settings.sglang_prompt_cache_dir.strip():
            for field, flag in _STORAGE_FLAGS:
                value = getattr(config, field, None)
                if value is not None:
                    cmd += [flag, str(value)]
        elif any(
            getattr(config, field, None) is not None for field, _ in _STORAGE_FLAGS
        ):
            logger.warning(
                "Ignoring hicache storage settings for instance %s: "
                "SGLANG_PROMPT_CACHE_DIR is not configured on this host",
                instance.id,
            )

        # Last, so a raw override beats the typed flag above it.
        if config.extra_args:
            cmd += list(config.extra_args)

        return cmd

    def build_env(self, instance: Any) -> dict[str, str]:
        """Activate the SGLang venv and point its prompt cache at this instance."""
        env: dict[str, str] = {}
        config = instance.config

        venv = settings.sglang_venv_path.strip()
        if venv:
            venv_path = Path(venv)
            env["VIRTUAL_ENV"] = str(venv_path)
            env["PATH"] = f"{venv_path / 'bin'}:{os.environ.get('PATH', '')}"
            # An inherited PYTHONHOME would send the interpreter looking for
            # its stdlib outside the venv, which is why `activate` unsets it.
            # CPython reads an empty value as unset, and build_env can only
            # add variables, so empty is how the removal is expressed.
            env["PYTHONHOME"] = ""

        cache_root = settings.sglang_prompt_cache_dir.strip()
        if cache_root:
            alias_safe = config.alias.replace(":", "-").replace("/", "-")
            cache_dir = Path(cache_root) / f"{alias_safe}-{instance.id}"
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                env[HICACHE_STORAGE_DIR_ENV] = str(cache_dir)
            except OSError as exc:
                logger.warning(
                    "Cannot create SGLang prompt cache dir %s: %s", cache_dir, exc
                )

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
        """Initialize parsing context for SGLang scheduler log parsing."""
        return {
            "recent_generations": [],
            "last_state": {
                "busy": False,
                "phase": InstancePhase.IDLE.value,
            },
        }

    def parse_log_line(
        self, instance_id: str, line: str, context: dict[str, Any]
    ) -> RuntimeStateUpdate | None:
        """Derive busy/phase and decode throughput from the scheduler lines.

        SGLang logs one line per scheduler step rather than per request, so
        the running-request count is the only reliable busy signal: a decode
        step with no running requests means the queue drained.
        """
        is_prefill = _RE_PREFILL_BATCH.search(line) is not None
        is_decode = _RE_DECODE_BATCH.search(line) is not None
        if not is_prefill and not is_decode:
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

        if is_prefill:
            busy = True
            phase = InstancePhase.PREFILL
            active_slots = running if running is not None else 1
        else:
            busy = running is None or running > 0
            phase = InstancePhase.GENERATING if busy else InstancePhase.IDLE
            active_slots = running if running is not None else (1 if busy else 0)

        if decode_tps is not None and busy:
            metrics = GenerationMetrics(
                instance_id=instance_id,
                decode_tps=decode_tps,
                decode_ms_per_token=(1000.0 / decode_tps) if decode_tps > 0 else None,
            )
            recent = context.get("recent_generations", [])
            recent.append(metrics)
            if len(recent) > 100:
                recent = recent[-100:]
            context["recent_generations"] = recent

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
