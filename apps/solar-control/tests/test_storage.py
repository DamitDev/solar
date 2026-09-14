"""Tests for the /api/storage host storage management routes."""

import json
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from app.models import Host, HostStatus
from app.routes.management.storage import (
    DeleteItem,
    DeleteRequest,
    _build_host_storage,
    _derive_origin,
    _harbor_ref,
    bulk_delete_models,
    delete_host_model,
    get_host_storage,
    list_host_storage,
)


@pytest.fixture
def mock_host():
    return Host(
        id="host-1",
        name="Test Host",
        url="http://test-host:8000",
        api_key="test-key",
        status=HostStatus.ONLINE,
        disk_total_gb=100.0,
        disk_used_gb=40.0,
        disk_available_gb=60.0,
    )


@pytest.fixture
def mock_host_2():
    return Host(
        id="host-2",
        name="Test Host 2",
        url="http://test-host-2:8000",
        api_key="test-key-2",
        status=HostStatus.ONLINE,
    )


def _manifest_entry(**overrides):
    entry = {
        "name": "repo--iris-osl--v3",
        "path": "/opt/solar/models/repo--iris-osl--v3",
        "size_bytes": 1000,
        "source_uri": "repo://iris-osl:v3",
    }
    entry.update(overrides)
    return entry


def _instance(**overrides):
    inst = {
        "id": "inst-1",
        "alias": "iris-osl",
        "status": "running",
        "model_source": "repo://iris-osl:v3",
    }
    inst.update(overrides)
    return inst


class _Ctx:
    """Async context manager wrapper for mocked aiohttp responses."""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


def _delete_response(status: int, detail: str) -> _Ctx:
    resp = AsyncMock()
    resp.status = status
    resp.text.return_value = json.dumps({"detail": detail, "name": "x"})
    return _Ctx(resp)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_aggregation_populates_in_use_by(mock_host):
    """A running instance whose model_source matches is joined into in_use_by."""
    with (
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            new=AsyncMock(return_value=[mock_host]),
        ),
        patch("aiohttp.ClientSession.get") as mock_get,
        patch(
            "app.redis_state.host_store.get_host_instances",
            new=AsyncMock(
                return_value=[
                    _instance(),
                    _instance(id="inst-2", alias="other", status="stopped"),
                ]
            ),
        ),
    ):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = [
            _manifest_entry(metadata={"harbor_ref": "imgrepo.damit.hu/x:y"})
        ]
        mock_get.return_value.__aenter__.return_value = mock_resp

        response = await list_host_storage()

        assert len(response.hosts) == 1
        host = response.hosts[0]
        assert host.reachable is True
        assert host.host_name == "Test Host"
        assert host.disk_total_gb == 100.0
        assert host.total_size_bytes == 1000
        assert response.unreachable_hosts == []

        model = host.models[0]
        assert model.slug == "repo--iris-osl--v3"
        assert model.origin == "repository"
        assert model.harbor_ref == "imgrepo.damit.hu/x:y"
        assert len(model.in_use_by) == 1
        assert model.in_use_by[0].instance_id == "inst-1"
        assert model.in_use_by[0].alias == "iris-osl"
        assert model.in_use_by[0].status == "running"


@pytest.mark.anyio
async def test_unreachable_host_is_listed_not_502(mock_host):
    offline = Host(
        id="host-2",
        name="Offline Host",
        url="http://offline:8000",
        api_key="k",
        status=HostStatus.OFFLINE,
    )
    with (
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            new=AsyncMock(return_value=[mock_host, offline]),
        ),
        patch("aiohttp.ClientSession.get") as mock_get,
        patch(
            "app.redis_state.host_store.get_host_instances",
            new=AsyncMock(return_value=[]),
        ),
    ):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = []
        mock_get.return_value.__aenter__.return_value = mock_resp

        response = await list_host_storage()

        assert len(response.hosts) == 2
        by_name = {h.host_name: h for h in response.hosts}
        assert by_name["Test Host"].reachable is True
        assert by_name["Offline Host"].reachable is False
        assert by_name["Offline Host"].error is not None
        assert response.unreachable_hosts == ["Offline Host"]


@pytest.mark.anyio
async def test_connection_error_marks_host_unreachable(mock_host):
    with (
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            new=AsyncMock(return_value=[mock_host]),
        ),
        patch("aiohttp.ClientSession.get") as mock_get,
    ):
        mock_get.side_effect = aiohttp.ClientConnectionError("Refused")

        response = await list_host_storage()

        host = response.hosts[0]
        assert host.reachable is False
        assert host.error is not None
        assert response.unreachable_hosts == ["Test Host"]


@pytest.mark.anyio
async def test_single_host_endpoint_404_for_unknown_host():
    with patch("app.database.hosts.host_db.get_host", new=AsyncMock(return_value=None)):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await get_host_storage("missing")
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Origin derivation + harbor_ref
# ---------------------------------------------------------------------------


