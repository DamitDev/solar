"""S-058: spawn-time GPU re-verification in the process manager.

When a chosen device went busy after placement (TOCTOU window, or a foreign
process that bypassed the ledgers), ``_try_start_instance`` must fail fast
naming the device and never Popen the backend.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from solar_host.config import config_manager
from solar_host.models import InstanceStatus
from solar_host.process_manager import process_manager


def _make_gpu_instance(gpu_ids: list[int] | None, vram_gb: float | None) -> str:
    """Register a llama.cpp instance carrying a device assignment."""
    instance = process_manager.create_instance(
        {
            "backend_type": "llamacpp",
            "alias": "gpu-test",
            "model": "/models/test.gguf",
            "model_source": "local://models/test.gguf",
        },
        gpu_ids=gpu_ids,
        vram_gb=vram_gb,
    )
    return instance.id


class TestSpawnVerify:
    @pytest.mark.anyio
    async def test_fails_fast_naming_the_device_and_never_spawns(
        self, monkeypatch
    ) -> None:
        instance_id = _make_gpu_instance(gpu_ids=[2], vram_gb=10.0)
        # The device went busy between placement and launch.
        monkeypatch.setattr(
            "solar_host.process_manager.verify_gpu_capacity",
            lambda gpu_ids, required: (2, 3.5),
        )
        with patch("solar_host.process_manager.subprocess.Popen") as mock_popen:
            # attempt 0 + retry budget 2 → the failure signals a retry (None).
            result = await process_manager._try_start_instance(instance_id, attempt=0)

        assert result is None  # retry signal — the existing backoff re-places
        mock_popen.assert_not_called()

        instance = config_manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == InstanceStatus.FAILED
        assert "GPU 2" in (instance.error_message or "")
        assert "3.5 GB free" in (instance.error_message or "")
        assert "10.0 GB" in (instance.error_message or "")
        # No spawned process was tracked.
        assert instance_id not in process_manager.processes

    @pytest.mark.anyio
    async def test_no_assignment_skips_the_check(self, monkeypatch) -> None:
        """An instance without gpu_ids gets a no-op verification (D6)."""
        instance_id = _make_gpu_instance(gpu_ids=None, vram_gb=None)
        verify = Mock(return_value=None)
        monkeypatch.setattr("solar_host.process_manager.verify_gpu_capacity", verify)
        with patch(
            "solar_host.process_manager.subprocess.Popen",
            side_effect=OSError("spawn failed anyway"),
        ):
            await process_manager._try_start_instance(instance_id, attempt=0)

        # The guard lives inside verify_gpu_capacity: the call happens, but
        # with an empty assignment it must never consult the device ledger.
        verify.assert_called_once_with([], 0.0)
        instance = config_manager.get_instance(instance_id)
        assert instance is not None
        assert instance.status == InstanceStatus.FAILED
