"""Models package for solar-host with multi-backend support."""

from typing import Annotated

from pydantic import Field

# Import base models first (no dependencies on config types)
from solar_host.models.base import (
    BackendType,
    GenerationMetrics,
    Instance,
    InstanceCreate,
    InstancePhase,
    InstancePriority,
    InstanceResponse,
    InstanceRuntimeState,
    InstanceStateEvent,
    InstanceStatus,
    InstanceUpdate,
    InstanceUsageSnapshot,
    LogMessage,
    MemoryInfo,
)
from solar_host.models.huggingface import (
    HuggingFaceCausalConfig,
    HuggingFaceClassificationConfig,
    HuggingFaceEmbeddingConfig,
    HuggingFaceVisionConfig,
)

# Import config models
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.models.sglang import SglangConfig

# Create the discriminated union type for InstanceConfig
InstanceConfig = Annotated[
    LlamaCppConfig
    | HuggingFaceCausalConfig
    | HuggingFaceClassificationConfig
    | HuggingFaceEmbeddingConfig
    | HuggingFaceVisionConfig
    | SglangConfig,
    Field(discriminator="backend_type"),
]

__all__ = [
    # Enums
    "BackendType",
    "GenerationMetrics",
    "HuggingFaceCausalConfig",
    "HuggingFaceClassificationConfig",
    "HuggingFaceEmbeddingConfig",
    "HuggingFaceVisionConfig",
    # Instance models
    "Instance",
    # Config types
    "InstanceConfig",
    "InstanceCreate",
    "InstancePhase",
    "InstancePriority",
    "InstanceResponse",
    "InstanceRuntimeState",
    "InstanceStateEvent",
    "InstanceStatus",
    "InstanceUpdate",
    "InstanceUsageSnapshot",
    "LlamaCppConfig",
    # Runtime models
    "LogMessage",
    # Other
    "MemoryInfo",
    "SglangConfig",
]
