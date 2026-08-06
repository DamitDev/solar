"""Shared validation helpers (S-036, S-040).

Centralized validators used by route handlers and services so
priorities, constraints, and error formats stay consistent across
the codebase.
"""

import json
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
BACKEND_FIELD_OWNERS: dict[str, frozenset[str]] = {
    "llamacpp": _LLAMACPP_ONLY_FIELDS,
    "huggingface": _HUGGINGFACE_ONLY_FIELDS,
    "huggingface_classification": frozenset({"labels"}),
    "huggingface_embedding": frozenset({"normalize_embeddings"}),
    "huggingface_causal": frozenset({"use_flash_attention"}),
    "huggingface_vision": frozenset({"use_flash_attention"}),
}

# Device values matching DEVICE_OPTIONS in the webui backendConfig.ts.
DEVICE_OPTIONS: frozenset[str] = frozenset({"auto", "cuda", "mps", "cpu"})
_DEVICE_TO_GPU_TYPE: dict[str, str] = {
    "cuda": "nvidia_cuda",
    "mps": "apple_mps",
}


def normalize_gpu_type(value: Any) -> str | None:
    """Case-fold, unify ``-``/``_``, resolve aliases; None for unknown tokens.

    The canonical tokens are the three ``VALID_GPU_TYPES``; everything else
    is either an alias (``nvidia``, ``mps``, ``metal``, ``none``, ...) or an
    unknown token, which the caller turns into a 422.
    """
    if not isinstance(value, str):
        return None
    token = value.strip().lower().replace("_", "-")
    if token in VALID_GPU_TYPES:
        return token
    return GPU_TYPE_ALIASES.get(token)


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


def _validate_backend_model_selection(
    backend: dict[str, Any], model_source: Any
) -> list[dict[str, str]]:
    """Validate the model file selector and the download filters.

    ``model_file`` picks a GGUF inside the pulled model directory and only
    llama.cpp consumes it; ``file_filters`` maps to HuggingFace Hub
    ``allow_patterns``, which ORAS (``repo://``) and ``local://`` cannot honour.
    """
    errors: list[dict[str, str]] = []

    model_file = backend.get("model_file")
    if model_file is not None:
        if backend.get("backend_type") != "llamacpp":
            errors.append(
                {
                    "field": "backend.model_file",
                    "message": "model_file is only supported for the llamacpp backend",
                }
            )
        elif not isinstance(model_file, str) or not model_file.strip():
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
    if mmproj is not None:
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


def _validate_backend_field_ownership(
    backend: dict[str, Any],
) -> list[dict[str, str]]:
    """Reject fields used with the wrong backend type (C3).

    The host silently drops unknown fields (Pydantic ``extra='ignore'``) —
    exactly the class of bug the reported symptom belongs to: a ``device``
    on a llamacpp intent vanished without a trace. This table is the
    control-side mirror of the host's config models; a test pins it against
    the documented field lists so a host-side field addition fails loudly.
    """
    errors: list[dict[str, str]] = []
    backend_type = backend.get("backend_type")
    if not isinstance(backend_type, str) or backend_type not in VALID_BACKEND_TYPES:
        return errors

    llamacpp_fields = BACKEND_FIELD_OWNERS["llamacpp"]
    hf_common = BACKEND_FIELD_OWNERS["huggingface"]

    for key in backend:
        if key in ("backend_type", "file_filters") or key in FORBIDDEN_BACKEND_FIELDS:
            continue
        if backend_type == "llamacpp":
            if key in hf_common:
                errors.append(
                    {
                        "field": f"backend.{key}",
                        "message": (
                            f"{key} is only supported for huggingface_* backends "
                            f"(this intent uses llamacpp)"
                        ),
                    }
                )
                continue
            for hf_type, fields in BACKEND_FIELD_OWNERS.items():
                if (
                    hf_type.startswith("huggingface")
                    and hf_type != "huggingface"
                    and key in fields
                ):
                    errors.append(
                        {
                            "field": f"backend.{key}",
                            "message": (
                                f"{key} is only supported for the {hf_type} "
                                f"backend (this intent uses llamacpp)"
                            ),
                        }
                    )
        elif backend_type.startswith("huggingface"):
            if key in llamacpp_fields:
                errors.append(
                    {
                        "field": f"backend.{key}",
                        "message": (
                            f"{key} is only supported for the llamacpp backend "
                            f"(this intent uses {backend_type})"
                        ),
                    }
                )
                continue
            if key not in hf_common:
                owner = next(
                    (
                        t
                        for t, fields in BACKEND_FIELD_OWNERS.items()
                        if t.startswith("huggingface")
                        and t != "huggingface"
                        and key in fields
                    ),
                    None,
                )
                if owner is not None and owner != backend_type:
                    errors.append(
                        {
                            "field": f"backend.{key}",
                            "message": (
                                f"{key} is only supported for the {owner} backend "
                                f"(this intent uses {backend_type})"
                            ),
                        }
                    )
                # Unknown keys are ignored by the host; flagging them would
                # break forward compatibility, so they pass.

    return errors


