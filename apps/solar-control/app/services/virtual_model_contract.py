"""Contract check for virtual models (S-060).

Pure logic: given a candidate instance's reported context size and
capabilities, decide whether it satisfies a virtual model's declared
contract. Used at save time (validate existing targets) and at routing
time (skip targets that would silently violate the guarantee).
"""

from app.models.virtual_model import VirtualModelContract


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
    if contract.context_size is not None:
        if context_size is None:
            return None
        if context_size < contract.context_size:
            return f"context {context_size} < declared {contract.context_size}"
    if contract.capabilities:
        have = set(capabilities or [])
        missing = [c for c in contract.capabilities if c not in have]
        if missing:
            return f"missing capabilities: {', '.join(missing)}"
    return None
