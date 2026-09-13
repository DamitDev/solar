"""Save-time validation for virtual models (S-060).

Hard errors become HTTP 422; soft problems are returned as warnings and
ride along on the 201/200 response. Target contract satisfaction is only
enforced against targets that currently exist in the registry — missing
targets are allowed with a warning and re-checked lazily at routing time
(``app.services.virtual_model_contract``).
"""

import re

from app.models.virtual_model import NAME_PATTERN, VirtualModelCreate

_NAME_RE = re.compile(NAME_PATTERN)


def _registry_alias_conflicts(name: str, registry_aliases: list[str]) -> list[str]:
    """Registry aliases that an exact-or-prefix relationship with `name` breaks.

    The gateway matches registry aliases by exact name first, then by prefix.
    A virtual whose name equals a registry alias would be unreachable
    (virtual lookup wins); a virtual whose name is a prefix of a registry
    alias (or vice versa) would change where previously prefix-matched
    requests route. Both directions are rejected.
    """
    return [
        alias
        for alias in registry_aliases
        if name == alias or name.startswith(alias) or alias.startswith(name)
    ]


def validate_virtual_model(
    data: VirtualModelCreate, registry: dict[str, list]
) -> tuple[list[str], list[str]]:
    """Validate a create/update payload against the live registry.

    Args:
        data: The payload to validate.
        registry: Mapping of registry alias -> list of instances (objects
            carrying ``context_size`` and ``capabilities``), as returned by
            ``registry_store.get_registry()``.

    Returns:
        Tuple of (hard_errors, warnings). When hard_errors is non-empty the
        caller must reject the request with a 422.
    """
    from app.services.virtual_model_contract import contract_violation

    errors: list[str] = []
    warnings: list[str] = []

    if not _NAME_RE.fullmatch(data.name):
        errors.append(
            f"invalid name '{data.name}': must start with alphanumeric and "
            "contain only alphanumerics, dots, underscores, colons and dashes"
        )

    if len(set(data.targets)) != len(data.targets):
        errors.append("duplicate targets are not allowed")

    conflicts = _registry_alias_conflicts(data.name, list(registry))
    for alias in conflicts:
        errors.append(
            f"name '{data.name}' collides with registry alias '{alias}' "
            "(exact or prefix relationship)"
        )

    for target in data.targets:
        instances = registry.get(target)
        if not instances:
            warnings.append(
                f"target '{target}' is not in the registry yet — "
                "will be checked at routing time"
            )
            continue
        reasons = [
            reason
            for inst in instances
            if (
                reason := contract_violation(
                    getattr(inst, "context_size", None),
                    getattr(inst, "capabilities", None),
                    data.contract,
                )
            )
            is not None
        ]
        if len(reasons) == len(instances):
            errors.append(
                f"target '{target}' cannot satisfy the contract: {reasons[0]}"
            )

    return errors, warnings


def validation_errors_to_detail(errors: list[str]) -> dict:
    """HTTP 422 body matching the intents route's error shape."""
    return {"detail": "Invalid virtual model", "errors": errors}
