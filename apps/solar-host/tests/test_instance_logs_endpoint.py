"""C2 GET /instances/{id}/logs: in-memory buffer first, then the on-disk
file fallback (which keeps working after the instance record is gone), and
404 only when neither exists."""

import sys
import time
from collections import deque

import pytest

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.config import config_manager, settings
from solar_host.models import InstanceStatus, LogMessage
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.process_manager import ProcessManager


class _ScriptRunner(LlamaCppRunner):
    def __init__(self, script: str, ready_marker: str = "READY_MARKER"):
        super().__init__()
        self._script = script
        self._ready_marker = ready_marker

    def build_command(self, instance) -> list[str]:
        return [sys.executable, "-u", "-c", self._script]

    def is_ready_line(self, line: str) -> bool:
        return self._ready_marker in line


def _make_instance(instance_id: str = "inst-1", status=InstanceStatus.STOPPED):
    from solar_host.models.base import Instance

    instance = Instance(
        id=instance_id,
        config=LlamaCppConfig(model="/tmp/test.gguf", alias="test"),
        status=status,
    )
    config_manager.add_instance(instance)
    return instance


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr("solar_host.config.settings.log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 5.0)
    monkeypatch.setattr("solar_host.config.settings.log_buffer_size", 1000)
    config_manager.config_file = tmp_path / "config.json"
    config_manager.instances = {}
    return tmp_path


def _buffer_log(manager: ProcessManager, instance_id: str, lines: list[str]):
    manager.log_buffers[instance_id] = deque(
        [LogMessage(seq=i, timestamp="t", line=line) for i, line in enumerate(lines)],
        maxlen=settings.log_buffer_size,
    )
    manager.log_sequences[instance_id] = len(lines)


@pytest.mark.anyio
async def test_buffer_returned_when_present(_isolated_env, monkeypatch):
    manager = ProcessManager()
    _make_instance("inst-1")
    _buffer_log(manager, "inst-1", ["alpha", "beta"])
    monkeypatch.setattr("solar_host.routes.instances.process_manager", manager)
    from solar_host.routes.instances import get_instance_logs

    logs = await get_instance_logs("inst-1")
    assert [m.line for m in logs] == ["alpha", "beta"]


@pytest.mark.anyio
async def test_file_fallback_when_buffer_empty(_isolated_env, monkeypatch):
    manager = ProcessManager()
    _make_instance("inst-1")
    path = manager.log_dir / "alias_inst-1_123.log"
    path.write_text("file line one\nfile line two\n")
    monkeypatch.setattr("solar_host.routes.instances.process_manager", manager)
    from solar_host.routes.instances import get_instance_logs

    logs = await get_instance_logs("inst-1")
    assert [m.line for m in logs] == ["file line one", "file line two"]
    # seq synthesized from the line index, timestamp from the file mtime
    assert logs[0].seq == 0
    assert logs[0].timestamp


@pytest.mark.anyio
async def test_file_fallback_after_instance_deleted(_isolated_env, monkeypatch):
    """Post-mortem reads work even when the instance record is gone."""
    manager = ProcessManager()
    _make_instance("inst-1")
    manager.delete_instance("inst-1")  # purges buffers, removes the record
    path = manager.log_dir / "alias_inst-1_123.log"
    path.write_text("last words\n")
    monkeypatch.setattr("solar_host.routes.instances.process_manager", manager)
    from solar_host.routes.instances import get_instance_logs

    logs = await get_instance_logs("inst-1")
    assert [m.line for m in logs] == ["last words"]


@pytest.mark.anyio
async def test_404_only_when_neither_exists(_isolated_env, monkeypatch):
    from fastapi import HTTPException

    from solar_host.routes.instances import get_instance_logs

    manager = ProcessManager()
    # No instance record, no buffer, no log file -> 404.
    monkeypatch.setattr("solar_host.routes.instances.process_manager", manager)
    with pytest.raises(HTTPException) as excinfo:
        await get_instance_logs("inst-1")
    assert excinfo.value.status_code == 404


@pytest.mark.anyio
async def test_file_tail_bounded_by_log_buffer_size(_isolated_env, monkeypatch):
    monkeypatch.setattr("solar_host.config.settings.log_buffer_size", 2)
    manager = ProcessManager()
    _make_instance("inst-1")
    path = manager.log_dir / "alias_inst-1_123.log"
    path.write_text("one\ntwo\nthree\nfour\n")
    monkeypatch.setattr("solar_host.routes.instances.process_manager", manager)
    from solar_host.routes.instances import get_instance_logs

    logs = await get_instance_logs("inst-1")
    assert [m.line for m in logs] == ["three", "four"]
