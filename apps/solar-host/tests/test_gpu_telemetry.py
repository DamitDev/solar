"""S-058: per-device GPU telemetry (get_gpu_list / verify_gpu_capacity).

Driven through the GPU_TELEMETRY_OVERRIDE dev hook (L2): the same hook the
integration suite uses to fake a real NVIDIA host, so the unit tests and
the subprocess-level suite exercise identical code paths.
"""

from __future__ import annotations

import json

import pytest

from solar_host.config import settings
from solar_host.memory_monitor import (
    detect_gpu_type,
    get_gpu_list,
    get_memory_info,
    verify_gpu_capacity,
)

_OVERRIDE = [
    {"index": 0, "name": "RTX 4090", "total_gb": 24.0, "used_gb": 20.0},
    {"index": 1, "name": "RTX 4090", "total_gb": 24.0, "used_gb": 12.0},
    {"index": 2, "name": "RTX 4090", "total_gb": 24.0, "used_gb": 0.0},
]


@pytest.fixture
def three_gpus(monkeypatch):
    """A 3-device fake NVIDIA host; only device 2 is genuinely free."""
    monkeypatch.setattr(
        settings,
        "gpu_telemetry_override",
        json.dumps(_OVERRIDE),
    )
    # The module-level 5 s caches are shared across tests — clear them so
    # this fixture's devices are what every call below sees.
    monkeypatch.setattr("solar_host.memory_monitor._gpu_list_cache", None)
    monkeypatch.setattr("solar_host.memory_monitor._gpu_list_timestamp", 0.0)
    monkeypatch.setattr("solar_host.memory_monitor._memory_cache", None)
    monkeypatch.setattr("solar_host.memory_monitor._cache_timestamp", 0.0)


class TestGetGpuList:
    def test_shape_and_available_gb(self, three_gpus) -> None:
        gpus = get_gpu_list()
        assert len(gpus) == 3
        first = gpus[0]
        assert first.index == 0
        assert first.name == "RTX 4090"
        assert first.total_gb == 24.0
        assert first.used_gb == 20.0
        assert first.available_gb == 4.0  # total - used
        # L1: exactly the five spec fields — no uuid.
        fields = set(first.model_dump().keys())
        assert fields == {"index", "name", "total_gb", "used_gb", "available_gb"}

    def test_empty_list_without_nvml(self, _hermetic_settings, monkeypatch) -> None:
        """D6: no telemetry (no NVML, no override) → empty list.

        ``get_gpu_devices`` is stubbed rather than relying on pynvml being
        absent, so the test is hermetic on a real GPU host too. The cache
        is cleared like the sibling tests, so the assertion does not depend
        on run order.
        """
        monkeypatch.setattr("solar_host.memory_monitor._gpu_list_cache", None)
        monkeypatch.setattr("solar_host.memory_monitor.get_gpu_devices", list)
        assert get_gpu_list() == []

    def test_bad_override_json_is_ignored(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "gpu_telemetry_override", "not json at all")
        monkeypatch.setattr("solar_host.memory_monitor._gpu_list_cache", None)
        # With the override ignored, telemetry falls back to NVML — stub it
        # so the test does not depend on pynvml being uninstalled.
        monkeypatch.setattr("solar_host.memory_monitor.get_gpu_devices", list)
        assert get_gpu_list() == []

    def test_non_list_override_is_ignored(self, monkeypatch) -> None:
        """A JSON scalar override must not raise TypeError (review fix)."""
        monkeypatch.setattr(settings, "gpu_telemetry_override", "5")
        monkeypatch.setattr("solar_host.memory_monitor._gpu_list_cache", None)
        monkeypatch.setattr("solar_host.memory_monitor.get_gpu_devices", list)
        assert get_gpu_list() == []


class TestOverrideCoherence:
    """L2: a fake host is indistinguishable from a real one end to end."""

    def test_aggregate_memory_sums_fake_devices(self, three_gpus) -> None:
        mem = get_memory_info()
        assert mem is not None
        assert mem["memory_type"] == "VRAM"
        assert mem["total_gb"] == 72.0
        assert mem["used_gb"] == 32.0
        assert mem["available_gb"] == 40.0

    def test_gpu_type_reports_nvidia(self, three_gpus, monkeypatch) -> None:
        monkeypatch.setattr("solar_host.memory_monitor._gpu_type_cache", None)
        assert detect_gpu_type() == "nvidia_cuda"


class TestVerifyGpuCapacity:
    def test_returns_first_failing_device(self, three_gpus) -> None:
        # Device 2 fits anything up to 24 GB; device 0 has only 4 GB left,
        # so a 5 GB request fails there first — in gpu_ids order.
        failure = verify_gpu_capacity([2, 0], 5.0)
        assert failure == (0, 4.0)

    def test_missing_device_counts_as_no_capacity(self, three_gpus) -> None:
        failure = verify_gpu_capacity([3], 1.0)
        assert failure == (3, 0.0)

    def test_fits_returns_none(self, three_gpus) -> None:
        assert verify_gpu_capacity([2], 24.0) is None
        assert verify_gpu_capacity([0], 4.0) is None  # exactly fits

    def test_no_gpus_or_footprint_is_a_noop(self, three_gpus) -> None:
        assert verify_gpu_capacity([], 10.0) is None
        assert verify_gpu_capacity([0], 0.0) is None

    def test_empty_ledger_degrades_to_no_check(
        self, _hermetic_settings, monkeypatch
    ) -> None:
        """D6: NVML down (empty ledger) never blocks a GPU-assigned start.

        The spawn-time re-verify must degrade to no check when the device
        ledger is empty — a driver reload or a manual restart of a stopped
        instance whose gpu_ids persisted must not fail the spawn.
        """
        monkeypatch.setattr("solar_host.memory_monitor.get_gpu_devices", list)
        assert verify_gpu_capacity([0], 10.0) is None
        assert verify_gpu_capacity([0, 1], 10.0) is None
