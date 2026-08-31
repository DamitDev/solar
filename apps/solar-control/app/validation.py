"""Shared validation helpers (S-036, S-040).

Centralized validators used by route handlers and services so
priorities, constraints, and error formats stay consistent across
the codebase.
"""

import json
import math
import re
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

VALID_PRIORITIES: frozenset[str] = frozenset({"production", "staging", "ephemeral"})
VALID_STRATEGIES: frozenset[str] = frozenset({"rolling", "immediate"})
VALID_BACKEND_TYPES: frozenset[str] = frozenset(
    {
        "llamacpp",
        "huggingface_causal",
        "huggingface_classification",
        "huggingface_embedding",
        "huggingface_vision",
        "sglang",
    }
)
VALID_MODEL_SOURCE_SCHEMES: frozenset[str] = frozenset({"repo", "huggingface", "local"})
FORBIDDEN_BACKEND_FIELDS: frozenset[str] = frozenset(
    {
        "alias",
        "model_source",
        "host",
        "port",
        "api_key",
        # SGLang's served model directory: resolved from model_source, so a
        # client-supplied value would suppress resolution and pin the intent
        # to one host's filesystem layout.
        "model_path",
    }
)

# C3 accelerator vocabulary: the tokens hosts actually report via
# detect_gpu_type() (nvidia_cuda / apple_mps / cpu). Placement filters on
# exact equality, so aliases users actually type get normalized to the
# canonical token instead of silently matching nothing.
VALID_GPU_TYPES: frozenset[str] = frozenset({"nvidia_cuda", "apple_mps", "cpu"})
GPU_TYPE_ALIASES: dict[str, str] = {
    "nvidia": "nvidia_cuda",
    "cuda": "nvidia_cuda",
    "nvidia-cuda": "nvidia_cuda",
    "mps": "apple_mps",
    "metal": "apple_mps",
    "apple": "apple_mps",
    "apple-mps": "apple_mps",
    "none": "cpu",
}

# C3 field ownership: which backend owns which field (mirrors the host's
# config models — the duplication is intentional and pinned by a test).
# ``huggingface`` covers all huggingface_* backends; the per-type entries
# are the backend-specific extras (labels, normalize_embeddings,
# use_flash_attention).
_LLAMACPP_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "model_file",
        "mmproj",
        "mmproj_offload",
        "threads",
        "n_gpu_layers",
        "devices",
        "split_mode",
        "tensor_split",
        "main_gpu",
        "temp",
        "top_p",
        "top_k",
        "min_p",
        "ctx_size",
        "chat_template_file",
        "chat_template_kwargs",
        "reasoning",
        "reasoning_budget",
        "spec_type",
        "spec_draft_n_max",
        "cache_type_k",
        "cache_type_v",
        "rope_scaling",
        "rope_scale",
        "yarn_orig_ctx",
        "special",
        "ot",
        "model_type",
        "pooling",
    }
)
_HUGGINGFACE_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "device",
        "dtype",
        "max_length",
        "trust_remote_code",
    }
)
# Ownership is many-to-many, so ``dtype`` and ``trust_remote_code`` appear
# here as well as in the huggingface set: both backends accept them.
_SGLANG_FIELDS: frozenset[str] = frozenset(
    {
        "dtype",
        "trust_remote_code",
        "tp_size",
        "dp_size",
        "context_length",
        "mem_fraction_static",
        "chunked_prefill_size",
        "max_running_requests",
        "cuda_graph_max_bs",
        "cuda_graph_max_bs_decode",
        "swa_full_tokens_ratio",
        "quantization",
        "kv_cache_dtype",
        "moe_runner_backend",
        "speculative_algorithm",
        "enable_hierarchical_cache",
        "hicache_ratio",
        "hicache_mem_layout",
        "hicache_io_backend",
        "hicache_storage_backend",
        "hicache_storage_backend_extra_config",
        "hicache_storage_prefetch_policy",
        "extra_args",
        "extra_env",
    }
)
BACKEND_FIELD_OWNERS: dict[str, frozenset[str]] = {
    "llamacpp": _LLAMACPP_ONLY_FIELDS,
    "huggingface": _HUGGINGFACE_ONLY_FIELDS,
    "huggingface_classification": frozenset({"labels"}),
    "huggingface_embedding": frozenset({"normalize_embeddings"}),
    "huggingface_causal": frozenset({"use_flash_attention"}),
    "huggingface_vision": frozenset({"use_flash_attention"}),
    "sglang": _SGLANG_FIELDS,
}

# SGLang's kernels are CUDA-only, so an SGLang intent can only ever land on an
# NVIDIA host. The token is set for the user when the placement leaves it open.
SGLANG_GPU_TYPE: str = "nvidia_cuda"

# Flags solar-host derives itself (port allocator, host API key, alias-based
# served name). Mirrors RESERVED_SGLANG_ARGS in the host's SGlang config model.
RESERVED_SGLANG_ARGS: frozenset[str] = frozenset(
    {
        "--host",
        "--port",
        "--api-key",
        "--model-path",
        "--served-model-name",
    }
)

# Device values matching DEVICE_OPTIONS in the webui backendConfig.ts.
DEVICE_OPTIONS: frozenset[str] = frozenset({"auto", "cuda", "mps", "cpu"})
_DEVICE_TO_GPU_TYPE: dict[str, str] = {
    "cuda": "nvidia_cuda",
    "mps": "apple_mps",
}


