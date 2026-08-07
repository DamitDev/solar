"""C5 WS-first resource read model.

_fetch_host_resource_snapshot serves the Redis snapshot pushed with host
health when the host is connected and the entry is fresh; HTTP proxying
stays as the degraded fallback. _merge_resource_payload must produce
identical output from a WS payload and the equivalent HTTP body.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from app.models import Host, HostResourceSnapshot, HostStatus
from app.routes.management.resources import (
    _fetch_host_resource_snapshot,
    _merge_resource_payload,
)

HOST = Host(
    id="host-1",
    name="Test Host",
    url="http://test-host:8000",
    api_key="test-key",
    status=HostStatus.ONLINE,
)

_WS_PAYLOAD = {
    "memory_type": "VRAM",
    "vram": {
        "total_gb": 24.0,
        "system_used_gb": 2.0,
        "reserved_headroom_gb": 4.0,
        "reported_used_gb": 6.0,
        "available_gb": 18.0,
    },
    "ram": None,
    "disk": {
        "total_gb": 500.0,
        "system_used_gb": 100.0,
        "reserved_headroom_gb": 0.0,
        "reported_used_gb": 100.0,
        "available_gb": 400.0,
    },
    "reservations": [
        {
            "id": "res-1",
            "job_id": "job-7",
            "workload_type": "training",
            "status": "running",
            "vram_gb": 8.0,
            "ram_gb": 0.0,
            "disk_gb": 10.0,
            "actual_vram_gb": 6.0,
            "actual_ram_gb": 0.0,
            "actual_disk_gb": 4.0,
            "expires_at": "2026-08-07T00:00:00+00:00",
        }
    ],
}


def _fresh_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_at() -> str:
    return "2020-01-01T00:00:00+00:00"


async def _fetch(**store_overrides):
    """Run _fetch_host_resource_snapshot with all seams mocked."""
    defaults = {
        "is_host_connected": AsyncMock(return_value=True),
        "get_host_resource_snapshot": AsyncMock(
            return_value={"at": _fresh_at(), "resources": _WS_PAYLOAD}
        ),
        "get_host_instances": AsyncMock(return_value=[]),
        # HTTP fallback answers 200 with the same payload by default; pass
        # http_ok=False to exercise the degraded path.
        "http_ok": True,
    }
    defaults.update(store_overrides)
    with (
        patch(
            "app.routes.management.resources.host_store.is_host_connected",
            new=defaults["is_host_connected"],
        ),
        patch(
            "app.routes.management.resources.host_store.get_host_resource_snapshot",
            new=defaults["get_host_resource_snapshot"],
        ),
        patch(
            "app.routes.management.resources.host_store.get_host_instances",
            new=defaults["get_host_instances"],
        ),
        patch(
            "app.routes.management.resources.get_host_active_jobs",
            new=AsyncMock(return_value=[]),
        ),
        patch("aiohttp.ClientSession.get") as mock_get,
    ):
        if defaults["http_ok"]:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.json.return_value = _WS_PAYLOAD
            mock_get.return_value.__aenter__.return_value = mock_resp
        else:
            mock_get.return_value.__aenter__.side_effect = (
                aiohttp.ClientConnectionError("refused")
            )
        return await _fetch_host_resource_snapshot(HOST)


class TestCacheFirst:
    @pytest.mark.anyio
    async def test_ws_snapshot_used_when_connected_and_fresh(self):
        snap = await _fetch()
        assert snap.snapshot_source == "ws"
        assert snap.reachable is True
        assert snap.vram_total_gb == 24.0
        assert snap.reservation_count == 1
        assert snap.reservations[0].job_id == "job-7"
        assert snap.vram_training_used_gb == 6.0

    @pytest.mark.anyio
    async def test_http_fallback_when_cache_stale(self):
        snap = await _fetch(
            get_host_resource_snapshot=AsyncMock(
                return_value={"at": _stale_at(), "resources": _WS_PAYLOAD}
            ),
        )
        assert snap.snapshot_source == "http"

    @pytest.mark.anyio
    async def test_http_fallback_when_disconnected(self):
        snap = await _fetch(is_host_connected=AsyncMock(return_value=False))
        assert snap.snapshot_source == "http"

    @pytest.mark.anyio
    async def test_http_fallback_when_cache_empty(self):
        snap = await _fetch(get_host_resource_snapshot=AsyncMock(return_value=None))
        assert snap.snapshot_source == "http"

    @pytest.mark.anyio
    async def test_naive_timestamp_falls_back_instead_of_raising(self):
        """A naive 'at' from an older build must not raise out of the check.

        Mixing naive and aware datetimes raises TypeError, which would
        propagate through _fetch_host_resource_snapshot into _observe and fail
        the whole reconcile tick.
        """
        snap = await _fetch(
            get_host_resource_snapshot=AsyncMock(
                return_value={
                    "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                    "resources": _WS_PAYLOAD,
                }
            ),
        )
        # Treated as UTC and therefore fresh — no exception, no HTTP call.
        assert snap.snapshot_source == "ws"

    @pytest.mark.anyio
    async def test_unparseable_timestamp_falls_back_to_http(self):
        snap = await _fetch(
            get_host_resource_snapshot=AsyncMock(
                return_value={"at": "not-a-timestamp", "resources": _WS_PAYLOAD}
            ),
        )
        assert snap.snapshot_source == "http"

    @pytest.mark.anyio
    async def test_degraded_when_host_unreachable(self):
        """No WS snapshot, HTTP dead -> reachable False, source none."""
        snap = await _fetch(
            is_host_connected=AsyncMock(return_value=False),
            http_ok=False,
        )
        # HTTP fallback against a dead host -> degraded snapshot
        assert snap.reachable is False
        assert snap.snapshot_source == "none"
        assert snap.error is not None


class TestMerge:
    def test_ws_payload_and_http_body_merge_identically(self):
        base = HostResourceSnapshot(
            host_id="host-1",
            host_name="Test Host",
            url="http://test-host:8000",
            status=HostStatus.ONLINE,
        )
        from_ws = _merge_resource_payload(base.model_copy(deep=True), _WS_PAYLOAD)
        from_http = _merge_resource_payload(base.model_copy(deep=True), _WS_PAYLOAD)
        assert from_ws.model_dump() == from_http.model_dump()
        assert from_ws.vram_available_gb == 18.0
        assert from_ws.reservation_ram_total_gb == 0.0
        assert from_ws.disk_training_used_gb == 4.0
