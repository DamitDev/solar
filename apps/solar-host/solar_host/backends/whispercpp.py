"""whisper.cpp backend runner implementation.

whisper.cpp ships as a separate binary (``whisper-server``), like SGLang and
vLLM, so the runner resolves an executable and the host does not advertise the
backend when it is missing. The binary speaks ``/inference`` rather than the OpenAI
path, so each instance is fronted by ``solar_host.servers.audio_server``, which owns
the OpenAI contract and forwards to the child.
"""

import re
import shutil
import sys
from typing import Any

from solar_host.backends.base import BackendRunner, RuntimeStateUpdate
from solar_host.config import settings
from solar_host.models.base import BackendType, InstancePhase

# The shim prints this line only after the child runtime answers its own health
# route and is alive. The uvicorn banner is deliberately NOT the readiness
# signal: it appears before the child can serve anything.
_RE_READY = re.compile(r"\[READY\] runtime ready")

# Structured shim log lines. duration_s is the audio duration the runtime
# reported, not a token count, so no token metric is invented here.
_RE_REQUEST = re.compile(r"\[REQUEST\] model=(\S+) endpoint=(\S+) format=(\S+)")
_RE_COMPLETE = re.compile(
    r"\[COMPLETE\] model=(\S+) duration_s=([0-9.]+) time_ms=([0-9.]+)"
)
_RE_ERROR = re.compile(r"\[ERROR\] (.+)")


def resolve_executable() -> str | None:
    """Return the whisper-server executable, or None when it cannot resolve."""
    configured = settings.whispercpp_binary
    if configured and shutil.which(configured):
        return configured
    return shutil.which("whisper-server")


def is_supported() -> bool:
    """True when this host can actually run the whisper.cpp backend.

    whisper.cpp runs on CPU as well as on GPUs, so no GPU requirement applies.
    """
    return resolve_executable() is not None


class WhisperCppRunner(BackendRunner):
    """Backend runner for whisper.cpp instances."""

    def get_backend_type(self) -> str:
        return BackendType.WHISPERCPP.value

    def is_ready_line(self, line: str) -> bool:
        """True when the shim reports that the child runtime can serve."""
        return _RE_READY.search(line) is not None

    def build_command(self, instance: Any) -> list[str]:
        """Build the audio shim command; the shim spawns whisper-server."""
        executable = resolve_executable()
        if not executable:
            raise RuntimeError(
                "whisper-server not found; install whisper.cpp or set "
                "WHISPERCPP_BINARY"
            )

        config = instance.config
        cmd = [
            sys.executable,
            "-m",
            "solar_host.servers.audio_server",
            "--model-type",
            "whispercpp",
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
            "--language",
            config.language,
            "--threads",
            str(config.threads),
            "--device",
            config.device,
            "--runtime-timeout-s",
            str(settings.instance_ready_timeout_s),
            "--request-timeout-s",
            str(settings.audio_request_timeout_s),
        ]
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

    def get_supported_endpoints_for_model_type(self, backend_type: str) -> list[str]:
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
