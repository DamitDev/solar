"""Per-instance capabilities for the OpenAI /v1/models advertisement.

Downstream consumers (the orchestrator's vision sensor) read the
``capabilities`` sequence from the OpenAI model listing. llama.cpp and the
HF server advertise their own capabilities, but SGLang's /v1/models carries
none — so the host derives them from the model directory and reports them
alongside the instance, and solar-control stamps them onto the listing.

The detector stays quiet (returns ``None``) when it cannot positively
identify a vision model: text/embedding/classification models keep their
current advertisement, and an upstream advertisement is never overridden.
"""

import json
from pathlib import Path
from typing import Any

VISION_CAPABILITIES: list[str] = ["completion", "multimodal"]

# HF ``model_type`` values known to be multimodal, checked when the
# checkpoint's config.json has no ``vision_config`` key (that key is the
# primary marker; this is the fallback for checkpoints that omit it).
_KNOWN_VISION_MODEL_TYPES: frozenset[str] = frozenset(
    {
        "qwen2_vl",
        "qwen2_5_vl",
        "qwen3_vl",
        "llava",
        "llava_next",
        "idefics3",
        "internvl_chat",
        "minicpmv",
        "moondream2",
        "paligemma",
        "phi3_v",
        "pixtral",
        "smolvlm",
    }
)


def _model_dir(config: Any) -> Path | None:
    """Resolve the local model directory from an instance config, if any."""
    path = getattr(config, "model_path", None) or getattr(config, "model_id", None)
    if isinstance(path, str) and path:
        candidate = Path(path)
        if candidate.is_dir():
            return candidate

    source = getattr(config, "model_source", None)
    if isinstance(source, str) and source.startswith("local://"):
        candidate = Path(source[len("local://") :])
        if candidate.is_dir():
            return candidate

    return None


def _load_model_config(model_dir: Path) -> dict[str, Any] | None:
    """Read a checkpoint's config.json, tolerating any read or parse error."""
    config_file = model_dir / "config.json"
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def capabilities_for_config(config: Any) -> list[str] | None:
    """Return the capabilities list for an instance config, or ``None``.

    Only positive vision detection produces a value: multimodal chat models
    advertise ``["completion", "multimodal"]``; everything else stays
    ``None`` so upstream advertisements keep their authority.
    """
    backend_type = getattr(config, "backend_type", "llamacpp")

    if backend_type == "huggingface_vision":
        return list(VISION_CAPABILITIES)

    if backend_type == "llamacpp":
        if getattr(config, "mmproj", None):
            return list(VISION_CAPABILITIES)
        return None

    if backend_type not in ("sglang", "huggingface_causal"):
        return None

    model_dir = _model_dir(config)
    if model_dir is None:
        return None

    checkpoint_config = _load_model_config(model_dir)
    if checkpoint_config is None:
        return None

    if "vision_config" in checkpoint_config:
        return list(VISION_CAPABILITIES)

    model_type = str(checkpoint_config.get("model_type", ""))
    if model_type in _KNOWN_VISION_MODEL_TYPES or model_type.endswith("_vl"):
        return list(VISION_CAPABILITIES)

    return None
