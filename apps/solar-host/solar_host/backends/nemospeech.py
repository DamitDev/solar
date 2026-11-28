"""NeMo-Speech.cpp backend runner implementation.

NeMo-Speech.cpp ships as a separate binary (``nemo-speech``), like SGLang and
vLLM, so the runner resolves an executable and the host does not advertise the
backend when it is missing. The binary's ``serve`` command already exposes an
OpenAI-compatible ``/v1/audio/transcriptions`` subset, but not the whole solar
contract (no ``diarized_json``, no ``segments[]``), so each instance is fronted by
``solar_host.servers.audio_server``, which owns the contract and forwards to it.
"""

import re
import shutil
import sys
from typing import Any

from solar_host.backends.base import BackendRunner, RuntimeStateUpdate
from solar_host.config import settings
from solar_host.models.base import BackendType, InstancePhase

# The shim prints this line only after the child runtime answers its own
# readiness route and is alive. The uvicorn banner is deliberately NOT the
# readiness signal: it appears before the child can serve anything.
_RE_READY = re.compile(r"\[READY\] runtime ready")

# Structured shim log lines. elapsed_s is the wall-clock time the shim spent on
# the request, not a token count or the audio duration, so no token metric is
# invented here.
_RE_REQUEST = re.compile(r"\[REQUEST\] model=(\S+) endpoint=(\S+) format=(\S+)")
_RE_COMPLETE = re.compile(
    r"\[COMPLETE\] model=(\S+) elapsed_s=([0-9.]+) time_ms=([0-9.]+)"
)
_RE_ERROR = re.compile(r"\[ERROR\] (.+)")


def resolve_executable() -> str | None:
    """Return the nemo-speech executable, or None when it cannot resolve."""
    configured = settings.nemo_speech_binary
    if configured and shutil.which(configured):
        return configured
    return shutil.which("nemo-speech")


def is_supported() -> bool:
    """True when this host can actually run the NeMo-Speech.cpp backend.

    The GGML runtime runs on CPU as well as on GPUs, so no GPU requirement
    applies; the device choice is per instance (``--device cpu`` / ``cuda``).
    """
    return resolve_executable() is not None


class NemoSpeechRunner(BackendRunner):
    """Backend runner for NeMo-Speech.cpp instances."""

    def get_backend_type(self) -> str:
        return BackendType.NEMOSPEECH.value

    def is_ready_line(self, line: str) -> bool:
        """True when the shim reports that the child runtime can serve."""
        return _RE_READY.search(line) is not None

    def build_command(self, instance: Any) -> list[str]:
        """Build the audio shim command; the shim spawns nemo-speech serve."""
        executable = resolve_executable()
        if not executable:
            raise RuntimeError(
                "nemo-speech not found; install NeMo-Speech.cpp or set "
                "NEMO_SPEECH_BINARY"
            )

        config = instance.config
        cmd = [
            sys.executable,
            "-m",
            "solar_host.servers.audio_server",
            "--model-type",
            "nemospeech",
            "--runtime-binary",
            executable,
            "--model",
            config.model,
            "--alias",
            config.alias,
            "--host",
            config.host,
            "--port",
            str(instance.port),
            "--device",
            config.device,
            "--runtime-timeout-s",
            str(settings.instance_ready_timeout_s),
            "--request-timeout-s",
            str(settings.audio_request_timeout_s),
        ]
        if config.encoder:
            # The shim's own option is --encoder; it translates the value to
            # the child runtime's --salm-encoder (v0.3.0's spelling, because
            # --asr.salm.encoder_path is rejected).
            cmd.extend(["--encoder", config.encoder])
        if config.diar_model:
            cmd.extend(["--diar-model", config.diar_model])
        if config.language:
            cmd.extend(["--language", config.language])
        if config.instruction:
            cmd.extend(["--instruction", config.instruction])
        if config.translate_to:
            cmd.extend(["--translate-to", config.translate_to])
        if config.diarize:
            cmd.append("--diarize")
        return cmd

    def build_env(self, instance: Any) -> dict[str, str]:
        """Deliver the host key via SOLAR_API_KEY (IT Sec #97: never argv)."""
        env = super().build_env(instance)
        if settings.api_key:
            env["SOLAR_API_KEY"] = settings.api_key
        return env

    def get_health_endpoint(self) -> str:
        return "/health"

    def get_supported_endpoints(self) -> list[str]:
        return ["/v1/audio/transcriptions", "/v1/models", "/health"]

    def get_supported_endpoints_for_model_type(self, _backend_type: str) -> list[str]:
        """The GGML runtimes are single-purpose: one endpoint set."""
        return ["/v1/audio/transcriptions", "/v1/models", "/health"]

    def initialize_context(self) -> dict[str, Any]:
        return {
            "busy": False,
            "current_request": None,
            "recent_requests": [],
            "last_state": {"busy": False, "phase": InstancePhase.IDLE.value},
        }

    def parse_log_line(
        self, instance_id: str, line: str, context: dict[str, Any]
    ) -> RuntimeStateUpdate | None:
        last_state = context.get("last_state", {})

        if self.is_ready_line(line):
            return self._create_update(
                busy=False,
                phase=InstancePhase.IDLE,
                last_state=last_state,
                context=context,
            )

        match = _RE_REQUEST.search(line)
        if match:
            context["busy"] = True
            context["current_request"] = {
                "model": match.group(1),
                "endpoint": match.group(2),
                "format": match.group(3),
            }
            return self._create_update(
                busy=True,
                phase=InstancePhase.GENERATING,
                last_state=last_state,
                context=context,
            )

        if _RE_COMPLETE.search(line):
            context["busy"] = False
            context["current_request"] = None
            return self._create_update(
                busy=False,
                phase=InstancePhase.IDLE,
                last_state=last_state,
                context=context,
            )

        if _RE_ERROR.search(line):
            context["busy"] = False
            context["current_request"] = None
            return self._create_update(
                busy=False,
                phase=InstancePhase.IDLE,
                last_state=last_state,
                context=context,
            )

        return None

    def _create_update(
        self,
        busy: bool,
        phase: InstancePhase,
        last_state: dict[str, Any],
        context: dict[str, Any],
    ) -> RuntimeStateUpdate | None:
        if last_state.get("busy") == busy and last_state.get("phase") == phase.value:
            return None
        context["last_state"] = {"busy": busy, "phase": phase.value}
        return RuntimeStateUpdate(
            busy=busy,
            phase=phase,
            active_slots=1 if busy else 0,
        )
