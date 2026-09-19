"""vLLM backend configuration model.

vLLM's flag surface is large and moves fast, so this model carries typed
fields for the knobs that are stable and commonly tuned, plus ``extra_args``
and ``extra_env`` escape hatches for everything else. A typed field maps to
exactly one CLI flag in :mod:`solar_host.backends.vllm`; ``extra_args`` is
appended last so a raw override wins over the typed value.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Flags solar-host owns: the port comes from the host's allocator, the API key
# from host settings, the served name from the alias the gateway routes on,
# and the prompt-token details flag plus the stats-log switch belong to the
# usage-accounting contract (disabling the stats line would drop the live
# decode throughput, disabling prompt-token details the cached split).
# Overriding them through extra_args would break routing, auth or telemetry.
RESERVED_VLLM_ARGS: frozenset[str] = frozenset(
    {
        "--host",
        "--port",
        "--api-key",
        "--model",
        "--served-model-name",
        "--enable-prompt-tokens-details",
        "--no-enable-prompt-tokens-details",
        "--disable-log-stats",
    }
)


class VllmConfig(BaseModel):
    """Configuration for a vLLM server instance.

    Note: api_key is NOT a config parameter - instances always use the host's
    API key.
    """

    model_config = ConfigDict(protected_namespaces=())

    backend_type: Literal["vllm"] = Field(
        default="vllm", description="Backend type identifier"
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
            "Local path to the model directory served by vLLM (the positional "
            "model argument to `vllm serve`); resolved from model_source when "
            "omitted"
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
    def check_model_or_source(self) -> "VllmConfig":
        if not self.model_path and not self.model_source:
            raise ValueError("Either 'model_path' or 'model_source' must be provided")
        return self

    # ── Parallelism and memory ──────────────────────────────────
    tensor_parallel_size: int | None = Field(
        default=None, ge=1, description="Tensor parallel size (--tensor-parallel-size)"
    )
    pipeline_parallel_size: int | None = Field(
        default=None,
        ge=1,
        description="Pipeline parallel size (--pipeline-parallel-size)",
    )
    max_model_len: int | None = Field(
        default=None, ge=1, description="Maximum context length (--max-model-len)"
    )
    gpu_memory_utilization: float | None = Field(
        default=None,
        gt=0.0,
        le=1.0,
        description=(
            "Fraction of GPU memory reserved for the engine "
            "(--gpu-memory-utilization)"
        ),
    )
    max_num_seqs: int | None = Field(
        default=None,
        ge=1,
        description="Maximum concurrent sequences (--max-num-seqs)",
    )
    max_num_batched_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Maximum tokens per scheduler step (--max-num-batched-tokens)",
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
    moe_backend: str | None = Field(
        default=None,
        description=(
            "MoE kernel backend, e.g. 'cutlass' or 'flashinfer_cutlass' "
            "(--moe-backend)"
        ),
    )
    trust_remote_code: bool = Field(
        default=False,
        description="Allow the model repo's own modelling code (--trust-remote-code)",
    )
    enforce_eager: bool = Field(
        default=False,
        description="Skip CUDA graph capture (--enforce-eager)",
    )
    enable_prefix_caching: bool | None = Field(
        default=None,
        description=(
            "Enable or disable the automatic prefix cache (--enable-prefix-caching "
            "/ --no-enable-prefix-caching); None leaves the engine default"
        ),
    )
    speculative_config: str | None = Field(
        default=None,
        description=(
            "JSON object string of speculative-decoding settings, e.g. "
            '\'{"method": "mtp", "num_speculative_tokens": 5}\' '
            "(--speculative-config)"
        ),
    )
    tool_call_parser: str | None = Field(
        default=None,
        description="Tool-call parser, e.g. 'hermes' (--tool-call-parser)",
    )
    reasoning_parser: str | None = Field(
        default=None,
        description="Reasoning parser, e.g. 'deepseek_r1' (--reasoning-parser)",
    )
    enable_auto_tool_choice: bool = Field(
        default=False,
        description="Let the model choose tool calls (--enable-auto-tool-choice)",
    )

    @field_validator("speculative_config", mode="before")
    @classmethod
    def normalize_speculative_config(cls, raw: Any) -> Any:
        """Parse and re-serialize the speculative config as compact JSON.

        Accepts a JSON string (webui form value) or a dict (programmatic use).
        Storing the canonical form means solar-control's drift detection never
        sees a restart-worthy difference between ``{"a": 1}`` and ``{"a":1}``.
        vLLM json.loads() this flag, so a bare scalar or a list would fail
        inside the server rather than here; booleans are left as they are
        (unlike llama.cpp's chat_template_kwargs, nothing here coerces
        boolean-looking strings).
        """
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"speculative_config is not valid JSON: {exc}"
                ) from exc
        else:
            parsed = raw
        # Pydantic only turns ValueError into a validation error, hence not
        # TypeError.
        if isinstance(parsed, dict):
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        raise ValueError("speculative_config must be a JSON object")

    # ── Escape hatches ──────────────────────────────────────────
    extra_args: list[str] | None = Field(
        default=None,
        description=(
            "Raw CLI arguments appended after the typed flags, for vLLM options "
            "without a typed field (e.g. ['--max-num-partial-prefills', '4'])"
        ),
    )
    extra_env: dict[str, str] | None = Field(
        default=None,
        description=(
            "Extra environment variables for the vLLM process, e.g. "
            "{'VLLM_USE_FLASHINFER_MOE_FP4': '1'}"
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
            # Both spellings reach vLLM's argparse identically.
            flag = arg.split("=", 1)[0]
            if flag in RESERVED_VLLM_ARGS:
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
