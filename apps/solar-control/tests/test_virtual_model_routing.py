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
            target, reasons, contract = await gateway._resolve_virtual("a:8b", None)
        assert target is None
        assert reasons == {}
        assert contract is None

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
            target, reasons, _ = await gateway._resolve_virtual("team", None)
        assert target == "first:8b"
        assert reasons == {}

    @pytest.mark.anyio
    async def test_returns_contract_for_instance_filtering(self, gateway):
        contract = VirtualModelContract(context_size=200_000)
        registry = {"big:8b": [_inst(ctx=1_048_576)]}
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=[_vm("team", ("big:8b",), contract)],
            ),
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
        ):
            target, _, returned = await gateway._resolve_virtual("team", None)
        assert target == "big:8b"
        assert returned is contract

    @pytest.mark.anyio
    async def test_empty_targets_explicitly_reported(self, gateway):
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=[_vm("hollow", ())],
        ):
            target, reasons, contract = await gateway._resolve_virtual("hollow", None)
        assert target is None
        assert reasons == {"<none>": "no targets configured"}
        assert contract is not None

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
            target, reasons, _ = await gateway._resolve_virtual("team", None)
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
            target, reasons, _ = await gateway._resolve_virtual("team", None)
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
            target, reasons, _ = await gateway._resolve_virtual("vision", None)
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
            target, reasons, _ = await gateway._resolve_virtual("team", None)
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
            target, reasons, _ = await gateway._resolve_virtual("team", ["other:*"])
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
            target, _, _ = await gateway._resolve_virtual("team", ["team*"])
        assert target == "hidden-internal:8b"


class TestVirtualModelsEntries:
    @pytest.mark.anyio
    async def test_appended_to_both_arrays(self, gateway):
        result = {"models": [], "data": []}
        vm = _vm("team", ("a:8b",), VirtualModelContract(context_size=200_000))
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=[vm],
        ):
            await gateway._append_virtual_entries(result, None)
        assert result["data"][0]["id"] == "team"
        assert result["data"][0]["max_model_len"] == 200_000
        assert result["data"][0]["owned_by"] == "solar-virtual"
        assert result["models"][0]["name"] == "team"

    @pytest.mark.anyio
    async def test_undeclared_contract_fields_omitted(self, gateway):
        result = {"models": [], "data": []}
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=[_vm("plain", ("a:8b",))],
        ):
            await gateway._append_virtual_entries(result, None)
        assert "max_model_len" not in result["data"][0]
        assert "capabilities" not in result["data"][0]

    @pytest.mark.anyio
    async def test_pattern_filtering(self, gateway):
        result = {"models": [], "data": []}
        vms = [_vm("team", ("a:8b",)), _vm("other", ("b:8b",))]
        with patch(
            "app.services.virtual_model_cache.virtual_model_cache.get_all",
            return_value=vms,
        ):
            await gateway._append_virtual_entries(result, ["team*"])
        assert [m["id"] for m in result["data"]] == ["team"]

    @pytest.mark.anyio
    async def test_cache_miss_loads_from_db(self, gateway):
        """Cache miss must populate from the DB, not silently skip (S-060 review)."""
        from app.services.virtual_model_cache import virtual_model_cache

        result = {"models": [], "data": []}
        with (
            patch(
                "app.services.virtual_model_cache.virtual_model_cache.get_all",
                return_value=None,
            ),
            patch(
                "app.database.virtual_models.virtual_model_db.list_all",
                new=AsyncMock(return_value=[_vm("team", ("a:8b",))]),
            ),
        ):
            await gateway._append_virtual_entries(result, None)
        assert [m["id"] for m in result["data"]] == ["team"]
        virtual_model_cache.invalidate()


