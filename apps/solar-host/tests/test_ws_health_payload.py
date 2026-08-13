"""C5 host health push: send_health carries the FULL resource snapshot
(byte-identical to GET /resources) alongside the legacy summary keys, so
control can serve cache-first from the WS read model."""

from unittest.mock import AsyncMock

import pytest

from solar_host.ws_client import SolarControlClient


class _Dim:
    def __init__(
        self,
        total_gb,
        system_used_gb,
        reserved_headroom_gb,
        reported_used_gb,
        available_gb,
    ):
        self.total_gb = total_gb
        self.system_used_gb = system_used_gb
        self.reserved_headroom_gb = reserved_headroom_gb
        self.reported_used_gb = reported_used_gb
        self.available_gb = available_gb


class _Snap:
    """ResourceSnapshot stand-in with a fixed model_dump output."""

    memory_type = "VRAM"
    vram = _Dim(24.0, 2.0, 4.0, 6.0, 18.0)
    ram = _Dim(128.0, 20.0, 8.0, 28.0, 100.0)
    disk = _Dim(500.0, 100.0, 0.0, 100.0, 400.0)

    def __init__(self):
        self.reservations = [
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
        ]

    def model_dump(self, mode):
        return {
            "memory_type": self.memory_type,
            "vram": {
                "total_gb": self.vram.total_gb,
                "system_used_gb": self.vram.system_used_gb,
                "reserved_headroom_gb": self.vram.reserved_headroom_gb,
                "reported_used_gb": self.vram.reported_used_gb,
                "available_gb": self.vram.available_gb,
            },
            "ram": {
                "total_gb": self.ram.total_gb,
                "system_used_gb": self.ram.system_used_gb,
                "reserved_headroom_gb": self.ram.reserved_headroom_gb,
                "reported_used_gb": self.ram.reported_used_gb,
                "available_gb": self.ram.available_gb,
            },
            "disk": {
                "total_gb": self.disk.total_gb,
                "system_used_gb": self.disk.system_used_gb,
                "reserved_headroom_gb": self.disk.reserved_headroom_gb,
                "reported_used_gb": self.disk.reported_used_gb,
                "available_gb": self.disk.available_gb,
            },
            "reservations": self.reservations,
        }


class _ResourceManager:
    def snapshot(self) -> _Snap:
        return _Snap()


@pytest.mark.anyio
async def test_send_health_carries_full_resource_snapshot(monkeypatch):
    client = SolarControlClient(
        control_url="ws://127.0.0.1:1/ws/host-channel",
        api_key="test-key",
        host_name="test-host",
    )
    client._connected = True
    client._sio = AsyncMock()

    # Keep memory/gpu/disk probing deterministic and fast (send_health
    # imports these from solar_host.memory_monitor at call time).
    monkeypatch.setattr(
        "solar_host.memory_monitor.get_memory_info", lambda: {"total_gb": 128.0}
    )
    monkeypatch.setattr("solar_host.memory_monitor.detect_gpu_type", lambda: "cpu")
    monkeypatch.setattr("solar_host.memory_monitor.get_disk_info", lambda path: None)
    monkeypatch.setattr("solar_host.config.config_manager.roles", ["inference"])

    snap = _Snap()
    await client.send_health(
        memory={"total_gb": 128.0}, resource_manager=_ResourceManager()
    )

    emit_args = client._sio.emit.call_args
    event, payload = emit_args.args[0], emit_args.args[1]
    assert event == "host_health"
    data = payload["data"]

    # Full snapshot — byte-identical to GET /resources, including the
    # per-reservation list and memory_type.
    assert data["resources"] == snap.model_dump(mode="json")
    assert data["resources"]["memory_type"] == "VRAM"
    assert data["resources"]["reservations"][0]["job_id"] == "job-7"

    # Legacy summary keys still present for older control versions.
    assert data["reservations"]["active_count"] == 1
    assert data["reservations"]["vram"]["available_gb"] == 18.0
    assert data["gpu_type"] == "cpu"
