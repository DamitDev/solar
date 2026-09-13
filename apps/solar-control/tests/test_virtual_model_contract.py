"""Unit tests for the virtual model contract check (S-060)."""

import pytest
from pydantic import ValidationError

from app.models.virtual_model import VirtualModelContract
from app.services.virtual_model_contract import contract_violation


class TestContractViolation:
    def test_satisfied_when_no_constraints(self):
        contract = VirtualModelContract()
        assert contract_violation(4096, ["completion"], contract) is None

    def test_satisfied_when_context_equal(self):
        contract = VirtualModelContract(context_size=200_000)
        assert contract_violation(200_000, [], contract) is None

    def test_satisfied_when_context_larger(self):
        contract = VirtualModelContract(context_size=200_000)
        assert contract_violation(1_048_576, [], contract) is None

    def test_violation_when_context_smaller(self):
        contract = VirtualModelContract(context_size=200_000)
        reason = contract_violation(131_072, [], contract)
        assert reason is not None
        assert "131072" in reason or "131,072" in reason

    def test_unverified_when_context_unknown_without_contract(self):
        """No context declared: an unknown size is irrelevant."""
        contract = VirtualModelContract()
        assert contract_violation(None, [], contract) is None

    def test_unverified_when_context_unknown(self):
        """A host that reports no context size makes the contract unverifiable.

        Unverified is NOT a violation: SGLang instances pre-dating the host
        probe would never route at all otherwise. Routing serves the target
        and flags it; only a CONTRADICTING size is a violation.
        """
        contract = VirtualModelContract(context_size=200_000)
        assert contract_violation(None, [], contract) is None
        assert contract_violation(None, None, contract) is None
        assert contract_violation(None, [], contract, unknown_is_violation=True) is not None

    def test_violation_when_capability_missing(self):
        contract = VirtualModelContract(capabilities=["multimodal"])
        reason = contract_violation(4096, ["completion"], contract)
        assert reason is not None
        assert "multimodal" in reason

    def test_violation_when_capabilities_unknown(self):
        contract = VirtualModelContract(capabilities=["multimodal"])
        reason = contract_violation(4096, None, contract)
        assert reason is not None
        assert "multimodal" in reason

    def test_satisfied_when_all_capabilities_present(self):
        contract = VirtualModelContract(
            context_size=200_000, capabilities=["multimodal", "completion"]
        )
        assert (
            contract_violation(262_144, ["completion", "multimodal"], contract) is None
        )

    def test_reports_all_missing_capabilities(self):
        contract = VirtualModelContract(capabilities=["multimodal", "embedding"])
        reason = contract_violation(4096, ["completion"], contract)
        assert "multimodal" in reason
        assert "embedding" in reason

    def test_no_contract_object_rejected(self):
        with pytest.raises(ValidationError):
            VirtualModelContract(context_size=0)
        with pytest.raises(ValidationError):
            VirtualModelContract(context_size=-5)
