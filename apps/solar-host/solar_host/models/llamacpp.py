"""LlamaCpp backend configuration models."""

import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _coerce_template_kwargs(value: Any) -> Any:
    """Recursively turn boolean-looking strings into real booleans.

    The webui stores ``chat_template_kwargs`` as free text, so users can
    accidentally quote booleans (``{"enable_thinking": "true"}``). llama.cpp
    validates the kwargs against the model's chat-template JSON schema per
    request and answers 400 ``invalid type for ... (expected boolean, got
    string)``. Normalizing at the config boundary keeps the CLI flag (and any
    API consumer) free of string-typed booleans.
    """
    if isinstance(value, dict):
        return {k: _coerce_template_kwargs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_template_kwargs(v) for v in value]
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return value


def _serialize_template_kwargs(value: Any) -> str:
    """Compact, canonical JSON serialization for chat template kwargs."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalize_csv(value: Any) -> Any:
    """Collapse a comma-separated list to its canonical ``a,b,c`` form.

    solar-control compares the stored intent value against the instance config
    to detect drift, so ``"0, 1"`` normalized here but stored verbatim there
    would look like a config change on every reconciliation pass. Both sides
    normalize identically (pinned by a test).
    """
    if not isinstance(value, str):
        return value
    parts = [part.strip() for part in value.split(",")]
    return ",".join(part for part in parts if part) or None


class LlamaCppConfig(BaseModel):
    """Configuration for a llama.cpp server instance.

    Note: api_key is NOT a config parameter - instances always use the host's API key.
    """

    model_config = ConfigDict(protected_namespaces=())

    backend_type: Literal["llamacpp"] = Field(
        default="llamacpp", description="Backend type identifier"
    )

    @model_validator(mode="before")
    @classmethod
    def strip_api_key(cls, data: Any) -> Any:
        """Remove api_key from old configs - instances use host API key."""
        if isinstance(data, dict):
            data.pop("api_key", None)
        return data

    model_source: str | None = Field(
        default=None, description="Model source URI (e.g. local://path/to/model.gguf)"
    )
    model: str | None = Field(default=None, description="Path to the GGUF model file")
    model_file: str | None = Field(
        default=None,
        description=(
            "Filename, relative path or glob (e.g. '*UD-Q4_K_XL*.gguf') selecting "
            "the GGUF inside the pulled model directory; resolved into 'model'"
        ),
    )
    file_filters: list[str] | None = Field(
        default=None,
        description=(
            "HuggingFace download filters (allow_patterns) applied when pulling "
            "the model, e.g. ['*UD-Q4_K_XL*', 'mmproj-BF16.gguf']"
        ),
    )

    @model_validator(mode="after")
    def check_model_or_source(self) -> "LlamaCppConfig":
        # model_file alone is not enough: it is a selector that needs a
        # directory to resolve against, which model/model_source provides.
        if not self.model and not self.model_source:
            raise ValueError("Either 'model' or 'model_source' must be provided")
        return self

    mmproj: str | None = Field(
        default=None,
        description=(
            "Path to the multimodal projector GGUF for vision models; a bare "
            "filename or glob is resolved inside the model directory"
        ),
    )
    mmproj_offload: bool = Field(
        default=True,
        description="Whether to GPU-offload the multimodal projector (default: enabled)",
    )
    alias: str = Field(..., description="Model alias (e.g., gpt-oss:120b)")
    threads: int = Field(default=1, description="Number of threads")
    n_gpu_layers: int = Field(default=999, description="Number of GPU layers")
    devices: str | None = Field(
        default=None,
        description=(
            "Comma-separated list of devices to offload to (--device), e.g. "
            "'CUDA0,CUDA1'; 'none' disables offloading"
        ),
    )
    split_mode: Literal["none", "layer", "row", "tensor"] | None = Field(
        default=None,
        description=(
            "How to split the model across GPUs (--split-mode): none (single "
            "GPU), layer (default), row, or tensor (experimental)"
        ),
    )
    tensor_split: str | None = Field(
        default=None,
        description=(
            "Comma-separated proportions of the model to offload to each GPU "
            "(--tensor-split), e.g. '3,1'"
        ),
    )
    main_gpu: int | None = Field(
        default=None,
        ge=0,
        description=(
            "GPU used for the model with split_mode 'none', or for KV and "
            "intermediate results with split_mode 'row' (--main-gpu)"
        ),
    )

    @field_validator("devices", "tensor_split", mode="before")
    @classmethod
    def normalize_device_lists(cls, raw: Any) -> Any:
        """Canonicalize the comma-separated multi-GPU lists."""
        return _normalize_csv(raw)

    @field_validator("tensor_split", mode="after")
    @classmethod
    def check_tensor_split(cls, value: str | None) -> str | None:
        """Reject a tensor split llama-server would parse as zeros.

        llama.cpp reads the list with strtod and silently treats anything
        unparseable as 0.0, which loads the whole model onto one GPU instead
        of failing — a config error better surfaced at create time.
        """
        if value is None:
            return None
        for part in value.split(","):
            try:
                proportion = float(part)
            except ValueError as exc:
                raise ValueError(
                    f"tensor_split must be comma-separated numbers, got '{part}'"
                ) from exc
            if not math.isfinite(proportion):
                raise ValueError(
                    f"tensor_split must be comma-separated numbers, got '{part}'"
                )
            if proportion < 0:
                raise ValueError("tensor_split proportions must not be negative")
        return value

    temp: float = Field(default=1.0, description="Temperature")
    top_p: float = Field(default=1.0, description="Top-p sampling")
    top_k: int = Field(default=0, description="Top-k sampling")
    min_p: float = Field(default=0.0, description="Min-p sampling")
    ctx_size: int = Field(default=131072, description="Context size")
    chat_template_file: str | None = Field(
        default=None, description="Path to Jinja chat template"
    )
    chat_template_kwargs: str | None = Field(
        default=None,
        description="JSON string of chat template kwargs (e.g. '{\"enable_thinking\":true}')",
    )

    @field_validator("chat_template_kwargs", mode="before")
    @classmethod
    def normalize_chat_template_kwargs(cls, raw: Any) -> Any:
        """Parse, normalize, and re-serialize chat template kwargs.

        Accepts a JSON string (webui form value) or a dict (programmatic
        use). Boolean-looking strings are coerced to real booleans and the
        result is stored as compact canonical JSON, so the value handed to
        llama-server can never carry string-typed booleans. Invalid JSON
        raises immediately instead of surfacing as a runtime 400 per request.
        """
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"chat_template_kwargs is not valid JSON: {exc}"
                ) from exc
        else:
            parsed = raw
        return _serialize_template_kwargs(_coerce_template_kwargs(parsed))

    reasoning: Literal["on", "off", "auto"] | None = Field(
        default=None,
        description="Reasoning/thinking mode: 'on', 'off', or 'auto' (passed as --reasoning to llama-server)",
    )
    reasoning_budget: int | None = Field(
        default=None,
        description="Reasoning budget token limit (passed as --reasoning-budget to llama-server)",
    )
    spec_type: Literal["draft-mtp", "draft-dspark"] | None = Field(
        default=None,
        description=(
            "Speculative decoding type (passed as --spec-type to llama-server): "
            "'draft-mtp' uses the main model's MTP heads, 'draft-dspark' needs a "
            "separate DSpark draft model"
        ),
    )
    spec_draft_model: str | None = Field(
        default=None,
        description=(
            "Path to the DSpark draft GGUF (--spec-draft-model); a bare filename "
            "or glob is resolved inside the model directory"
        ),
    )
    spec_draft_n_max: int | None = Field(
        default=None,
        ge=1,
        description="Maximum speculative draft tokens (passed as --spec-draft-n-max to llama-server)",
    )
    spec_draft_conf_min: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Truncate a drafted block at the first position whose predicted "
            "acceptance falls below this value (--spec-draft-conf-min); requires "
            "a draft model with a confidence head"
        ),
    )

    @model_validator(mode="after")
    def check_speculative_decoding(self) -> "LlamaCppConfig":
        """Keep the speculative fields consistent with the chosen ``spec_type``.

        llama-server accepts the draft-model flags with any ``--spec-type`` but
        silently ignores them unless the type actually loads a draft model, so
        a mismatch here is a config error the user wants to hear about at
        create time rather than debug from a missing speedup.
        """
        if self.spec_type == "draft-dspark" and not (
            self.spec_draft_model and self.spec_draft_model.strip()
        ):
            raise ValueError("spec_type 'draft-dspark' requires 'spec_draft_model'")
        if self.spec_type != "draft-dspark":
            if self.spec_draft_model and self.spec_draft_model.strip():
                raise ValueError(
                    "'spec_draft_model' is only supported with spec_type 'draft-dspark'"
                )
            if self.spec_draft_conf_min is not None:
                raise ValueError(
                    "'spec_draft_conf_min' is only supported with spec_type 'draft-dspark'"
                )
        return self

    cache_type_k: (
        Literal["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"]
        | None
    ) = Field(default=None, description="KV cache quantization type for keys (-ctk)")
    cache_type_v: (
        Literal["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"]
        | None
    ) = Field(default=None, description="KV cache quantization type for values (-ctv)")
    rope_scaling: Literal["none", "linear", "yarn"] | None = Field(
        default=None, description="RoPE scaling method (--rope-scaling)"
    )
    rope_scale: float | None = Field(
        default=None, description="RoPE context scaling factor (--rope-scale)"
    )
    yarn_orig_ctx: int | None = Field(
        default=None, description="YaRN original context size (--yarn-orig-ctx)"
    )
    host: str = Field(default="0.0.0.0", description="Host to bind to")
    port: int | None = Field(
        default=None, description="Port (auto-assigned if not specified)"
    )
    special: bool = Field(
        default=False, description="Enable llama-server --special flag"
    )
    ot: str | None = Field(
        default=None,
        description="Override tensor string (passed as -ot flag to llama-server)",
    )
    model_type: Literal["llm", "embedding", "reranker"] | None = Field(
        default="llm", description="Model type: llm (default), embedding, or reranker"
    )
    pooling: Literal["none", "mean", "cls", "last", "rank"] | None = Field(
        default=None,
        description="Pooling strategy for embedding models (only valid when model_type is embedding)",
    )
