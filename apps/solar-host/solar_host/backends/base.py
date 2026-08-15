"""Abstract base class for backend runners."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from solar_host.models.base import InstancePhase, InstanceUsageSnapshot


@dataclass
class RuntimeStateUpdate:
    """Represents a runtime state update parsed from log output."""

    busy: bool
    phase: InstancePhase
    prefill_progress: float | None = None
    active_slots: int = 0
    slot_id: int | None = None
    task_id: int | None = None
    prefill_prompt_tokens: int | None = None
    generated_tokens: int | None = None
    decode_tps: float | None = None
    decode_ms_per_token: float | None = None
    checkpoint_index: int | None = None
    checkpoint_total: int | None = None


class BackendRunner(ABC):
    """Abstract base class for backend-specific runners.

    Each backend type (llama.cpp, HuggingFace, etc.) implements this interface
    to handle process spawning, log parsing, and health checking.
    """

    @abstractmethod
    def build_command(self, instance: Any) -> list[str]:
        """Build the command to start the backend process.

        Args:
            instance: The Instance object containing config and runtime info.

        Returns:
            List of command arguments to spawn the process.
        """

    @abstractmethod
    def parse_log_line(
        self, instance_id: str, line: str, context: dict[str, Any]
    ) -> RuntimeStateUpdate | None:
        """Parse a log line and optionally return a runtime state update.

        Args:
            instance_id: The instance ID this log belongs to.
            line: The log line to parse.
            context: Mutable context dict for tracking state across log lines
                     (e.g., active slots, pending generations).

        Returns:
            RuntimeStateUpdate if the log line indicates a state change, None otherwise.
        """

    @abstractmethod
    def get_health_endpoint(self) -> str:
        """Get the health check endpoint path for this backend.

        Returns:
            The health endpoint path (e.g., "/health").
        """

    @abstractmethod
    def get_supported_endpoints(self) -> list[str]:
        """Get the list of API endpoints this backend supports.

        Returns:
            List of endpoint paths (e.g., ["/v1/chat/completions", "/v1/completions"]).
        """

    @abstractmethod
    def get_backend_type(self) -> str:
        """Get the backend type identifier.

        Returns:
            The backend type string (e.g., "llamacpp", "huggingface_causal").
        """

    def get_served_model_name(self, config: Any) -> str:
        """The model name the backend process answers to.

        Normally the alias itself, which is also what solar-control routes on.
        Override when the backend cannot serve the alias verbatim (SGLang reads
        ``:`` as a LoRA separator), so control can translate the request's
        ``model`` field instead of the operator having to rename the model.

        Args:
            config: The instance config.

        Returns:
            The name the backend serves the model under.
        """
        return config.alias

    def build_env(self, instance: Any) -> dict[str, str]:
        """Extra environment variables for the backend process.

        Merged over the inherited environment at spawn time. Override for
        backends configured through the environment rather than argv (e.g.
        SGLang's venv activation and prompt cache directory).

        Args:
            instance: The Instance object containing config and runtime info.

        Returns:
            Mapping of variable name to value; empty by default.
        """
        return {}

    def initialize_context(self) -> dict[str, Any]:
        """Initialize the parsing context for a new instance.

        Override this method to provide backend-specific context initialization.

        Returns:
            Initial context dictionary.
        """
        return {}

    def on_process_started(self, instance_id: str, context: dict[str, Any]) -> None:
        """Called when the backend process has started.

        Override this method to perform post-start initialization.

        Args:
            instance_id: The instance ID.
            context: The instance's parsing context.
        """

    def on_process_stopped(self, instance_id: str, context: dict[str, Any]) -> None:
        """Called when the backend process has stopped.

        Override this method to perform cleanup.

        Args:
            instance_id: The instance ID.
            context: The instance's parsing context.
        """

    def get_supported_endpoints_for_type(self, backend_type: str) -> list[str]:
        """Get supported endpoints based on specific backend type.

        Override this method if the backend supports multiple model types
        with different endpoints (e.g., HuggingFace causal vs classification).

        Args:
            backend_type: The specific backend type string.

        Returns:
            List of endpoint paths for that backend type.
        """
        return self.get_supported_endpoints()

    def get_last_generation(self, context: dict[str, Any]) -> Any | None:
        """Get the last generation metrics from context.

        Override this method to provide generation metrics tracking.

        Args:
            context: The instance's parsing context.

        Returns:
            GenerationMetrics if available, None otherwise.
        """
        return None

    def is_ready_line(self, line: str) -> bool:
        """True when *line* proves the backend is accepting requests.

        The lifecycle status only moves starting -> running on this signal;
        a live process is not evidence that the model is loaded.
        """
        return False

    def get_metrics_path(self) -> str | None:
        """Path of the backend's Prometheus endpoint, or None if it has none.

        Hosts launch backends with the metrics flag enabled (llama.cpp
        ``--metrics --slots``, SGLang ``--enable-metrics``); the returned
        path is where the counters live for the metrics poll loop.
        """
        return None

    def parse_metrics(self, text: str) -> InstanceUsageSnapshot | None:
        """Parse /metrics exposition text into an instance usage snapshot.

        Returns None when the text carries none of the backend's own
        counters (e.g. a generic process metrics body only).
        """
        return None

    def apply_usage_snapshot(
        self,
        instance_id: str,
        context: dict[str, Any],
        snapshot: InstanceUsageSnapshot,
    ) -> RuntimeStateUpdate | None:
        """React to a fresh usage snapshot for *instance_id*.

        The metrics poll loop calls this after storing each snapshot. The
        base implementation does nothing; backends override it to drive the
        authoritative busy signal from the backend's own counters and to
        finalize per-request metrics from counter deltas (SGLang).

        Args:
            instance_id: The instance ID.
            context: The instance's parsing context.
            snapshot: The freshly parsed snapshot.

        Returns:
            RuntimeStateUpdate if the snapshot changes the runtime state,
            None otherwise.
        """
        return None
