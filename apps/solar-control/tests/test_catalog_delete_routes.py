"""Tests for the S-048 catalog delete routes (under /api/catalog).

The CatalogDeleteService is faked at the builder; version-list enrichment
runs against patched proxy/availability sources.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routes.management.catalog import router

REPO_VERSIONS = {
    "versions": [
        {
            "version": "v2",
            "harbor_ref": "imgrepo.damit.hu/supernova/mymodel:v2",
            "created_at": "2026-08-01T00:00:00Z",
            "size_bytes": 2048,
            "checksum": "sha256:b",
        },
        {
            "version": "v1",
            "harbor_ref": "imgrepo.damit.hu/supernova/mymodel:v1",
            "created_at": "2026-07-01T00:00:00Z",
            "size_bytes": 1024,
            "checksum": "sha256:a",
        },
    ]
}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def _fake_service(*, delete_version=None, delete_artifact=None):
    svc = MagicMock()
    svc.delete_version = delete_version or AsyncMock()
    svc.delete_artifact = delete_artifact or AsyncMock(
        return_value={
            "name": "mymodel",
            "deleted": ["v2", "v1"],
            "failed": [],
            "artifact_removed": True,
            "harbor_repository_removed": True,
        }
    )
    return svc


def _cm_response(status: int, payload):
    resp = AsyncMock()
    resp.status = status
    resp.json.return_value = payload
    cm = AsyncMock()
    cm.__aenter__.return_value = resp
    return cm


@pytest.fixture
def catalog_settings():
    with patch("app.routes.management.catalog.settings") as mock_settings:
        mock_settings.data_repository_url = "http://data-repo:8000"
        mock_settings.data_repository_api_key = ""
        mock_settings.data_repository_timeout_s = 10.0
        yield mock_settings


# ---------------------------------------------------------------------------
# DELETE /api/catalog/models/{name}/versions/{version}
# ---------------------------------------------------------------------------


def test_delete_version_returns_204(catalog_settings):
    svc = _fake_service(delete_version=AsyncMock())
    with patch(
        "app.services.catalog_delete.build_catalog_delete_service",
        return_value=svc,
    ):
        resp = _client().delete("/catalog/models/mymodel/versions/v1")

    assert resp.status_code == 204
    assert resp.content == b""
    svc.delete_version.assert_awaited_once_with("mymodel", "v1")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (HTTPException(409, "served by running instances"), 409),
        (HTTPException(404, "not found"), 404),
        (HTTPException(422, "reserved alias"), 422),
        (HTTPException(502, "harbor down"), 502),
    ],
)
def test_delete_version_propagates_service_errors(catalog_settings, exc, expected):
    svc = _fake_service(delete_version=AsyncMock(side_effect=exc))
    with patch(
        "app.services.catalog_delete.build_catalog_delete_service",
        return_value=svc,
    ):
        resp = _client().delete("/catalog/models/mymodel/versions/v1")

    assert resp.status_code == expected


# ---------------------------------------------------------------------------
# DELETE /api/catalog/models/{name}
# ---------------------------------------------------------------------------


def test_delete_artifact_returns_result(catalog_settings):
    svc = _fake_service()
    with patch(
        "app.services.catalog_delete.build_catalog_delete_service",
        return_value=svc,
    ):
        resp = _client().delete("/catalog/models/mymodel")

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "mymodel"
    assert body["deleted"] == ["v2", "v1"]
    assert body["failed"] == []
    assert body["artifact_removed"] is True
    assert body["harbor_repository_removed"] is True
    svc.delete_artifact.assert_awaited_once_with("mymodel")


def test_delete_artifact_propagates_service_errors(catalog_settings):
    svc = _fake_service(
        delete_artifact=AsyncMock(
            side_effect=HTTPException(409, "served by running instances")
        )
    )
    with patch(
        "app.services.catalog_delete.build_catalog_delete_service",
        return_value=svc,
    ):
        resp = _client().delete("/catalog/models/mymodel")

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET /api/catalog/models/{name}/versions
# ---------------------------------------------------------------------------


async def _mock_versions_route(
    *,
    listing=REPO_VERSIONS,
    deployed_by_version=None,
    running=None,
):
    """Patch proxy + enrichment sources and return the route's response."""
    with (
        patch(
            "aiohttp.ClientSession.request",
            return_value=_cm_response(200, listing),
        ) as mock_request,
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            return_value=[],
        ),
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=running or [],
        ),
    ):
        from app.routes.management.catalog import get_catalog_model_versions

        return await get_catalog_model_versions("mymodel"), mock_request