def _gpu_token(value: str) -> str:
    """Case-fold and unify separators to the canonical underscore form."""
    return value.strip().lower().replace("-", "_")


# Alias keys are accepted in either separator form, so both the table and the
# incoming value are folded the same way. Comparing a hyphenated token against
# the underscore-bearing VALID_GPU_TYPES only ever worked because the table
# happened to list hyphenated duplicates of the canonical names.
_NORMALIZED_GPU_ALIASES: dict[str, str] = {
    _gpu_token(alias): canonical for alias, canonical in GPU_TYPE_ALIASES.items()
}
_NORMALIZED_VALID_GPU_TYPES: frozenset[str] = frozenset(
    _gpu_token(t) for t in VALID_GPU_TYPES
)


def normalize_gpu_type(value: Any) -> str | None:
    """Case-fold, unify ``-``/``_``, resolve aliases; None for unknown tokens.

    The canonical tokens are the three ``VALID_GPU_TYPES``; everything else
    is either an alias (``nvidia``, ``mps``, ``metal``, ``none``, ...) or an
    unknown token, which the caller turns into a 422.
    """
    if not isinstance(value, str):
        return None
    token = _gpu_token(value)
    if token in _NORMALIZED_VALID_GPU_TYPES:
        return token
    return _NORMALIZED_GPU_ALIASES.get(token)


def validate_priority(instance_data: dict[str, Any]) -> None:
    """Validate the priority field if present (S-036)."""
    priority = instance_data.get("priority")
    if priority is not None and priority not in VALID_PRIORITIES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid priority '{priority}'. "
                f"Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"
            ),
        )


def validate_host_supports_backend(host: Any, backend_type: Any) -> None:
    """Reject a manual instance whose backend the target host cannot run.

    Manual creation names its host explicitly and so never passes through
    placement, which is where an intent's backend/accelerator fit is checked.
    A host that predates backend advertisement reports nothing, so an empty
    ``supported_backends`` is read as "no opinion" and only the accelerator
    contract applies.
    """
    if not isinstance(backend_type, str) or backend_type not in VALID_BACKEND_TYPES:
        return

    if backend_type == "sglang" and getattr(host, "gpu_type", None) != SGLANG_GPU_TYPE:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The sglang backend requires an NVIDIA host, but "
                f"'{host.name}' reports gpu_type "
                f"'{getattr(host, 'gpu_type', None)}'"
            ),
        )

    supported = getattr(host, "supported_backends", None) or []
    if supported and backend_type not in supported:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Host '{host.name}' does not support the '{backend_type}' "
                f"backend. It reports: {', '.join(sorted(supported))}"
            ),
        )


