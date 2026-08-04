"""A start for an instance that is already coming up must not launch a second.

Starting blocks while the server comes up, so a caller that gives up waiting
(a client timeout) and retries would otherwise orphan the first process — it
keeps its port and its share of the GPU with nothing tracking it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from solar_host.models.base import Instance, InstanceStatus
from solar_host.process_manager import ProcessManager


def _instance(status: InstanceStatus) -> Instance:
    return Instance(
        id="inst-1",
        config={
            "backend_type": "llamacpp",
            "model": "/tmp/test.gguf",
            "alias": "test",
        },
        status=status,
    )


def _live_process() -> MagicMock:
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


@pytest.mark.anyio
@pytest.mark.parametrize("status", [InstanceStatus.STARTING, InstanceStatus.RUNNING])
async def test_start_is_a_no_op_while_the_process_is_alive(status):
    manager = ProcessManager()
    manager.processes["inst-1"] = _live_process()

    with patch("solar_host.process_manager.config_manager") as cfg:
        cfg.get_instance.return_value = _instance(status)

        assert await manager._try_start_instance("inst-1", attempt=0) is True

    cfg.update_instance.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [InstanceStatus.STARTING, InstanceStatus.RUNNING])
async def test_a_status_left_behind_by_a_dead_process_does_not_block_a_start(status):
    """The guard must not strand an instance whose process is gone."""
    manager = ProcessManager()
    dead = MagicMock()
    dead.poll.return_value = 1
    manager.processes["inst-1"] = dead
    instance = _instance(status)

    with (
        patch("solar_host.process_manager.config_manager") as cfg,
        patch.object(manager, "_purge_instance_resources"),
        patch.object(manager, "_get_available_port", return_value=9999),
        patch(
            "solar_host.process_manager.get_runner_for_config",
            side_effect=RuntimeError("start attempted"),
        ),
    ):
        cfg.get_instance.return_value = instance

        with pytest.raises(RuntimeError, match="start attempted"):
            await manager._try_start_instance("inst-1", attempt=0)

    assert "inst-1" not in manager.processes