class TestContractFilter:
    """The load balancer itself must honor the contract (S-060 review, major #1)."""

    @staticmethod
    def _routable(ctx, caps=None):

        return SimpleNamespace(
            context_size=ctx,
            capabilities=caps,
            host_id="h1",
            instance_id=f"i-{ctx}",
            model_alias="mixed:8b",
            supported_endpoints=["/v1/chat/completions"],
        )

    @pytest.mark.anyio
    async def test_get_next_instance_skips_violating_instance(self, gateway):
        registry = {
            "mixed:8b": [
                self._routable(ctx=131_072),  # violates the 200K contract
                self._routable(ctx=1_048_576),  # satisfies it
            ]
        }
        contract = VirtualModelContract(context_size=200_000)
        with (
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
            patch(
                "app.gateway.OpenAIGateway._resolve_model_name",
                return_value="mixed:8b",
            ),
            patch(
                "app.redis_state.health_store.is_healthy",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.redis_state.routing_store.get_host_active",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "app.redis_state.routing_store.get_weight",
                new=AsyncMock(return_value=0.0),
            ),
            patch(
                "app.gateway.OpenAIGateway._get_host_name",
                new=AsyncMock(return_value="h1"),
            ),
        ):
            instance = await gateway._get_next_instance(
                "mixed:8b", contract_filter=contract
            )
        assert instance is not None
        assert instance.context_size == 1_048_576

    @pytest.mark.anyio
    async def test_get_next_instance_none_when_all_violate(self, gateway):
        registry = {"small:8b": [self._routable(ctx=131_072)]}
        contract = VirtualModelContract(context_size=200_000)
        with (
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
            patch(
                "app.gateway.OpenAIGateway._resolve_model_name",
                return_value="small:8b",
            ),
        ):
            instance = await gateway._get_next_instance(
                "small:8b", contract_filter=contract
            )
        assert instance is None

    @pytest.mark.anyio
    async def test_get_next_instance_unfiltered_without_contract(self, gateway):
        """Plain (non-virtual) routing is untouched by the filter."""
        registry = {"any:8b": [self._routable(ctx=131_072)]}
        with (
            patch(
                "app.redis_state.registry_store.get_registry",
                new=AsyncMock(return_value=registry),
            ),
            patch(
                "app.gateway.OpenAIGateway._resolve_model_name",
                return_value="any:8b",
            ),
            patch(
                "app.redis_state.health_store.is_healthy",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.redis_state.routing_store.get_host_active",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "app.redis_state.routing_store.get_weight",
                new=AsyncMock(return_value=0.0),
            ),
            patch(
                "app.gateway.OpenAIGateway._get_host_name",
                new=AsyncMock(return_value="h1"),
            ),
        ):
            instance = await gateway._get_next_instance("any:8b")
        assert instance is not None


class TestUnavailableError:
    def test_is_value_error(self):
        exc = VirtualModelUnavailableError("team", {"a:8b": "unavailable"})
        assert isinstance(exc, ValueError)
        assert "team" in str(exc)
        assert exc.reasons == {"a:8b": "unavailable"}


class TestSafestreamPayload:
    """Exhausted virtuals keep their structured reasons in the SSE relay."""

    @pytest.mark.anyio
    async def test_stream_payload_carries_code_and_targets(self):
        import json as jsonlib

        from app.routes.openai import _safe_stream

        captured = []

        class _FakeStream:
            def __init__(self):
                self._done = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._done:
                    raise StopAsyncIteration
                self._done = True
                raise VirtualModelUnavailableError(
                    "team", {"a:8b": "unavailable", "b:8b": "contract_violation: x"}
                )

            async def aclose(self):
                return None

        with patch(
            "app.routes.openai.gateway.stream_request",
            return_value=_FakeStream(),
        ):
            gen = _safe_stream("team", "/v1/chat/completions", {}, "ip", None, None)
            async for chunk in gen.body_iterator:
                captured.append(chunk)

        payload = jsonlib.loads(captured[0].decode().removeprefix("data: ").strip())
        assert payload["code"] == "virtual_model_unavailable"
        assert payload["targets"]["a:8b"] == "unavailable"
        assert "team" in payload["error"]