class TestOriginDerivation:
    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            ("repo://iris-osl:v3", "repository"),
            ("huggingface://org/model", "huggingface"),
            ("local://models/x.gguf", "local"),
            (None, "unknown"),
            ("weird://thing", "unknown"),
        ],
    )
    def test_derive_origin(self, uri, expected):
        assert _derive_origin(uri) == expected


class TestHarborRef:
    def test_surfaces_from_metadata(self):
        assert _harbor_ref({"harbor_ref": "imgrepo.damit.hu/x:y"}) == (
            "imgrepo.damit.hu/x:y"
        )

    def test_none_when_absent(self):
        assert _harbor_ref(None) is None
        assert _harbor_ref({"other": 1}) is None
        assert _harbor_ref({"harbor_ref": ""}) is None


# ---------------------------------------------------------------------------
# Single delete
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (404, "Model not found"),
        (409, "Model is in use by instance inst-1. Stop the instance first."),
    ],
)
async def test_single_delete_propagates_404_and_409(mock_host, status, detail):
    with (
        patch(
            "app.database.hosts.host_db.get_host",
            new=AsyncMock(return_value=mock_host),
        ),
        patch("aiohttp.ClientSession.delete") as mock_delete,
    ):
        mock_delete.return_value = _delete_response(status, detail)

        result = await delete_host_model("host-1", "repo--iris-osl--v3")

        assert result.status_code == status
        assert json.loads(bytes(result.body).decode())["detail"] == detail


@pytest.mark.anyio
async def test_single_delete_success(mock_host):
    with (
        patch(
            "app.database.hosts.host_db.get_host",
            new=AsyncMock(return_value=mock_host),
        ),
        patch("aiohttp.ClientSession.delete") as mock_delete,
    ):
        mock_delete.return_value = _delete_response(200, "Model deleted")

        result = await delete_host_model("host-1", "repo--iris-osl--v3")

        assert result.status_code == 200
        assert json.loads(bytes(result.body).decode())["detail"] == "Model deleted"


@pytest.mark.anyio
async def test_single_delete_unknown_host():
    from fastapi import HTTPException

    with patch("app.database.hosts.host_db.get_host", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await delete_host_model("missing", "x")
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bulk_delete_mixed_outcomes_never_raises(mock_host, mock_host_2):
    """200 / 409 / 404 / connection error collapse into one result list."""
    manifest = [
        {"name": "x", "size_bytes": 100, "path": "/p/x", "source_uri": "repo://a:x"},
        {"name": "y", "size_bytes": 200, "path": "/p/y", "source_uri": "repo://a:y"},
    ]

    def _delete_side_effect(url, *args, **kwargs):
        if "host-2" in str(url):
            raise aiohttp.ClientConnectionError("Refused")
        if url.endswith("/x"):
            return _delete_response(200, "Model deleted")
        if url.endswith("/y"):
            return _delete_response(409, "in use")
        return _delete_response(404, "not found")

    with (
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            new=AsyncMock(return_value=[mock_host, mock_host_2]),
        ),
        patch("aiohttp.ClientSession.get") as mock_get,
        patch("aiohttp.ClientSession.delete", side_effect=_delete_side_effect),
    ):
        # Size pre-fetch: both hosts answer with the same manifest.
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = manifest
        mock_get.return_value.__aenter__.return_value = mock_resp

        results = await bulk_delete_models(
            DeleteRequest(
                items=[
                    DeleteItem(host_id="host-1", slug="x"),
                    DeleteItem(host_id="host-1", slug="y"),
                    DeleteItem(host_id="host-1", slug="z"),
                    DeleteItem(host_id="host-2", slug="w"),
                ]
            )
        )

        assert len(results) == 4
        by_slug = {r.slug: r for r in results}
        assert by_slug["x"].status == "deleted"
        assert by_slug["x"].freed_bytes == 100
        assert by_slug["y"].status == "in_use"
        assert by_slug["z"].status == "not_found"
        assert by_slug["w"].status == "unreachable"
        # freed_bytes sums only over deleted items
        assert sum(r.freed_bytes for r in results) == 100


@pytest.mark.anyio
async def test_bulk_delete_unknown_host_is_not_found(mock_host):
    with (
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            new=AsyncMock(return_value=[mock_host]),
        ),
        patch("aiohttp.ClientSession.get") as mock_get,
    ):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = []
        mock_get.return_value.__aenter__.return_value = mock_resp

        results = await bulk_delete_models(
            DeleteRequest(items=[DeleteItem(host_id="missing", slug="x")])
        )

        assert results[0].status == "not_found"
        assert results[0].detail == "Host not found"
        assert results[0].freed_bytes == 0


# ---------------------------------------------------------------------------
# _build_host_storage direct
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_host_storage_offline_fast_path():
    offline = Host(
        id="host-9",
        name="Down",
        url="http://down:8000",
        api_key="k",
        status=HostStatus.ERROR,
    )
    with patch("aiohttp.ClientSession.get") as mock_get:
        result = await _build_host_storage(offline)
        mock_get.assert_not_called()
    assert result.reachable is False
    assert result.error is not None
