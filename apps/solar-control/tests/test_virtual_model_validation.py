"""Unit tests for virtual model save-time validation (S-060)."""

from types import SimpleNamespace

from app.models.virtual_model import VirtualModelContract, VirtualModelCreate
from app.services.virtual_model_validation import (
    validate_virtual_model,
)


def _inst(ctx=4096, caps=None):
    return SimpleNamespace(context_size=ctx, capabilities=caps)


def _payload(name="team-chat", targets=("a:8b",), contract=None):
    return VirtualModelCreate(
        name=name,
        targets=list(targets),
        contract=contract or VirtualModelContract(),
    )


class TestValidateVirtualModel:
    def test_valid_minimal_no_warnings_when_target_exists(self):
        registry = {"a:8b": [_inst()]}
        errors, warnings = validate_virtual_model(_payload(), registry)
        assert errors == []
        assert warnings == []

    def test_missing_target_is_warning_not_error(self):
        errors, warnings = validate_virtual_model(_payload(), {})
        assert errors == []
        assert len(warnings) == 1
        assert "not in the registry yet" in warnings[0]

    def test_bad_name_rejected(self):
        errors, _ = validate_virtual_model(_payload(name="-bad"), {})
        assert any("invalid name" in e for e in errors)

    def test_duplicate_targets_rejected(self):
        errors, _ = validate_virtual_model(
            _payload(targets=("a:8b", "a:8b")), {"a:8b": [_inst()]}
        )
        assert any("duplicate" in e for e in errors)

    def test_exact_shadow_rejected(self):
        registry = {"a:8b": [_inst()]}
        errors, _ = validate_virtual_model(_payload(name="a:8b"), registry)
        assert any("collides with registry alias" in e for e in errors)

    def test_virtual_prefix_of_alias_rejected(self):
        registry = {"team-chat:latest": [_inst()]}
        errors, _ = validate_virtual_model(_payload(name="team-chat"), registry)
        assert any("collides" in e for e in errors)

    def test_alias_prefix_of_virtual_rejected(self):
        registry = {"team": [_inst()]}
        errors, _ = validate_virtual_model(_payload(name="team-chat"), registry)
        assert any("collides" in e for e in errors)

    def test_unrelated_name_allowed(self):
        registry = {"other:1b": [_inst()]}
        errors, _ = validate_virtual_model(_payload(name="team-chat"), registry)
        assert errors == []

    def test_existing_violating_target_rejected(self):
        registry = {"small:8b": [_inst(ctx=131_072)]}
        contract = VirtualModelContract(context_size=200_000)
        errors, _ = validate_virtual_model(
            _payload(targets=("small:8b",), contract=contract), registry
        )
        assert any("cannot satisfy the contract" in e for e in errors)

    def test_existing_satisfying_target_passes(self):
        registry = {"big:32b": [_inst(ctx=1_048_576, caps=["multimodal"])]}
        contract = VirtualModelContract(
            context_size=200_000, capabilities=["multimodal"]
        )
        errors, warnings = validate_virtual_model(
            _payload(targets=("big:32b",), contract=contract), registry
        )
        assert errors == []
        assert warnings == []

    def test_partial_violation_passes(self):
        """One violating instance among several is fine — routing skips it."""
        registry = {"mixed:8b": [_inst(ctx=131_072), _inst(ctx=1_048_576)]}
        contract = VirtualModelContract(context_size=200_000)
        errors, _ = validate_virtual_model(
            _payload(contract=contract), registry
        )
        assert errors == []
