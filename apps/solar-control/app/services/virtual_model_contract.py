"""Contract check for virtual models (S-060).

Pure logic: given a candidate instance's reported context size and
capabilities, decide whether it satisfies a virtual model's declared
contract. Used at save time (validate existing targets) and at routing
time (skip targets that would silently violate the guarantee).
"""

from app.models.virtual_model import VirtualModelContract, VirtualModelResponse


def contract_violation(
    context_size: int | None,
    capabilities: list[str] | None,
    contract: VirtualModelContract,
) -> str | None:
    """Return a human-readable violation reason, or None if satisfied.

    An unknown context size (host did not report one) makes the contract
    unverifiable, not violated: routing serves the target and the webui
    flags it as unverified. Only a *contradicting* reported size is a
    violation.
    """
    if (
        contract.context_size is not None
        and context_size is not None
        and context_size < contract.context_size
    ):
        return f"context {context_size} < declared {contract.context_size}"
    if contract.capabilities:
        have = set(capabilities or [])
        missing = [c for c in contract.capabilities if c not in have]
        if missing:
            return f"missing capabilities: {', '.join(missing)}"
    return None


def enrich_target_status(
    vm: VirtualModelResponse, registry: dict[str, list]
) -> VirtualModelResponse:
    """Fill in target_status for one virtual model against the live registry."""
    status: dict[str, str] = {}
    for target in vm.targets:
        instances = registry.get(target)
        if not instances:
            status[target] = "missing"
            continue
        reasons = [
            reason
            for inst in instances
            if (
                reason := contract_violation(
                    getattr(inst, "context_size", None),
                    getattr(inst, "capabilities", None),
                    vm.contract,
                )
            )
            is not None
        ]
        if len(reasons) == len(instances):
            status[target] = f"violating: {reasons[0]}"
        elif vm.contract.context_size is not None and all(
            getattr(inst, "context_size", None) is None for inst in instances
        ):
            # The contract cannot be verified against this target (host
            # predates the context probe): serve-but-flag, never block.
            status[target] = "unverified"
        else:
            status[target] = "satisfied"
    return vm.model_copy(update={"target_status": status})
