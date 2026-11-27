"""Backend runners package for solar-host."""

from solar_host.backends.base import BackendRunner, RuntimeStateUpdate
from solar_host.backends.huggingface import HuggingFaceRunner
from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.backends.nemospeech import NemoSpeechRunner
from solar_host.backends.nemospeech import is_supported as nemospeech_is_supported
from solar_host.backends.sglang import SglangRunner
from solar_host.backends.sglang import is_supported as sglang_is_supported
from solar_host.backends.vllm import VllmRunner
from solar_host.backends.vllm import is_supported as vllm_is_supported
from solar_host.backends.whispercpp import WhisperCppRunner
from solar_host.backends.whispercpp import is_supported as whispercpp_is_supported
from solar_host.models.base import BackendType

__all__ = [
    "BackendRunner",
    "HuggingFaceRunner",
    "LlamaCppRunner",
    "NemoSpeechRunner",
    "RuntimeStateUpdate",
    "SglangRunner",
    "VllmRunner",
    "WhisperCppRunner",
    "supported_backend_types",
]


def supported_backend_types() -> list[str]:
    """The backend types this host can actually run.

    llama.cpp and HuggingFace ship with solar-host itself, so they are always
    advertised. SGLang, vLLM, whisper.cpp and NeMo-Speech.cpp need a separate
    install, so solar-control must not place their instances here unless the
    executable resolves. whisper.cpp and NeMo-Speech.cpp are not CUDA-only: their
    GGML runtimes run on a CPU as well.
    """
    return [
        bt.value
        for bt in BackendType
        if (bt is not BackendType.SGLANG or sglang_is_supported())
        and (bt is not BackendType.VLLM or vllm_is_supported())
        and (bt is not BackendType.WHISPERCPP or whispercpp_is_supported())
        and (bt is not BackendType.NEMOSPEECH or nemospeech_is_supported())
    ]
