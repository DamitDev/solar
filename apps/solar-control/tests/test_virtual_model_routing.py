"""Tests for virtual model routing in the gateway (S-060)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import OpenAIGateway, VirtualModelUnavailableError
from app.models.virtual_model import (
    VirtualModelContract,
    VirtualModelResponse,
)


def _vm(name, targets, contract=None):
    return VirtualModelResponse(
        id=f"00000000-0000-0000-0000-00000000000{name}",
        name=name,
        targets=list(targets),
        contract=contract or VirtualModelContract(),
    )


def _inst(ctx=4096, caps=None):
    return SimpleNamespace(context_size=ctx, capabilities=caps)


@pytest.fixture
def gateway():
    g = OpenAIGateway()
    g._ensure_session = AsyncMock()
    g.session = object()
    return g


class TestResolveVirtual:
    @pytest.mark.anyio
    async def test_not_a_virtual_returns_none_no_reasons(self, gateway):
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=[],
        ):
            target, reasons = await gateway._resolve_virtual("a:8b", None)
        assert target is None
        assert reasons == {}

    @pytest.mark.anyio
    async def test_first_target_selected_in_order(self, gateway):
        registry = {
            "first:8b": [_inst()],
            "second:8b": [_inst()],
        }
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("team", ("first:8b", "second:8b"))],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            target, reasons = await gateway._resolve_virtual("team", None)
        assert target == "first:8b"
        assert reasons == {}

    @pytest.mark.anyio
    async def test_dead_target_skipped(self, gateway):
        registry = {"second:8b": [_inst()]}
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("team", ("dead:8b", "second:8b"))],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            target, reasons = await gateway._resolve_virtual("team", None)
        assert target == "second:8b"
        assert reasons["dead:8b"] == "unavailable"

    @pytest.mark.anyio
    async def test_contract_violating_target_skipped(self, gateway):
        registry = {
            "small:8b": [_inst(ctx=131_072)],
            "big:8b": [_inst(ctx=1_048_576)],
        }
        contract = VirtualModelContract(context_size=200_000)
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("team", ("small:8b", "big:8b"), contract)],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            target, reasons = await gateway._resolve_virtual("team", None)
        assert target == "big:8b"
        assert reasons["small:8b"].startswith("contract_violation:")

    @pytest.mark.anyio
    async def test_unknown_capability_counts_as_violation(self, gateway):
        registry = {"quiet:8b": [_inst(ctx=1_048_576, caps=None)]}
        contract = VirtualModelContract(capabilities=["multimodal"])
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("vision", ("quiet:8b",), contract)],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            target, reasons = await gateway._resolve_virtual("vision", None)
        assert target is None
        assert reasons["quiet:8b"].startswith("contract_violation:")

    @pytest.mark.anyio
    async def test_all_targets_dead_yields_reasons(self, gateway):
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("team", ("dead:8b", "gone:8b"))],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value={}),
            ),
        ):
            target, reasons = await gateway._resolve_virtual("team", None)
        assert target is None
        assert reasons == {"dead:8b": "unavailable", "gone:8b": "unavailable"}

    @pytest.mark.anyio
    async def test_scoped_endpoint_cannot_reach_hidden_virtual(self, gateway):
        """A scoped endpoint that doesn't match the virtual name gets nothing."""
        registry = {"first:8b": [_inst()]}
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("team", ("first:8b",))],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            target, reasons = await gateway._resolve_virtual("team", ["other:*"])
        assert target is None
        assert reasons == {}

    @pytest.mark.anyio
    async def test_scoped_endpoint_matching_virtual_routes_beyond_scope(self, gateway):
        """The virtual is the scope boundary: targets bypass endpoint globs."""
        registry = {"hidden-internal:8b": [_inst()]}
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("team", ("hidden-internal:8b",))],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            target, _ = await gateway._resolve_virtual("team", ["team*"])
        assert target == "hidden-internal:8b"


class TestVirtualModelsEntries:
    def test_appended_to_both_arrays(self):
        result = {"models": [], "data": []}
        vm = _vm("team", ("a:8b",), VirtualModelContract(context_size=200_000))
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=[vm],
        ):
            OpenAIGateway._append_virtual_entries(result, None)
        assert result["data"][0]["id"] == "team"
        assert result["data"][0]["max_model_len"] == 200_000
        assert result["data"][0]["owned_by"] == "solar-virtual"
        assert result["models"][0]["name"] == "team"

    def test_undeclared_contract_fields_omitted(self):
        result = {"models": [], "data": []}
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=[_vm("plain", ("a:8b",))],
        ):
            OpenAIGateway._append_virtual_entries(result, None)
        assert "max_model_len" not in result["data"][0]
        assert "capabilities" not in result["data"][0]

    def test_pattern_filtering(self):
        result = {"models": [], "data": []}
        vms = [_vm("team", ("a:8b",)), _vm("other", ("b:8b",))]
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=vms,
        ):
            OpenAIGateway._append_virtual_entries(result, ["team*"])
        assert [m["id"] for m in result["data"]] == ["team"]


class TestUnavailableError:
    def test_is_value_error(self):
        exc = VirtualModelUnavailableError("team", {"a:8b": "unavailable"})
        assert isinstance(exc, ValueError)
        assert "team" in str(exc)
        assert exc.reasons == {"a:8b": "unavailable"}