def _validate_device(
    backend: dict[str, Any], placement: dict[str, Any]
) -> list[dict[str, str]]:
    """Validate the HuggingFace-only ``device`` field against the placement (C3).

    ``device`` is a contract for HuggingFace backends only: llama.cpp has no
    such field (its device selection is ``n_gpu_layers``/``ot``), so a
    ``device`` on a llamacpp intent used to be silently dropped. For HF
    backends the value must be one of ``auto/cuda/mps/cpu`` and must not
    contradict an explicitly chosen ``placement.gpu_type`` — the reported
    ``mps`` plus NVIDIA-host symptom is fully static and a hard 422.
    """
    errors: list[dict[str, str]] = []
    device = backend.get("device")
    if device is None:
        return errors

    if backend.get("backend_type") == "llamacpp":
        errors.append(
            {
                "field": "backend.device",
                "message": (
                    "device is only supported for huggingface_* backends; "
                    "llama.cpp device selection is n_gpu_layers/ot"
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


def validate_intent_warnings(data: dict[str, Any]) -> list[dict[str, str]]:
    """Static (non-fleet) advisory warnings for an intent payload (C3).

    Warnings never block an edit; they ride along on the success response
    (``IntentResponse.warnings``). Dynamic fleet-derived warnings live in
    ``app.services.intent_validation.validate_intent_fleet``.
    """
    warnings: list[dict[str, str]] = []
    backend = data.get("backend", {})
    if (
        isinstance(backend, dict)
        and backend.get("backend_type") == "llamacpp"
        and backend.get("pooling") is not None
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
    return warnings


def validate_intent_update(
    data: dict[str, Any], *, current_alias: str
) -> list[dict[str, str]]:
    """Validate an intent update request (S-039 §12.5).

    Applies every creation rule — an update must not be able to write a
    spec that submission would reject — plus alias immutability.
    """
    errors = validate_intent_create(data)

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


def validate_intent_create(data: dict[str, Any]) -> list[dict[str, str]]:
    """Validate an intent creation request (S-039 §4.7).

    Returns a list of {field, message} errors. Empty list means valid.
    Does NOT raise — the route handler decides the HTTP status.

    Shared with the update path (:func:`validate_intent_update`) so the two
    cannot drift apart.
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
        errors.extend(_validate_backend_field_ownership(backend))
        errors.extend(_validate_device(backend, placement))
        errors.extend(_validate_backend_model_selection(backend, model_source))
        errors.extend(_validate_backend_speculative_decoding(backend))

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


def canonicalize_intent_backend(backend: dict[str, Any]) -> None:
    """Normalize ``backend.chat_template_kwargs`` in place (C1).

    Parses the kwargs (accepting a JSON string, as the webui form sends, or
    a dict), recursively coerces boolean-looking strings to real booleans,
    and re-serializes as compact canonical JSON — the exact form the host's
    ``LlamaCppConfig.normalize_chat_template_kwargs`` produces. Storing the
    canonical form means new intents never carry a representation that
    drift detection would flag; the normalization-aware comparison in the
    reconciler remains for already-stored intents.

    Raises HTTPException(422) on malformed JSON or a non-object value.
    """
    kwargs = backend.get("chat_template_kwargs")
    if kwargs is None:
        return

    def _invalid(message: str) -> HTTPException:
        return HTTPException(
            status_code=422,
            detail={
                "detail": "Invalid intent",
                "errors": [
                    {
                        "field": "backend.chat_template_kwargs",
                        "message": message,
                    }
                ],
            },
        )

    if isinstance(kwargs, dict):
        parsed = kwargs
    elif isinstance(kwargs, str):
        if not kwargs.strip():
            return
        try:
            parsed = json.loads(kwargs)
        except (ValueError, TypeError) as exc:
            raise _invalid(f"chat_template_kwargs is not valid JSON: {exc}") from exc
    else:
        raise _invalid("chat_template_kwargs must be a JSON object string or a dict")

    if not isinstance(parsed, dict):
        raise _invalid("chat_template_kwargs must be a JSON object")

    backend["chat_template_kwargs"] = json.dumps(
        _coerce_jsonish(parsed), ensure_ascii=False, separators=(",", ":")
    )