def _validate_backend_model_selection(
    backend: dict[str, Any],
    model_source: Any,
    *,
    exempt_fields: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Validate the model file selector and the download filters.

    ``model_file`` picks a GGUF inside the pulled model directory and only
    llama.cpp consumes it; ``file_filters`` maps to HuggingFace Hub
    ``allow_patterns``, which ORAS (``repo://``) and ``local://`` cannot honour.

    ``model_file``'s wrong-backend rejection lives in the ownership table
    (``_LLAMACPP_ONLY_FIELDS``) rather than here, so one mechanism owns it and
    grandfathering applies uniformly.

    ``exempt_fields`` grandfathers values an update carried over unchanged, so
    tightening a rule cannot strand a stored intent. ``file_filters`` is
    deliberately *not* grandfathered: it is validated against ``model_source``,
    which an update can change independently of the backend, so a carried-over
    value can become newly invalid.
    """
    errors: list[dict[str, str]] = []

    model_file = backend.get("model_file")
    if (
        model_file is not None
        and "model_file" not in exempt_fields
        # A wrong-backend model_file is reported by the ownership table; the
        # shape check is llama.cpp's own contract.
        and backend.get("backend_type") == "llamacpp"
        and (not isinstance(model_file, str) or not model_file.strip())
    ):
        errors.append(
            {
                "field": "backend.model_file",
                "message": "model_file must be a non-empty string",
            }
        )

    file_filters = backend.get("file_filters")
    if file_filters is not None:
        if not isinstance(file_filters, list) or not all(
            isinstance(f, str) and f.strip() for f in file_filters
        ):
            errors.append(
                {
                    "field": "backend.file_filters",
                    "message": "file_filters must be a list of non-empty patterns",
                }
            )
        elif file_filters and not str(model_source).startswith("huggingface://"):
            errors.append(
                {
                    "field": "backend.file_filters",
                    "message": (
                        "file_filters only applies to huggingface:// model sources"
                    ),
                }
            )

    mmproj = backend.get("mmproj")
    if mmproj is not None and "mmproj" not in exempt_fields:
        model_type = backend.get("model_type")
        if model_type not in (None, "llm"):
            errors.append(
                {
                    "field": "backend.mmproj",
                    "message": (
                        f"mmproj is meaningless for model_type '{model_type}' — "
                        "a projector applies to LLM vision modes only"
                    ),
                }
            )

    return errors


def _validate_backend_speculative_decoding(
    backend: dict[str, Any],
) -> list[dict[str, str]]:
    """Validate the llama.cpp speculative decoding selection.

    ``draft-dspark`` drafts with a separate GGUF, so it is only a valid spec
    together with ``spec_draft_model``; ``draft-mtp`` reuses the served
    model's own heads and takes neither the draft model nor its confidence
    threshold. Solar Host rejects the same combinations — catching them here
    keeps a broken intent out of the store instead of surfacing as a failed
    reconciliation.
    """
    errors: list[dict[str, str]] = []

    spec_type = backend.get("spec_type")
    spec_draft_model = backend.get("spec_draft_model")
    spec_draft_conf_min = backend.get("spec_draft_conf_min")

    if spec_type is not None and backend.get("backend_type") != "llamacpp":
        errors.append(
            {
                "field": "backend.spec_type",
                "message": "spec_type is only supported for the llamacpp backend",
            }
        )
        return errors

    if spec_type is not None and spec_type not in {"draft-mtp", "draft-dspark"}:
        errors.append(
            {
                "field": "backend.spec_type",
                "message": (
                    f"'{spec_type}' is not a supported spec_type. "
                    f"Must be one of: draft-dspark, draft-mtp"
                ),
            }
        )

    if spec_type == "draft-dspark":
        if not isinstance(spec_draft_model, str) or not spec_draft_model.strip():
            errors.append(
                {
                    "field": "backend.spec_draft_model",
                    "message": (
                        "spec_type 'draft-dspark' requires spec_draft_model: a "
                        "filename, relative path or glob selecting the draft GGUF"
                    ),
                }
            )
    else:
        if spec_draft_model is not None:
            errors.append(
                {
                    "field": "backend.spec_draft_model",
                    "message": "spec_draft_model requires spec_type 'draft-dspark'",
                }
            )
        if spec_draft_conf_min is not None:
            errors.append(
                {
                    "field": "backend.spec_draft_conf_min",
                    "message": "spec_draft_conf_min requires spec_type 'draft-dspark'",
                }
            )

    if spec_draft_conf_min is not None and (
        not isinstance(spec_draft_conf_min, int | float)
        or isinstance(spec_draft_conf_min, bool)
        or not 0.0 <= float(spec_draft_conf_min) <= 1.0
    ):
        errors.append(
            {
                "field": "backend.spec_draft_conf_min",
                "message": "spec_draft_conf_min must be a number between 0 and 1",
            }
        )
    return errors


def _backend_field_owners(key: str) -> frozenset[str]:
    """The backend types that accept *key*; empty when control does not know it.

    Ownership is many-to-many: ``use_flash_attention`` belongs to both
    ``huggingface_causal`` and ``huggingface_vision``, and the ``huggingface``
    entry stands for every ``huggingface_*`` type.
    """
    owners: set[str] = set()
    for owner, fields in BACKEND_FIELD_OWNERS.items():
        if key not in fields:
            continue
        if owner == "huggingface":
            owners.update(t for t in VALID_BACKEND_TYPES if t.startswith("huggingface"))
        else:
            owners.add(owner)
    return frozenset(owners)


def _describe_owners(owners: frozenset[str]) -> str:
    """Human-readable owner list, collapsing the full huggingface_* set."""
    hf_types = {t for t in VALID_BACKEND_TYPES if t.startswith("huggingface")}
    if owners == hf_types:
        return "huggingface_* backends"
    names = sorted(owners)
    if owners > hf_types:
        # A field shared with another backend (dtype, trust_remote_code) would
        # otherwise spell out all four huggingface names before it.
        names = ["huggingface_*"] + sorted(owners - hf_types)
    if len(names) == 1:
        return f"the {names[0]} backend"
    return "the " + " or ".join(names) + " backends"


def _validate_backend_field_ownership(
    backend: dict[str, Any],
    *,
    exempt_fields: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Reject fields used with the wrong backend type (C3).

    The host silently drops unknown fields (Pydantic ``extra='ignore'``) —
    exactly the class of bug the reported symptom belongs to: a ``device``
    on a llamacpp intent vanished without a trace. This table is the
    control-side mirror of the host's config models; a test pins it against
    the documented field lists so a host-side field addition fails loudly.

    ``exempt_fields`` carries fields an update left untouched, so tightening
    this table can never strand an already-stored intent (see
    :func:`validate_intent_update`).
    """
    errors: list[dict[str, str]] = []
    backend_type = backend.get("backend_type")
    if not isinstance(backend_type, str) or backend_type not in VALID_BACKEND_TYPES:
        return errors

    for key, value in backend.items():
        if key in ("backend_type", "file_filters") or key in FORBIDDEN_BACKEND_FIELDS:
            continue
        # _validate_device owns "device" and names the llamacpp alternative;
        # reporting it here too would show the user two errors for one field.
        if key == "device":
            continue
        if key in exempt_fields:
            continue
        # An explicit null configures nothing — the host's Pydantic default is
        # identical to omitting the key. _validate_device already skips None,
        # so rejecting here would make the two inconsistent.
        if value is None:
            continue
        owners = _backend_field_owners(key)
        # No owner means control does not know the field. The host ignores
        # unknown fields, so flagging them would break forward compatibility.
        if not owners or backend_type in owners:
            continue
        errors.append(
            {
                "field": f"backend.{key}",
                "message": (
                    f"{key} is only supported for {_describe_owners(owners)} "
                    f"(this intent uses {backend_type})"
                ),
            }
        )

    return errors


def _validate_device(
    backend: dict[str, Any],
    placement: dict[str, Any],
    *,
    exempt_fields: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Validate the HuggingFace-only ``device`` field against the placement (C3).

    ``device`` is a contract for HuggingFace backends only: llama.cpp has no
    such field (its device selection is ``devices``/``n_gpu_layers``/``ot``),
    so a ``device`` on a llamacpp intent used to be silently dropped. For HF
    backends the value must be one of ``auto/cuda/mps/cpu`` and must not
    contradict an explicitly chosen ``placement.gpu_type`` — the reported
    ``mps`` plus NVIDIA-host symptom is fully static and a hard 422.

    ``exempt_fields`` grandfathers the *ownership* rejection only. The value
    checks below still run: an update that leaves ``device`` alone but points
    ``placement.gpu_type`` at a different accelerator is a new contradiction,
    not a stored one.
    """
    errors: list[dict[str, str]] = []
    device = backend.get("device")
    if device is None:
        return errors

    if backend.get("backend_type") == "llamacpp":
        if "device" in exempt_fields:
            return errors
        errors.append(
            {
                "field": "backend.device",
                "message": (
                    "device is only supported for huggingface_* backends; "
                    "llama.cpp device selection is devices/n_gpu_layers/ot"
                ),
            }
        )
        return errors

    if device not in DEVICE_OPTIONS:
        errors.append(
            {
                "field": "backend.device",
                "message": (
                    f"'{device}' is not a valid device. Must be one of: "
                    f"{', '.join(sorted(DEVICE_OPTIONS))}"
                ),
            }
        )
        return errors

    required = _DEVICE_TO_GPU_TYPE.get(device)
    gpu_type = placement.get("gpu_type") if isinstance(placement, dict) else None
    if required is not None and gpu_type is not None and gpu_type != required:
        errors.append(
            {
                "field": "backend.device",
                "message": (
                    f"device '{device}' requires gpu_type '{required}', but "
                    f"placement.gpu_type is '{gpu_type}'"
                ),
            }
        )
    return errors


def _validate_sglang(
    backend: dict[str, Any],
    data: dict[str, Any],
) -> list[dict[str, str]]:
    """Validate the SGLang contract and pin the placement to NVIDIA.

    SGLang only runs on CUDA, so an unset ``placement.gpu_type`` is filled in
    on *data* rather than left to chance (placement filters on exact equality,
    and an unset token would let the reconciler pick a CPU or Apple host and
    fail at start). A gpu_type naming a different accelerator is a hard 422.

    ``extra_args``/``extra_env`` are the version-specific escape hatches; they
    are shape-checked here, and ``extra_args`` may not carry a flag solar-host
    derives itself — a second ``--port`` would silently break gateway routing.
    """
    errors: list[dict[str, str]] = []
    if backend.get("backend_type") != "sglang":
        return errors

    placement = data.get("placement")
    if placement is None:
        placement = {}
        data["placement"] = placement
    if isinstance(placement, dict):
        gpu_type = placement.get("gpu_type")
        if gpu_type is None:
            placement["gpu_type"] = SGLANG_GPU_TYPE
        elif gpu_type != SGLANG_GPU_TYPE:
            errors.append(
                {
                    "field": "placement.gpu_type",
                    "message": (
                        f"the sglang backend requires gpu_type "
                        f"'{SGLANG_GPU_TYPE}', but placement.gpu_type is "
                        f"'{gpu_type}'"
                    ),
                }
            )

    extra_args = backend.get("extra_args")
    if extra_args is not None:
        if not isinstance(extra_args, list) or not all(
            isinstance(a, str) and a.strip() for a in extra_args
        ):
            errors.append(
                {
                    "field": "backend.extra_args",
                    "message": "extra_args must be a list of non-empty strings",
                }
            )
        else:
            for arg in extra_args:
                flag = arg.strip().split("=", 1)[0]
                if flag in RESERVED_SGLANG_ARGS:
                    errors.append(
                        {
                            "field": "backend.extra_args",
                            "message": (
                                f"'{flag}' is managed by solar-host and must not "
                                f"be set through extra_args"
                            ),
                        }
                    )

    extra_env = backend.get("extra_env")
    if extra_env is not None and (
        not isinstance(extra_env, dict)
        or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str)
            for k, v in extra_env.items()
        )
    ):
        errors.append(
            {
                "field": "backend.extra_env",
                "message": "extra_env must be an object mapping names to strings",
            }
        )

    return errors


def derive_gpu_count(data: dict[str, Any]) -> int | None:
    """Derive the implied GPU count from the backend (S-058 spec §5).

    sglang → ``tp_size``; llama.cpp → the ``devices`` list length, or the
    ``tensor_split`` count when ``devices`` is absent (the host's
    ``check_tensor_split`` validator already enforces they agree). Returns
    None when the backend carries no count signal.
    """
    backend = data.get("backend")
    if not isinstance(backend, dict):
        return None
    backend_type = backend.get("backend_type")

    if backend_type == "sglang":
        tp_size = backend.get("tp_size")
        if isinstance(tp_size, int) and not isinstance(tp_size, bool) and tp_size >= 1:
            return tp_size
        return None

    if backend_type == "llamacpp":
        devices = backend.get("devices")
        if isinstance(devices, str) and devices.strip():
            parts = [p.strip() for p in devices.split(",") if p.strip()]
            if parts:
                return len(parts)
        tensor_split = backend.get("tensor_split")
        if isinstance(tensor_split, str) and tensor_split.strip():
            parts = [p.strip() for p in tensor_split.split(",") if p.strip()]
            if parts:
                return len(parts)
    return None


def _validate_gpu_count(data: dict[str, Any], errors: list[dict[str, str]]) -> None:
    """Validate and resolve ``resources.gpu_count`` (S-058 spec §5).

    - Derive from the backend when not explicit (sglang ``tp_size``;
      llama.cpp ``devices`` list length / ``tensor_split`` count).
    - Explicit and derived disagreement → 422.
    - Explicit ``> 1`` with a ``huggingface_*`` backend → 422 (HF stays
      single-GPU).
    - Resolve in place (``data[\"resources\"][\"gpu_count\"]``) so the route
      persists the concrete count and every consumer sees an ``int >= 1``.
    """
    resources = data.get("resources")
    if not isinstance(resources, dict):
        return

    explicit = resources.get("gpu_count")
    if explicit is not None and (
        not isinstance(explicit, int) or isinstance(explicit, bool) or explicit < 1
    ):
        errors.append(
            {
                "field": "resources.gpu_count",
                "message": "gpu_count must be an integer >= 1",
            }
        )
        return

    derived = derive_gpu_count(data)
    backend_type = (data.get("backend") or {}).get("backend_type")

    if explicit is not None and derived is not None and explicit != derived:
        if backend_type == "sglang":
            hint = "backend.tp_size"
        elif backend_type == "llamacpp":
            hint = "backend.devices"
        else:
            hint = "backend"
        errors.append(
            {
                "field": "resources.gpu_count",
                "message": (
                    f"resources.gpu_count is {explicit} but {hint} implies "
                    f"{derived} — they must agree"
                ),
            }
        )
        return

    resolved = explicit if explicit is not None else (derived or 1)
    resources["gpu_count"] = resolved

    if (
        isinstance(backend_type, str)
        and backend_type.startswith("huggingface")
        and resolved > 1
    ):
        errors.append(
            {
                "field": "resources.gpu_count",
                "message": (
                    "HuggingFace backends stay single-GPU — gpu_count must be 1"
                ),
            }
        )


# Device-name suffix of a llama.cpp ``devices`` entry ('CUDA0' → suffix 0).
# Entries without a numeric suffix ('none', and anything non-CUDA) do not
# match and stay legal — they never index a visible device.
_DEVICE_NAME_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _gpu_device_position_issues(
    backend: dict[str, Any],
    gpu_count: int,
    *,
    first_only: bool = False,
    exempt_fields: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Out-of-range llama.cpp device flags as issue dicts (S-058).

    After ``CUDA_VISIBLE_DEVICES`` the child process only ever sees
    ``CUDA0..CUDA(n-1)`` (spec §8 F1/D5): ``devices`` entries and
    ``main_gpu`` are *positions within the chosen set*, never physical
    indices, so the range is ``0..gpu_count-1``.

    Shared by the hard-error path (:func:`_validate_gpu_device_positions`)
    and the advisory warning in :func:`validate_intent_warnings` so the
    message wording cannot drift between the two. *first_only* limits the
    ``devices`` loop to the first offending entry (the advisory path
    reports one; ``main_gpu`` is still checked afterwards); *exempt_fields*
    grandfathers backend fields an update carries over unchanged.
    """
    issues: list[dict[str, str]] = []
    upper = gpu_count - 1

    devices = backend.get("devices")
    if isinstance(devices, str) and devices.strip() and "devices" not in exempt_fields:
        for entry in (p.strip() for p in devices.split(",") if p.strip()):
            m = _DEVICE_NAME_RE.match(entry)
            if m is None:
                continue  # 'none' and other non-CUDA tokens stay legal
            if int(m.group(2)) > upper:
                issues.append(
                    {
                        "field": "backend.devices",
                        "message": (
                            f"devices entry '{entry}' is out of range — "
                            f"device flags are positions within the chosen set "
                            f"(0..{upper}); physical ids are not selectable"
                        ),
                    }
                )
                if first_only:
                    break

    main_gpu = backend.get("main_gpu")
    if (
        isinstance(main_gpu, int)
        and not isinstance(main_gpu, bool)
        and main_gpu > upper
        and "main_gpu" not in exempt_fields
    ):
        issues.append(
            {
                "field": "backend.main_gpu",
                "message": (
                    f"main_gpu {main_gpu} is out of range — device flags are "
                    f"positions within the chosen set (0..{upper}); physical "
                    f"ids are not selectable"
                ),
            }
        )
    return issues


def _validate_gpu_device_positions(
    data: dict[str, Any],
    errors: list[dict[str, str]],
    *,
    exempt_fields: frozenset[str],
) -> None:
    """Reject llama.cpp device flags outside the scheduler-chosen set (S-058).

    After ``CUDA_VISIBLE_DEVICES`` the child process only ever sees
    ``CUDA0..CUDA(n-1)`` (spec §8 F1/D5): ``devices`` entries and
    ``main_gpu`` are *positions within the chosen set*, never physical
    indices. A stored ``devices: \"CUDA1,CUDA2\"`` with ``gpu_count=2``
    derives the right count, gets two cards, then passes ``--device
    CUDA1,CUDA2`` into a process where ``CUDA2`` does not exist.

    Runs after :func:`_validate_gpu_count` (which resolves
    ``resources.gpu_count`` in place) and only when that call produced no
    ``resources.gpu_count`` error — an unresolved count makes the range
    unknown. Backend fields an update carries over unchanged are
    grandfathered via ``exempt_fields`` (the create-time rule already
    blocked them; see :func:`_unchanged_backend_fields`), leaving the
    advisory warning in :func:`validate_intent_warnings` as the signal for
    an already-stored broken value.
    """
    backend = data.get("backend")
    if not isinstance(backend, dict) or backend.get("backend_type") != "llamacpp":
        return
    # _validate_gpu_count already failed → its message is the answer.
    if any(e.get("field") == "resources.gpu_count" for e in errors):
        return
    resources = data.get("resources")
    if not isinstance(resources, dict):
        return
    gpu_count = resources.get("gpu_count")
    if not isinstance(gpu_count, int) or isinstance(gpu_count, bool) or gpu_count < 1:
        return
    errors.extend(
        _gpu_device_position_issues(backend, gpu_count, exempt_fields=exempt_fields)
    )


def validate_intent_warnings(data: dict[str, Any]) -> list[dict[str, str]]:
    """Static (non-fleet) advisory warnings for an intent payload (C3).

    Warnings never block an edit; they ride along on the success response
    (``IntentResponse.warnings``). Dynamic fleet-derived warnings live in
    ``app.services.intent_validation.validate_intent_fleet``.
    """
    warnings: list[dict[str, str]] = []
    backend = data.get("backend", {})

    if isinstance(backend, dict) and backend.get("backend_type") == "llamacpp":
        if (
            backend.get("pooling") is not None
            and backend.get("model_type") != "embedding"
        ):
            warnings.append(
                {
                    "field": "backend.pooling",
                    "message": (
                        "pooling is only meaningful with model_type "
                        "'embedding' (llama.cpp tolerates it, but it will "
                        "have no effect)"
                    ),
                }
            )

        split_mode = backend.get("split_mode")
        if backend.get("tensor_split") is not None and split_mode == "none":
            warnings.append(
                {
                    "field": "backend.tensor_split",
                    "message": (
                        "tensor_split has no effect with split_mode 'none' — "
                        "that mode puts the whole model on main_gpu"
                    ),
                }
            )
        if backend.get("main_gpu") is not None and split_mode in ("layer", "tensor"):
            warnings.append(
                {
                    "field": "backend.main_gpu",
                    "message": (
                        f"main_gpu has no effect with split_mode '{split_mode}' — "
                        "it only applies to split_mode 'none' or 'row'"
                    ),
                }
            )

    # S-058 advisory: a multi-GPU intent without a per-GPU footprint cannot
    # be bin-packed by per-device placement.
    resources = data.get("resources") or {}
    if isinstance(resources, dict):
        gpu_count = resources.get("gpu_count")
        if gpu_count is None:
            gpu_count = derive_gpu_count(data)
        vram_gb = resources.get("vram_gb")
        if (gpu_count or 1) > 1 and (
            vram_gb is None or not isinstance(vram_gb, (int, float)) or vram_gb <= 0
        ):
            warnings.append(
                {
                    "field": "resources.vram_gb",
                    "message": (
                        f"gpu_count is {gpu_count} but no per-GPU vram_gb is set — "
                        "placement cannot bin-pack a device set without a "
                        "per-device footprint"
                    ),
                }
            )

    # S-058 advisory: llama.cpp device flags are positions within the
    # chosen set. Validation only sees what it is asked to validate, so an
    # already-stored out-of-range value is never blocked (the update path
    # grandfathers untouched fields) — this warning is the only signal such
    # an intent gets. Mirrors the hard rule's scope: no resources object,
    # no resolved count, no check (the create-time validator behaves the
    # same way on a payload without resources). Implementation shared with
    # the hard-error path via _gpu_device_position_issues (first issue
    # only, no exempt fields — warnings never block an edit).
    if (
        isinstance(backend, dict)
        and backend.get("backend_type") == "llamacpp"
        and isinstance(data.get("resources"), dict)
    ):
        gpu_count = resources.get("gpu_count")
        if not isinstance(gpu_count, int) or isinstance(gpu_count, bool):
            gpu_count = derive_gpu_count(data) or 1
        warnings.extend(
            _gpu_device_position_issues(backend, gpu_count, first_only=True)
        )
    return warnings


def _unchanged_backend_fields(backend: Any, current_backend: Any) -> frozenset[str]:
    """Backend keys an update carries over from the stored spec unchanged.

    Field ownership is a static table that can be tightened at any time, and
    an intent stored before a tightening would otherwise become permanently
    uneditable: every update replays the full spec, so it would fail on a
    field the user is not even touching. Exempting untouched fields keeps
    such intents editable while still rejecting a *newly* misplaced field.
    """
    if not isinstance(backend, dict) or not isinstance(current_backend, dict):
        return frozenset()
    # A backend_type change re-homes every field, so nothing is grandfathered.
    if backend.get("backend_type") != current_backend.get("backend_type"):
        return frozenset()
    return frozenset(
        key
        for key, value in backend.items()
        if key in current_backend and current_backend[key] == value
    )


def validate_intent_update(
    data: dict[str, Any],
    *,
    current_alias: str,
    current_backend: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Validate an intent update request (S-039 §12.5).

    Applies every creation rule — an update must not be able to write a
    spec that submission would reject — plus alias immutability. Backend
    fields carried over unchanged from ``current_backend`` are exempt from
    the field-ownership table; see :func:`_unchanged_backend_fields`.
    """
    errors = validate_intent_create(
        data,
        ownership_exempt_fields=_unchanged_backend_fields(
            data.get("backend"), current_backend
        ),
    )

    alias = data.get("alias")
    if isinstance(alias, str) and alias.strip() and alias != current_alias:
        errors.append(
            {
                "field": "alias",
                "message": (
                    f"alias is immutable (currently '{current_alias}'). It is the "
                    f"served name and the deployment's identity — create a new "
                    f"intent to serve a different alias"
                ),
            }
        )

    return errors


def validate_intent_create(
    data: dict[str, Any],
    *,
    ownership_exempt_fields: frozenset[str] = frozenset(),
) -> list[dict[str, str]]:
    """Validate an intent creation request (S-039 §4.7).

    Returns a list of {field, message} errors. Empty list means valid.
    Does NOT raise — the route handler decides the HTTP status.

    Shared with the update path (:func:`validate_intent_update`) so the two
    cannot drift apart; ``ownership_exempt_fields`` is that path's
    grandfathering hook and is always empty for a genuine creation.
    """
    errors: list[dict[str, str]] = []

    # alias
    alias = data.get("alias")
    if not alias or not isinstance(alias, str) or not alias.strip():
        errors.append(
            {"field": "alias", "message": "alias is required and must be non-empty"}
        )

    # model_source
    model_source = data.get("model_source", "")
    if not model_source:
        errors.append({"field": "model_source", "message": "model_source is required"})
    else:
        parsed = urlparse(model_source)
        if parsed.scheme not in VALID_MODEL_SOURCE_SCHEMES:
            errors.append(
                {
                    "field": "model_source",
                    "message": (
                        f"unsupported scheme '{parsed.scheme}'. "
                        f"Must be one of: {', '.join(sorted(VALID_MODEL_SOURCE_SCHEMES))}"
                    ),
                }
            )

    # replicas
    replicas = data.get("replicas", 1)
    if not isinstance(replicas, int) or replicas < 0:
        errors.append({"field": "replicas", "message": "replicas must be >= 0"})

    # priority
    priority = data.get("priority", "production")
    if priority not in VALID_PRIORITIES:
        errors.append(
            {
                "field": "priority",
                "message": (
                    f"'{priority}' is not a valid priority. "
                    f"Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"
                ),
            }
        )

    # strategy
    strategy = data.get("strategy", "rolling")
    if strategy not in VALID_STRATEGIES:
        errors.append(
            {
                "field": "strategy",
                "message": (
                    f"'{strategy}' is not a valid strategy. "
                    f"Must be one of: {', '.join(sorted(VALID_STRATEGIES))}"
                ),
            }
        )

    # placement (first, so _validate_device below sees the canonicalized
    # gpu_type)
    placement = data.get("placement", {})
    if isinstance(placement, dict):
        roles = placement.get("roles", ["inference"])
        if not isinstance(roles, list) or len(roles) == 0:
            errors.append(
                {
                    "field": "placement.roles",
                    "message": "placement.roles must be a non-empty list",
                }
            )

        # C3: gpu_type is a closed vocabulary. Unknown tokens are a hard
        # 422; aliases are canonicalized in place so the stored intent
        # matches hosts (placement filters on exact equality).
        gpu_type = placement.get("gpu_type")
        if gpu_type is not None:
            normalized = normalize_gpu_type(gpu_type)
            if normalized is None:
                errors.append(
                    {
                        "field": "placement.gpu_type",
                        "message": (
                            f"'{gpu_type}' is not a valid gpu_type. "
                            f"Must be one of: {', '.join(sorted(VALID_GPU_TYPES))}"
                        ),
                    }
                )
            else:
                placement["gpu_type"] = normalized

    # backend
    backend = data.get("backend", {})
    if not isinstance(backend, dict):
        errors.append({"field": "backend", "message": "backend must be an object"})
    else:
        backend_type = backend.get("backend_type")
        if not backend_type:
            errors.append(
                {"field": "backend.backend_type", "message": "backend_type is required"}
            )
        elif backend_type not in VALID_BACKEND_TYPES:
            errors.append(
                {
                    "field": "backend.backend_type",
                    "message": (
                        f"'{backend_type}' is not a supported backend_type. "
                        f"Must be one of: {', '.join(sorted(VALID_BACKEND_TYPES))}"
                    ),
                }
            )

        # Forbidden fields
        for forbidden in FORBIDDEN_BACKEND_FIELDS:
            if forbidden in backend:
                errors.append(
                    {
                        "field": f"backend.{forbidden}",
                        "message": f"'{forbidden}' is server-derived and must not be set by the client",
                    }
                )

        # C3: field ownership (a field used with the wrong backend type used
        # to be silently dropped by the host) and the device contract.
        errors.extend(
            _validate_backend_field_ownership(
                backend, exempt_fields=ownership_exempt_fields
            )
        )
        errors.extend(
            _validate_device(backend, placement, exempt_fields=ownership_exempt_fields)
        )
        errors.extend(
            _validate_backend_model_selection(
                backend, model_source, exempt_fields=ownership_exempt_fields
            )
        )
        errors.extend(_validate_backend_speculative_decoding(backend))
        errors.extend(_validate_sglang(backend, data))

    # resources.gpu_count (S-058): derive from the backend, reject
    # disagreement, and resolve the concrete count in place.
    _validate_gpu_count(data, errors)
    # S-058: llama.cpp device flags are positions within the chosen set —
    # out-of-range entries would name devices that do not exist in the
    # CUDA_VISIBLE_DEVICES-restricted process.
    _validate_gpu_device_positions(data, errors, exempt_fields=ownership_exempt_fields)

    return errors


def _coerce_jsonish(value: Any) -> Any:
    """Recursively turn boolean-looking strings into real booleans.

    Mirrors ``solar_host.models.llamacpp._coerce_template_kwargs`` (the
    duplication is intentional — control cannot import ``solar_host`` — and
    is pinned by a test asserting both behave identically). The host rewrites
    ``chat_template_kwargs`` at the config boundary, so canonicalizing at the
    API boundary stores the same form the host produces.
    """
    if isinstance(value, dict):
        return {k: _coerce_jsonish(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_jsonish(v) for v in value]
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return value


def _invalid_backend_field(field: str, message: str) -> HTTPException:
    """422 in the intent error envelope for a single backend field."""
    return HTTPException(
        status_code=422,
        detail={
            "detail": "Invalid intent",
            "errors": [{"field": f"backend.{field}", "message": message}],
        },
    )


def _normalize_csv(value: Any) -> Any:
    """Collapse a comma-separated list to its canonical ``a,b,c`` form.

    Mirrors ``solar_host.models.llamacpp._normalize_csv`` (the duplication is
    intentional — control cannot import ``solar_host`` — and is pinned by a
    test). Without it a stored ``"0, 1"`` would never match the host's
    ``"0,1"`` and drift detection would restart the instance on every pass.
    """
    if not isinstance(value, str):
        return value
    parts = [part.strip() for part in value.split(",")]
    return ",".join(part for part in parts if part) or None


def _canonicalize_device_lists(backend: dict[str, Any]) -> None:
    """Normalize the comma-separated multi-GPU lists in place."""
    for field in ("devices", "tensor_split"):
        if field in backend:
            backend[field] = _normalize_csv(backend[field])

    tensor_split = backend.get("tensor_split")
    if not isinstance(tensor_split, str):
        return
    # llama.cpp parses the list with strtod and silently reads an unparseable
    # entry as 0.0, so the whole model lands on one GPU instead of failing.
    for part in tensor_split.split(","):
        try:
            proportion = float(part)
        except ValueError as exc:
            raise _invalid_backend_field(
                "tensor_split",
                f"tensor_split must be comma-separated numbers, got '{part}'",
            ) from exc
        if not math.isfinite(proportion):
            raise _invalid_backend_field(
                "tensor_split",
                f"tensor_split must be comma-separated numbers, got '{part}'",
            )
        if proportion < 0:
            raise _invalid_backend_field(
                "tensor_split", "tensor_split proportions must not be negative"
            )


def _canonicalize_json_object_field(
    backend: dict[str, Any], field: str, *, coerce_booleans: bool
) -> None:
    """Store *field* as the compact canonical JSON the host produces, in place.

    Accepts a JSON object string (as the webui form sends) or a dict.
    ``coerce_booleans`` mirrors whether the owning host config model rewrites
    boolean-looking strings — it must match exactly, or the stored value and
    the instance config would differ and drift detection would restart the
    instance on every pass.

    Raises HTTPException(422) on malformed JSON or a non-object value.
    """
    value = backend.get(field)
    if value is None:
        return

    def _invalid(message: str) -> HTTPException:
        return _invalid_backend_field(field, message)

    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        if not value.strip():
            return
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError) as exc:
            raise _invalid(f"{field} is not valid JSON: {exc}") from exc
    else:
        raise _invalid(f"{field} must be a JSON object string or a dict")

    if not isinstance(parsed, dict):
        raise _invalid(f"{field} must be a JSON object")

    backend[field] = json.dumps(
        _coerce_jsonish(parsed) if coerce_booleans else parsed,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def canonicalize_intent_backend(backend: dict[str, Any]) -> None:
    """Normalize the backend fields the host rewrites on parse, in place (C1).

    ``chat_template_kwargs`` is parsed (accepting a JSON string, as the webui
    form sends, or a dict), recursively coerced so boolean-looking strings
    become real booleans, and re-serialized as compact canonical JSON — the
    exact form the host's ``LlamaCppConfig.normalize_chat_template_kwargs``
    produces. SGLang's ``hicache_storage_backend_extra_config`` gets the same
    treatment minus the boolean coercion (its host model does not coerce), and
    ``devices``/``tensor_split`` the comma-separated equivalent. Storing the
    canonical form means new intents never carry a representation that drift
    detection would flag; the normalization-aware comparison in the reconciler
    remains for already-stored intents.

    Raises HTTPException(422) on malformed JSON or a non-object value.
    """
    _canonicalize_device_lists(backend)
    _canonicalize_json_object_field(
        backend, "chat_template_kwargs", coerce_booleans=True
    )
    _canonicalize_json_object_field(
        backend, "hicache_storage_backend_extra_config", coerce_booleans=False
    )
