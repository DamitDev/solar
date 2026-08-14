"""Backend runners package for solar-host."""

from solar_host.backends.base import BackendRunner, RuntimeStateUpdate
from solar_host.backends.huggingface import HuggingFaceRunner
from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.backends.sglang import SglangRunner
from solar_host.backends.sglang import is_supported as sglang_is_supported
from solar_host.models.base import BackendType

__all__ = [
    "BackendRunner",
    "HuggingFaceRunner",
    "LlamaCppRunner",
    "RuntimeStateUpdate",
    "SglangRunner",
    "supported_backend_types",
]


def supported_backend_types() -> list[str]:
    """The backend types this host can actually run.

    llama.cpp and HuggingFace ship with solar-host itself, so they are always
    advertised. SGLang needs a separate CUDA-only install, so solar-control
    must not place an SGLang instance here unless it is present.
    """
    return [
        bt.value
        for bt in BackendType
        if bt is not BackendType.SGLANG or sglang_is_supported()
    ]
