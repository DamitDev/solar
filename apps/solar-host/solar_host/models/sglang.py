"""SGLang backend configuration model.

SGLang's flag surface is large and moves fast, so this model carries typed
fields for the knobs that are stable and commonly tuned, plus ``extra_args``
and ``extra_env`` escape hatches for everything else. A typed field maps to
exactly one CLI flag in :mod:`solar_host.backends.sglang`; ``extra_args`` is
appended last so a raw override wins over the typed value.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Flags solar-host owns: the port comes from the host's allocator, the API key
# from host settings, and the served name from the alias the gateway routes on.
# Overriding them through extra_args would break routing or auth.
RESERVED_SGLANG_ARGS: frozenset[str] = frozenset(
    {
        "--host",
        "--port",
        "--api-key",
        "--model-path",
        "--served-model-name",
    }
)


class SglangConfig(BaseModel):
    """Configuration for an SGLang server instance.

    Note: api_key is NOT a config parameter - instances always use the host's
    API key.
    """

    model_config = ConfigDict(protected_namespaces=())

    backend_type: Literal["sglang"] = Field(
        default="sglang", description="Backend type identifier"
    )

    @model_validator(mode="before")
    @classmethod
    def strip_api_key(cls, data: Any) -> Any:
        """Remove api_key from configs - instances use the host API key."""
        if isinstance(data, dict):
            data.pop("api_key", None)
        return data

    model_source: str | None = Field(
        default=None, description="Model source URI (e.g. local://path/to/model)"
    )
    model_path: str | None = Field(
        default=None,
        description=(
            "Local path to the model directory served by SGLang (--model-path); "
            "resolved from model_source when omitted"
        ),
    )
    file_filters: list[str] | None = Field(
        default=None,
        description=(
            "HuggingFace download filters (allow_patterns) applied when pulling "
            "the model"
        ),
    )
    alias: str = Field(..., description="Model alias (e.g., deepseek-v4:flash)")
    host: str = Field(default="0.0.0.0", description="Host to bind to")
    port: int | None = Field(
        default=None, description="Port (auto-assigned if not specified)"
    )

    @model_validator(mode="after")
    def check_model_or_source(self) -> "SglangConfig":
        if not self.model_path and not self.model_source:
            raise ValueError("Either 'model_path' or 'model_source' must be provided")
        return self

    # ── Parallelism and memory ──────────────────────────────────
    tp_size: int | None = Field(
        default=None, ge=1, description="Tensor parallel size (--tp-size)"
    )
    dp_size: int | None = Field(
        default=None, ge=1, description="Data parallel size (--dp-size)"
    )
    context_length: int | None = Field(
        default=None, ge=1, description="Maximum context length (--context-length)"
    )
    mem_fraction_static: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description=(
            "Fraction of GPU memory reserved for weights and the KV pool "
            "(--mem-fraction-static)"
        ),
    )
    chunked_prefill_size: int | None = Field(
        default=None,
        description="Prefill chunk size in tokens (--chunked-prefill-size)",
    )
    max_running_requests: int | None = Field(
        default=None,
        ge=1,
        description="Maximum concurrently running requests (--max-running-requests)",
    )
    cuda_graph_max_bs: int | None = Field(
        default=None,
        ge=1,
        description="Largest batch size captured into a CUDA graph (--cuda-graph-max-bs)",
    )
    cuda_graph_max_bs_decode: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Largest decode batch size captured into a CUDA graph "
            "(--cuda-graph-max-bs-decode)"
        ),
    )
    swa_full_tokens_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Ratio of full-attention to sliding-window tokens for hybrid "
            "attention models (--swa-full-tokens-ratio)"
        ),
    )

    # ── Model and kernels ───────────────────────────────────────
    dtype: str | None = Field(
        default=None, description="Weight dtype, e.g. 'bfloat16' (--dtype)"
    )
    quantization: str | None = Field(
        default=None, description="Quantization method, e.g. 'fp8' (--quantization)"
    )
    kv_cache_dtype: str | None = Field(
        default=None, description="KV cache dtype, e.g. 'fp8_e4m3' (--kv-cache-dtype)"
    )
    moe_runner_backend: str | None = Field(
        default=None,
        description="MoE kernel backend, e.g. 'flashinfer_mxfp4' (--moe-runner-backend)",
    )
    speculative_algorithm: str | None = Field(
        default=None,
        description="Speculative decoding algorithm, e.g. 'EAGLE' (--speculative-algorithm)",
    )
    trust_remote_code: bool = Field(
        default=False,
        description="Allow the model repo's own modelling code (--trust-remote-code)",
    )

    # ── Hierarchical (prompt) cache ─────────────────────────────
    enable_hierarchical_cache: bool = Field(
        default=False,
        description="Enable the hierarchical prefix cache (--enable-hierarchical-cache)",
    )
    hicache_ratio: float | None = Field(
        default=None,
        gt=0.0,
        description="Host-to-device cache size ratio (--hicache-ratio)",
    )
    hicache_mem_layout: str | None = Field(
        default=None,
        description="Host cache memory layout, e.g. 'page_first_direct' (--hicache-mem-layout)",
    )
    hicache_io_backend: str | None = Field(
        default=None,
        description="Host cache IO backend, e.g. 'direct' (--hicache-io-backend)",
    )
    hicache_storage_backend: str | None = Field(
        default=None,
        description=(
            "Persistent cache backend, e.g. 'file' (--hicache-storage-backend); the "
            "'file' backend needs SGLANG_PROMPT_CACHE_DIR configured on the host"
        ),
    )
    hicache_storage_backend_extra_config: str | None = Field(
        default=None,
        description=(
            "JSON string of storage backend options, e.g. "
            '\'{"max_size":"256G","eviction_ratio":0.9}\' '
            "(--hicache-storage-backend-extra-config)"
        ),
    )
    hicache_storage_prefetch_policy: str | None = Field(
        default=None,
        description=(
            "Prefetch policy, e.g. 'wait_complete' (--hicache-storage-prefetch-policy)"
        ),
    )

    @field_validator("hicache_storage_backend_extra_config", mode="before")
    @classmethod
    def normalize_storage_extra_config(cls, raw: Any) -> Any:
        """Parse and re-serialize the extra config as compact canonical JSON.

        Accepts a JSON string (webui form value) or a dict (programmatic use).
        Storing the canonical form means solar-control's drift detection never
        sees a restart-worthy difference between ``{"a": 1}`` and ``{"a":1}``.
        """
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"hicache_storage_backend_extra_config is not valid JSON: {exc}"
                ) from exc
        else:
            parsed = raw
        # SGLang json.loads() this flag and indexes it, so a bare scalar or a
        # list would fail inside the server rather than here. Pydantic only
        # turns ValueError into a validation error, hence not TypeError.
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        raise ValueError("hicache_storage_backend_extra_config must be a JSON object")

    # ── Escape hatches ──────────────────────────────────────────
    extra_args: list[str] | None = Field(
        default=None,
        description=(
            "Raw CLI arguments appended after the typed flags, for SGLang options "
            "without a typed field (e.g. ['--dist-init-addr', '10.0.0.1:5000'])"
        ),
    )
    extra_env: dict[str, str] | None = Field(
        default=None,
        description=(
            "Extra environment variables for the SGLang process, e.g. "
            "{'SGLANG_DSV4_COMPRESS_STATE_DTYPE': 'bf16'}"
        ),
    )

    @field_validator("extra_args", mode="after")
    @classmethod
    def check_extra_args(cls, value: list[str] | None) -> list[str] | None:
        """Reject empty entries and the flags solar-host owns."""
        if value is None:
            return None
        cleaned: list[str] = []
        for arg in value:
            if not isinstance(arg, str) or not arg.strip():
                raise ValueError("extra_args must not contain empty entries")
            arg = arg.strip()
            # Both spellings reach SGLang's argparse identically.
            flag = arg.split("=", 1)[0]
            if flag in RESERVED_SGLANG_ARGS:
                raise ValueError(
                    f"'{flag}' is managed by solar-host and must not be set "
                    f"through extra_args"
                )
            cleaned.append(arg)
        return cleaned or None

    @field_validator("extra_env", mode="after")
    @classmethod
    def check_extra_env(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        """Reject empty variable names."""
        if value is None:
            return None
        for name in value:
            if not name.strip():
                raise ValueError("extra_env must not contain empty variable names")
        return value or None
