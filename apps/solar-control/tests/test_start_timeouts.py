"""Start/restart proxy and reconciler start calls must use the configured
host start timeout.

A blocking instance start answers only once the backend reports readiness
(log-gated), which can take minutes for a cold model load — every hop of
the call chain must fit inside ``settings.host_start_timeout_s``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models import Host, HostStatus


@pytest.fixture
def mock_host():
    return Host(
        id="host-1",
        name="Test Host",
        url="http://test-host:8000",
        api_key="test-key",
        status=HostStatus.ONLINE,
    )


@pytest.mark.anyio
async def test_start_route_proxies_with_host_start_timeout():
    from app.routes.management.hosts import start_instance

    with patch(
        "app.routes.management.hosts._proxy_instance_action",
        new=AsyncMock(return_value={}),
    ) as mock_proxy:
        await start_instance("host-1", "inst-1")
        mock_proxy.assert_awaited_once_with(
            "host-1", "inst-1", "start", timeout=settings.host_start_timeout_s
        )


@pytest.mark.anyio
async def test_restart_route_proxies_with_host_start_timeout():
    from app.routes.management.hosts import restart_instance

    with patch(
        "app.routes.management.hosts._proxy_instance_action",
        new=AsyncMock(return_value={}),
    ) as mock_proxy:
        await restart_instance("host-1", "inst-1")
        mock_proxy.assert_awaited_once_with(
            "host-1", "inst-1", "restart", timeout=settings.host_start_timeout_s
        )


@pytest.mark.anyio
async def test_proxy_instance_action_passes_timeout_to_aiohttp(mock_host):
    from app.routes.management.hosts import _proxy_instance_action

    with (
        patch(
            "app.database.hosts.host_db.get_host", new=AsyncMock(return_value=mock_host)
        ),
        patch("aiohttp.ClientSession.post") as mock_post,
    ):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"status": "running"}
        mock_post.return_value.__aenter__.return_value = mock_resp

        await _proxy_instance_action(
            "host-1", "inst-1", "start", timeout=settings.host_start_timeout_s
        )
        kwargs = mock_post.call_args.kwargs
        assert kwargs["timeout"].total == settings.host_start_timeout_s


@pytest.mark.anyio
async def test_reconciler_start_uses_host_start_timeout(mock_host, monkeypatch):
    from app.services.reconciliation import reconciler

    monkeypatch.setattr("app.config.settings.host_start_timeout_s", 1234.5)

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_post.return_value.__aenter__.return_value = mock_resp

        await reconciler._start_instance(mock_host, "inst-1")
        kwargs = mock_post.call_args.kwargs
        assert kwargs["timeout"].total == 1234.5
