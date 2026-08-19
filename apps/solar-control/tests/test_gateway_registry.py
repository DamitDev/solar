from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import OpenAIGateway
from app.models import Host, HostStatus, RegistryEntry


class _Response:
    def __init__(self, status: int, payload=None):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _RequestContext:
    def __init__(self, response: _Response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Session:
    closed = False

    def __init__(self, response: _Response):
        self._response = response

    def get(self, *args, **kwargs):
        return _RequestContext(self._response)


class _URLSession:
    closed = False

    def __init__(self, responses: dict[str, _Response]):
        self._responses = responses

    def get(self, url, *args, **kwargs):
        return _RequestContext(self._responses[url])


class _RaisingSession:
    """Connection-level failure — drives poll_host's Redis-cache fallback."""

    closed = False

    def get(self, *args, **kwargs):
        raise ConnectionError("host unreachable")


@pytest.fixture
def host():
    return Host(
        id="host-1",
        name="Test Host",
        url="http://test-host:8000",
        api_key="host-api-key",
        status=HostStatus.ONLINE,
    )


@pytest.mark.anyio
async def test_refresh_recovers_connected_host_with_empty_instance_cache(host):
    instances = [
        {
            "id": "inst-1",
            "status": "running",
            "port": 3500,
            "supported_endpoints": ["/v1/chat/completions", "/v1/models"],
            "config": {
                "alias": "model-a",
                "api_key": "instance-api-key",
                "backend_type": "llamacpp",
            },
        }
    ]

    gateway = OpenAIGateway()
    gateway.session = _Session(_Response(200, instances))

    with (
        patch("app.gateway.host_db.get_all_hosts", AsyncMock(return_value=[host])),
        patch("app.gateway.host_db.update_host_status", AsyncMock()),
        patch(
            "app.socketio_app.host_handlers.is_host_connected",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.socketio_app.host_handlers.get_host_instances",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.gateway.host_store.get_disconnect_time", AsyncMock(return_value=None)
        ),
        patch("app.gateway.host_store.set_host_instances", AsyncMock()) as set_cache,
        patch("app.gateway.registry_store.set_registry", AsyncMock()) as set_registry,
    ):
        await gateway.refresh_model_registry()

    set_cache.assert_awaited_once_with(
        "host-1",
        [
            {
                "id": "inst-1",
                "alias": "model-a",
                "status": "running",
                "port": 3500,
                "supported_endpoints": ["/v1/chat/completions", "/v1/models"],
                "served_model_name": None,
                "capabilities": None,
                "backend_type": "llamacpp",
                "api_key": "instance-api-key",
                "managed_by": None,
                "intent_id": None,
                "model_source": None,
                "priority": None,
            }
        ],
    )

    registry = set_registry.await_args.args[0]
    assert list(registry) == ["model-a"]
    assert registry["model-a"][0].instance_id == "inst-1"
    assert registry["model-a"][0].api_key == "instance-api-key"


@pytest.mark.anyio
async def test_refresh_keeps_previous_registry_when_polling_fails(host):
    """The only host is unreachable, so its alias must be carried forward."""
    previous = {
        "model-a": [
            RegistryEntry(
                host_id="host-1",
                instance_id="inst-1",
                url="http://test-host:3500",
                api_key="key",
                model_alias="model-a",
            )
        ]
    }

    gateway = OpenAIGateway()
    gateway.session = _Session(_Response(503))

    with (
        patch("app.gateway.host_db.get_all_hosts", AsyncMock(return_value=[host])),
        patch("app.gateway.host_db.update_host_status", AsyncMock()),
        patch(
            "app.socketio_app.host_handlers.is_host_connected",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.gateway.host_store.get_disconnect_time", AsyncMock(return_value=None)
        ),
        patch("app.gateway.host_store.get_host_instances", AsyncMock(return_value=[])),
        patch(
            "app.gateway.registry_store.get_registry", AsyncMock(return_value=previous)
        ),
        patch("app.gateway.registry_store.set_registry", AsyncMock()) as set_registry,
    ):
        await gateway.refresh_model_registry()

    # The registry is always written; the carry-forward is what preserves the
    # alias, so assert on the surviving entry rather than on "we wrote nothing".
    registry = set_registry.await_args.args[0]
    assert [e.instance_id for e in registry["model-a"]] == ["inst-1"]


@pytest.mark.anyio
async def test_refresh_drops_stale_alias_on_healthy_host_when_another_host_fails():
    """A dead host must not freeze de-registration on the hosts that answered.

    The healthy host now reports zero running instances, so its alias is gone
    for real and has to leave the registry; the unreachable host's alias has
    no fresh evidence either way and is carried forward.
    """
    healthy = Host(
        id="host-healthy",
        name="Healthy Host",
        url="http://healthy-host:8000",
        api_key="healthy-key",
        status=HostStatus.ONLINE,
    )
    dead = Host(
        id="host-dead",
        name="Dead Host",
        url="http://dead-host:8000",
        api_key="dead-key",
        status=HostStatus.ONLINE,
    )
    previous = {
        "model-healthy": [
            RegistryEntry(
                host_id="host-healthy",
                instance_id="inst-healthy",
                url="http://healthy-host:3500",
                api_key="key",
                model_alias="model-healthy",
            )
        ],
        "model-dead": [
            RegistryEntry(
                host_id="host-dead",
                instance_id="inst-dead",
                url="http://dead-host:3500",
                api_key="key",
                model_alias="model-dead",
            )
        ],
    }

    gateway = OpenAIGateway()
    gateway.session = _URLSession(
        {
            "http://healthy-host:8000/instances": _Response(200, []),
            "http://dead-host:8000/instances": _Response(503),
        }
    )

    with (
        patch(
            "app.gateway.host_db.get_all_hosts",
            AsyncMock(return_value=[healthy, dead]),
        ),
        patch("app.gateway.host_db.update_host_status", AsyncMock()),
        patch(
            "app.socketio_app.host_handlers.is_host_connected",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.gateway.host_store.get_disconnect_time", AsyncMock(return_value=None)
        ),
        patch("app.gateway.host_store.get_host_instances", AsyncMock(return_value=[])),
        patch("app.gateway.host_store.set_host_instances", AsyncMock()),
        patch(
            "app.gateway.registry_store.get_registry", AsyncMock(return_value=previous)
        ),
        patch("app.gateway.registry_store.set_registry", AsyncMock()) as set_registry,
    ):
        await gateway.refresh_model_registry()

    registry = set_registry.await_args.args[0]
    assert "model-healthy" not in registry
    assert [e.instance_id for e in registry["model-dead"]] == ["inst-dead"]


@pytest.mark.anyio
async def test_refresh_does_not_duplicate_carried_forward_entry(host):
    """A host that fails over to its Redis cache keeps exactly one entry.

    The cache fallback re-emits the instance *and* the host is marked failed,
    so a carry-forward that ignored ``(host_id, instance_id)`` identity would
    register the same upstream twice and skew load balancing.
    """
    cached = [
        {
            "id": "inst-1",
            "alias": "model-a",
            "status": "running",
            "port": 3500,
            "backend_type": "llamacpp",
            "api_key": "key",
        }
    ]
    previous = {
        "model-a": [
            RegistryEntry(
                host_id="host-1",
                instance_id="inst-1",
                url="http://test-host:3500",
                api_key="key",
                model_alias="model-a",
            )
        ]
    }

    gateway = OpenAIGateway()
    gateway.session = _RaisingSession()

    with (
        patch("app.gateway.host_db.get_all_hosts", AsyncMock(return_value=[host])),
        patch("app.gateway.host_db.update_host_status", AsyncMock()),
        patch(
            "app.socketio_app.host_handlers.is_host_connected",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.gateway.host_store.get_disconnect_time", AsyncMock(return_value=None)
        ),
        patch(
            "app.socketio_app.host_handlers.get_host_instances",
            AsyncMock(return_value=cached),
        ),
        patch(
            "app.gateway.registry_store.get_registry", AsyncMock(return_value=previous)
        ),
        patch("app.gateway.registry_store.set_registry", AsyncMock()) as set_registry,
    ):
        await gateway.refresh_model_registry()

    registry = set_registry.await_args.args[0]
    assert [e.instance_id for e in registry["model-a"]] == ["inst-1"]


@pytest.mark.anyio
async def test_http_registry_preserves_llamacpp_context_size(host):
    instances = [
        {
            "id": "inst-1",
            "status": "running",
            "port": 3500,
            "supported_endpoints": ["/v1/chat/completions", "/v1/models"],
            "config": {
                "alias": "qwen3.6:35b",
                "api_key": "instance-api-key",
                "backend_type": "llamacpp",
                "ctx_size": 40960,
            },
        }
    ]

    gateway = OpenAIGateway()
    gateway.session = _Session(_Response(200, instances))

    with (
        patch("app.gateway.host_db.get_all_hosts", AsyncMock(return_value=[host])),
        patch("app.gateway.host_db.update_host_status", AsyncMock()),
        patch(
            "app.socketio_app.host_handlers.is_host_connected",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.gateway.host_store.get_disconnect_time",
            AsyncMock(return_value=None),
        ),
        patch("app.gateway.host_store.get_host_instances", AsyncMock(return_value=[])),
        patch("app.gateway.host_store.set_host_instances", AsyncMock()),
        patch("app.gateway.registry_store.set_registry", AsyncMock()) as set_registry,
    ):
        await gateway.refresh_model_registry()

    registry = set_registry.await_args.args[0]
    assert registry["qwen3.6:35b"][0].context_size == 40960


@pytest.mark.anyio
async def test_models_response_overrides_llamacpp_context_metadata(host):
    registry_entry = RegistryEntry(
        host_id="host-1",
        instance_id="inst-1",
        url="http://test-host:3500",
        api_key="instance-api-key",
        model_alias="qwen3.6:35b",
        context_size=40960,
    )
    upstream_models = {
        "models": [
            {
                "name": "qwen3.6:35b",
                "model": "qwen3.6:35b",
                "details": {"format": "gguf"},
                "capabilities": ["completion"],
            }
        ],
        "data": [
            {
                "id": "qwen3.6:35b",
                "object": "model",
                "owned_by": "llamacpp",
                "meta": {"n_ctx_train": 262144, "n_params": 34660610688},
            }
        ],
    }

    gateway = OpenAIGateway()
    gateway.session = _URLSession(
        {"http://test-host:3500/v1/models": _Response(200, upstream_models)}
    )

    with patch(
        "app.gateway.registry_store.get_registry",
        AsyncMock(return_value={"qwen3.6:35b": [registry_entry]}),
    ):
        result = await gateway.get_available_models()

    assert result["data"][0]["meta"]["n_ctx_train"] == 40960
    assert result["data"][0]["meta"]["ctx_size"] == 40960
    assert result["data"][0]["capabilities"] == ["completion"]
    assert result["models"][0]["details"]["context_length"] == 40960


@pytest.mark.anyio
async def test_models_response_fetches_missing_context_size(host):
    registry_entry = RegistryEntry(
        host_id="host-1",
        instance_id="inst-1",
        url="http://test-host:3500",
        api_key="instance-api-key",
        model_alias="qwen3.6:35b",
    )
    upstream_models = {
        "models": [],
        "data": [
            {
                "id": "qwen3.6:35b",
                "object": "model",
                "owned_by": "llamacpp",
                "meta": {"n_ctx_train": 262144},
            }
        ],
    }
    instance_details = {
        "id": "inst-1",
        "config": {"backend_type": "llamacpp", "ctx_size": 40960},
    }

    gateway = OpenAIGateway()
    gateway.session = _URLSession(
        {
            "http://test-host:3500/v1/models": _Response(200, upstream_models),
            "http://test-host:8000/instances/inst-1": _Response(200, instance_details),
        }
    )

    with (
        patch(
            "app.gateway.registry_store.get_registry",
            AsyncMock(return_value={"qwen3.6:35b": [registry_entry]}),
        ),
        patch("app.gateway.host_db.get_host", AsyncMock(return_value=host)),
    ):
        result = await gateway.get_available_models()

    assert result["data"][0]["meta"]["n_ctx_train"] == 40960


@pytest.mark.anyio
async def test_models_response_stamps_host_capabilities_when_upstream_has_none():
    """SGLang upstreams advertise no capabilities — the host-derived ones win."""
    registry_entry = RegistryEntry(
        host_id="host-1",
        instance_id="inst-1",
        url="http://test-host:3500",
        api_key="instance-api-key",
        model_alias="qwen3.6:35b",
        backend_type="sglang",
        capabilities=["completion", "multimodal"],
    )
    upstream_models = {
        "models": [],
        "data": [
            {
                "id": "qwen3.6-35b",
                "object": "model",
                "owned_by": "sglang",
                "max_model_len": 262144,
            }
        ],
    }

    gateway = OpenAIGateway()
    gateway.session = _URLSession(
        {"http://test-host:3500/v1/models": _Response(200, upstream_models)}
    )

    with patch(
        "app.gateway.registry_store.get_registry",
        AsyncMock(return_value={"qwen3.6:35b": [registry_entry]}),
    ):
        result = await gateway.get_available_models()

    assert result["data"][0]["capabilities"] == ["completion", "multimodal"]


@pytest.mark.anyio
async def test_models_response_prefers_upstream_capabilities_over_host_ones():
    """llama.cpp advertises its own capabilities — upstream truth wins."""
    registry_entry = RegistryEntry(
        host_id="host-1",
        instance_id="inst-1",
        url="http://test-host:3500",
        api_key="instance-api-key",
        model_alias="qwen3.8:27b",
        capabilities=["completion", "multimodal"],
    )
    upstream_models = {
        "models": [
            {
                "name": "qwen3.8:27b",
                "model": "qwen3.8:27b",
                "capabilities": ["completion"],
            }
        ],
        "data": [
            {
                "id": "qwen3.8:27b",
                "object": "model",
                "owned_by": "llamacpp",
            }
        ],
    }

    gateway = OpenAIGateway()
    gateway.session = _URLSession(
        {"http://test-host:3500/v1/models": _Response(200, upstream_models)}
    )

    with patch(
        "app.gateway.registry_store.get_registry",
        AsyncMock(return_value={"qwen3.8:27b": [registry_entry]}),
    ):
        result = await gateway.get_available_models()

    assert result["data"][0]["capabilities"] == ["completion"]


def test_ws_instance_payload_carries_capabilities():
    entry = RegistryEntry.from_ws_instance(
        host_id="host-1",
        host_url="http://test-host:8000",
        host_api_key="k",
        instance={
            "id": "inst-1",
            "alias": "qwen3.6:35b",
            "backend_type": "sglang",
            "capabilities": ["completion", "multimodal"],
            "port": 3500,
        },
    )

    assert entry.capabilities == ["completion", "multimodal"]


def test_http_instance_payload_carries_capabilities():
    entry = RegistryEntry.from_http_instance(
        host_id="host-1",
        host_url="http://test-host:8000",
        instance={
            "id": "inst-1",
            "port": 3500,
            "capabilities": ["completion", "multimodal"],
            "config": {
                "alias": "qwen3.6:35b",
                "backend_type": "sglang",
                "api_key": "k",
            },
        },
    )

    assert entry.capabilities == ["completion", "multimodal"]


def test_http_poll_cache_keeps_capabilities_for_the_ws_shape():
    """The cache re-seeded from polling is read back through
    from_ws_instance, so dropping it there would strand the advertisement."""
    cached = OpenAIGateway._ws_cache_from_http_instances(
        [
            {
                "id": "inst-1",
                "port": 3500,
                "capabilities": ["completion", "multimodal"],
                "config": {"alias": "qwen3.6:35b", "backend_type": "sglang"},
            }
        ]
    )

    assert cached[0]["capabilities"] == ["completion", "multimodal"]


def test_an_older_host_leaves_capabilities_unset():
    entry = RegistryEntry.from_ws_instance(
        host_id="host-1",
        host_url="http://test-host:8000",
        host_api_key="k",
        instance={"id": "inst-1", "alias": "qwen3.6:35b", "port": 3500},
    )

    assert entry.capabilities is None
