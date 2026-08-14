"""Shared validation helpers (S-036, S-040).

Centralized validators used by route handlers and services so
priorities, constraints, and error formats stay consistent across
the codebase.
"""

import json
import math
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


def validate_intent_warnings(data: dict[str, Any]) -> list[dict[str, str]]:
    """Static (non-fleet) advisory warnings for an intent payload (C3).

    Warnings never block an edit; they ride along on the success response
    (``IntentResponse.warnings``). Dynamic fleet-derived warnings live in
    ``app.services.intent_validation.validate_intent_fleet``.
    """
    warnings: list[dict[str, str]] = []
    backend = data.get("backend", {})
    if not isinstance(backend, dict) or backend.get("backend_type") != "llamacpp":
        return warnings

    if backend.get("pooling") is not None and backend.get("model_type") != "embedding":
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


def canonicalize_intent_backend(backend: dict[str, Any]) -> None:
    """Normalize the backend fields the host rewrites on parse, in place (C1).

    ``chat_template_kwargs`` is parsed (accepting a JSON string, as the webui
    form sends, or a dict), recursively coerced so boolean-looking strings
    become real booleans, and re-serialized as compact canonical JSON — the
    exact form the host's ``LlamaCppConfig.normalize_chat_template_kwargs``
    produces. ``devices`` and ``tensor_split`` get the same treatment for
    their comma-separated form. Storing the canonical form means new intents
    never carry a representation that drift detection would flag; the
    normalization-aware comparison in the reconciler remains for
    already-stored intents.

    Raises HTTPException(422) on malformed JSON or a non-object value.
    """
    _canonicalize_device_lists(backend)

    kwargs = backend.get("chat_template_kwargs")
    if kwargs is None:
        return

    def _invalid(message: str) -> HTTPException:
        return _invalid_backend_field("chat_template_kwargs", message)

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