@pytest.mark.anyio
async def test_versions_proxies_and_enriches(catalog_settings):
    resp, mock_request = await _mock_versions_route()

    call = mock_request.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == "http://data-repo:8000/api/models/mymodel/versions"

    assert [v.version for v in resp.versions] == ["v2", "v1"]
    assert resp.versions[0].harbor_ref == "imgrepo.damit.hu/supernova/mymodel:v2"
    assert resp.versions[0].created_at == datetime(2026, 8, 1, tzinfo=UTC)
    assert resp.versions[0].solar.running_instances == 0
    assert resp.versions[0].solar.deployed_hosts == []


@pytest.mark.anyio
async def test_versions_reports_running_instances_per_version(catalog_settings):
    from app.services.catalog_delete import RunningInstance

    running = [
        RunningInstance(
            host_id="h1",
            host_name="host-1",
            instance_id="i1",
            source="repo://mymodel:v2",
            name="mymodel",
            version="v2",
        ),
        RunningInstance(
            host_id="h2",
            host_name="host-2",
            instance_id="i2",
            source="repo://mymodel:v1",
            name="mymodel",
            version="v1",
        ),
    ]
    resp, _ = await _mock_versions_route(running=running)

    assert resp.versions[0].solar.running_instances == 1
    assert resp.versions[1].solar.running_instances == 1


@pytest.mark.anyio
async def test_versions_attributes_latest_to_newest(catalog_settings):
    from app.services.catalog_delete import RunningInstance

    running = [
        RunningInstance(
            host_id="h1",
            host_name="host-1",
            instance_id="i9",
            source="repo://mymodel:latest",
            name="mymodel",
            version="latest",
        )
    ]
    resp, _ = await _mock_versions_route(running=running)

    # latest serves the newest version (v2), not v1.
    assert resp.versions[0].solar.running_instances == 1
    assert resp.versions[1].solar.running_instances == 0


@pytest.mark.anyio
async def test_versions_reports_deployed_hosts_per_version(catalog_settings):
    from app.models import Host, HostStatus

    host = Host(
        id="h1",
        name="host-1",
        url="http://h1:8000",
        api_key="k",
        status=HostStatus.ONLINE,
    )
    listing = dict(REPO_VERSIONS)
    with (
        patch(
            "aiohttp.ClientSession.request",
            return_value=_cm_response(200, listing),
        ),
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            return_value=[host],
        ),
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=[],
        ),
        patch(
            "aiohttp.ClientSession.get",
            return_value=_cm_response(
                200,
                [
                    {
                        "name": "repo--mymodel--v2",
                        "model_name": "mymodel",
                        "version": "v2",
                        "size_bytes": 2048,
                        "path": "/models/repo--mymodel--v2",
                    }
                ],
            ),
        ),
    ):
        from app.routes.management.catalog import get_catalog_model_versions

        resp = await get_catalog_model_versions("mymodel")

    assert resp.versions[0].solar.deployed_hosts[0].host_name == "host-1"
    assert resp.versions[1].solar.deployed_hosts == []


@pytest.mark.anyio
async def test_versions_propagates_404(catalog_settings):
    with (
        patch(
            "aiohttp.ClientSession.request",
            return_value=_cm_response(404, {"detail": "missing"}),
        ),
        patch("app.database.hosts.host_db.get_all_hosts", return_value=[]),
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=[],
        ),
    ):
        from app.routes.management.catalog import get_catalog_model_versions

        with pytest.raises(HTTPException) as exc:
            await get_catalog_model_versions("ghost")
    assert exc.value.status_code == 404


@pytest.mark.anyio
async def test_versions_upstream_error_becomes_502(catalog_settings):
    with (
        patch(
            "aiohttp.ClientSession.request",
            return_value=_cm_response(500, {"detail": "boom"}),
        ),
        patch("app.database.hosts.host_db.get_all_hosts", return_value=[]),
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=[],
        ),
    ):
        from app.routes.management.catalog import get_catalog_model_versions

        with pytest.raises(HTTPException) as exc:
            await get_catalog_model_versions("mymodel")
    assert exc.value.status_code == 502
    assert "boom" in str(exc.value.detail)


@pytest.mark.anyio
async def test_versions_unreachable_becomes_502(catalog_settings):
    import aiohttp

    with (
        patch(
            "aiohttp.ClientSession.request",
            side_effect=aiohttp.ClientConnectionError("Refused"),
        ),
        patch("app.database.hosts.host_db.get_all_hosts", return_value=[]),
        patch(
            "app.services.catalog_delete.collect_running_instances",
            return_value=[],
        ),
    ):
        from app.routes.management.catalog import get_catalog_model_versions

        with pytest.raises(HTTPException) as exc:
            await get_catalog_model_versions("mymodel")
    assert exc.value.status_code == 502
    assert "unreachable" in str(exc.value.detail)
