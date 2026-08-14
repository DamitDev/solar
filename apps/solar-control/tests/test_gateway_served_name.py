"""Translating between the alias and the name a backend is served under.

Solar routes on the alias, but SGLang reads `a:b` as base model `a` plus LoRA
adapter `b`, so a `name:tag` alias cannot be its served model name. The host
reports what it launched, and the gateway translates in both directions:
requests going out, `/v1/models` coming back.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import OpenAIGateway
from app.models import Host, HostStatus, RegistryEntry

ALIAS = "deepseek-v4-flash:284b"
SERVED = "deepseek-v4-flash-284b"


class _Response:
    def __init__(self, status: int, payload=None, lines=()):
        self.status = status
        self._payload = payload
        self.content = _lines(lines)

    async def json(self):
        return self._payload


async def _lines(lines):
    for line in lines:
        yield line


class _RequestContext:
    def __init__(self, response: _Response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingSession:
    """Captures the body the gateway forwards upstream."""

    closed = False

    def __init__(self, response: _Response):
        self._response = response
        self.posted: dict = {}

    def get(self, *args, **kwargs):
        return _RequestContext(self._response)

    def post(self, url, *args, **kwargs):
        self.posted = kwargs.get("json") or {}
        return _RequestContext(self._response)


def _entry(**overrides) -> RegistryEntry:
    return RegistryEntry(
        **{
            "host_id": "host-1",
            "instance_id": "inst-1",
            "url": "http://test-host:3500",
            "api_key": "instance-api-key",
            "model_alias": ALIAS,
            "backend_type": "sglang",
            "served_model_name": SERVED,
            **overrides,
        }
    )


class TestOutgoingRequest:
    def test_the_model_becomes_the_name_the_backend_answers_to(self):
        body = OpenAIGateway._upstream_body(_entry(), {"model": ALIAS, "stream": True})

        assert body == {"model": SERVED, "stream": True}

    def test_a_partial_name_the_client_used_is_replaced_too(self):
        """Routing resolves prefixes, so what arrives here need not be the alias."""
        body = OpenAIGateway._upstream_body(_entry(), {"model": "deepseek-v4-flash"})

        assert body["model"] == SERVED

    def test_a_backend_serving_the_alias_gets_the_body_untouched(self):
        """llama.cpp and HuggingFace have always seen the client's model string;
        rewriting theirs would be an unrelated behaviour change."""
        data = {"model": "qwen3.6", "stream": True}
        entry = _entry(
            model_alias="qwen3.6:35b",
            backend_type="llamacpp",
            served_model_name="qwen3.6:35b",
        )

        assert OpenAIGateway._upstream_body(entry, data) is data

    def test_a_host_that_reports_no_served_name_is_taken_at_its_word(self):
        """It is the host that ran the command, so silence means "the alias" —
        inventing a translation here would break instances it launched."""
        data = {"model": ALIAS}

        assert OpenAIGateway._upstream_body(_entry(served_model_name=None), data) is data

    def test_the_caller_s_body_is_not_mutated(self):
        data = {"model": ALIAS}

        OpenAIGateway._upstream_body(_entry(), data)

        assert data == {"model": ALIAS}


class TestAdvertisedModels:
    def test_both_payload_shapes_get_the_alias_back(self):
        payload = {
            "models": [{"name": SERVED, "model": SERVED, "capabilities": ["completion"]}],
            "data": [{"id": SERVED, "object": "model"}],
        }

        patched = OpenAIGateway._restore_alias_in_models(payload, _entry())

        assert patched["models"][0]["name"] == ALIAS
        assert patched["models"][0]["model"] == ALIAS
        assert patched["models"][0]["capabilities"] == ["completion"]
        assert patched["data"][0]["id"] == ALIAS

    def test_an_unrelated_name_is_left_alone(self):
        payload = {"data": [{"id": "some-other-model"}]}

        patched = OpenAIGateway._restore_alias_in_models(payload, _entry())

        assert patched["data"][0]["id"] == "some-other-model"

    @pytest.mark.anyio
    async def test_the_advertised_id_is_the_one_clients_can_route_to(self):
        """A client discovers models here and sends the id straight back, so an
        id only SGLang knows would 404 on the next call."""
        gateway = OpenAIGateway()
        gateway.session = _RecordingSession(
            _Response(200, {"models": [], "data": [{"id": SERVED, "object": "model"}]})
        )

        with patch(
            "app.gateway.registry_store.get_registry",
            AsyncMock(return_value={ALIAS: [_entry()]}),
        ):
            result = await gateway.get_available_models()

        assert [m["id"] for m in result["data"]] == [ALIAS]


@pytest.mark.anyio
async def test_a_streamed_request_reaches_the_backend_translated():
    """The wiring, not just the helper: what actually goes on the wire."""
    gateway = OpenAIGateway()
    session = _RecordingSession(_Response(200, lines=[b"data: {}\n"]))
    gateway.session = session
    entry = _entry()
    host = Host(
        id="host-1",
        name="Test Host",
        url="http://test-host:8000",
        api_key="host-api-key",
        status=HostStatus.ONLINE,
    )

    with (
        patch.object(gateway, "_ensure_session", AsyncMock()),
        patch.object(gateway, "_broadcast_routing_event", AsyncMock()),
        patch.object(gateway, "_find_instance_or_retry", AsyncMock(return_value=entry)),
        patch.object(gateway, "_fetch_last_generation_metrics", AsyncMock(return_value={})),
        patch.object(gateway, "_emit_success", AsyncMock()),
        patch("app.gateway.host_db.get_host", AsyncMock(return_value=host)),
        patch("app.gateway.health_store.mark_healthy", AsyncMock()),
        patch("app.gateway.routing_store", AsyncMock()),
    ):
        chunks = [
            chunk
            async for chunk in gateway.stream_request(
                model=ALIAS,
                endpoint="/v1/chat/completions",
                data={"model": ALIAS, "stream": True},
            )
        ]

    assert chunks == [b"data: {}\n"]
    assert session.posted["model"] == SERVED


class TestRegistryEntry:
    def test_the_ws_instance_payload_carries_the_served_name(self):
        entry = RegistryEntry.from_ws_instance(
            host_id="host-1",
            host_url="http://test-host:8000",
            host_api_key="k",
            instance={
                "id": "inst-1",
                "alias": ALIAS,
                "backend_type": "sglang",
                "served_model_name": SERVED,
                "port": 3500,
            },
        )

        assert entry.served_model_name == SERVED

    def test_the_http_instance_payload_carries_the_served_name(self):
        entry = RegistryEntry.from_http_instance(
            host_id="host-1",
            host_url="http://test-host:8000",
            instance={
                "id": "inst-1",
                "port": 3500,
                "served_model_name": SERVED,
                "config": {"alias": ALIAS, "backend_type": "sglang", "api_key": "k"},
            },
        )

        assert entry.served_model_name == SERVED

    def test_the_http_poll_cache_keeps_it_for_the_ws_shape(self):
        """The cache re-seeded from polling is read back through
        from_ws_instance, so dropping it there would strand the translation."""
        cached = OpenAIGateway._ws_cache_from_http_instances(
            [
                {
                    "id": "inst-1",
                    "port": 3500,
                    "served_model_name": SERVED,
                    "config": {"alias": ALIAS, "backend_type": "sglang"},
                }
            ]
        )

        assert cached[0]["served_model_name"] == SERVED

    def test_an_older_host_leaves_it_unset(self):
        entry = RegistryEntry.from_ws_instance(
            host_id="host-1",
            host_url="http://test-host:8000",
            host_api_key="k",
            instance={"id": "inst-1", "alias": "qwen3.6:35b", "port": 3500},
        )

        assert entry.served_model_name is None
