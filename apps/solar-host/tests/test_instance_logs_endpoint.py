"""C2 GET /instances/{id}/logs: in-memory buffer first, then the on-disk
file fallback (which keeps working after the instance record is gone), and
404 only when neither exists."""

import sys
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


class TestBoundedTailing:
    """H4: the fallback must not read a whole file to return its last lines."""

    def test_tail_memory_is_bounded_not_proportional_to_file_size(self, _isolated_env):
        """Peak allocation stays near the tail, not near the file size.

        read_text().splitlines() allocates the whole file twice over; with 24 h
        retention a chatty instance's log can exceed the process's memory
        budget, so one GET must not be able to exhaust it.
        """
        import tracemalloc

        from solar_host.routes.instances import _tail_lines

        path = _isolated_env / "big.log"
        with path.open("w") as handle:
            for i in range(100_000):
                handle.write(f"line {i} {'x' * 50}\n")
        file_size = path.stat().st_size
        assert file_size > 5_000_000, file_size

        tracemalloc.start()
        try:
            lines = _tail_lines(path, 3)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert lines == [f"line {i} {'x' * 50}" for i in (99_997, 99_998, 99_999)]
        # A whole-file read would peak at >= file_size; the chunked tail is
        # bounded by _TAIL_CHUNK_BYTES plus the returned lines.
        assert peak < file_size // 10, (peak, file_size)

    def test_tail_handles_file_without_trailing_newline(self, _isolated_env):
        from solar_host.routes.instances import _tail_lines

        path = _isolated_env / "no-newline.log"
        path.write_text("one\ntwo\nthree")
        assert _tail_lines(path, 2) == ["two", "three"]

    def test_tail_returns_whole_short_file(self, _isolated_env):
        from solar_host.routes.instances import _tail_lines

        path = _isolated_env / "short.log"
        path.write_text("only\n")
        assert _tail_lines(path, 10) == ["only"]

    def test_tail_of_missing_file_is_empty(self, _isolated_env):
        from solar_host.routes.instances import _tail_lines

        assert _tail_lines(_isolated_env / "gone.log", 5) == []

    @pytest.mark.anyio
    async def test_glob_metacharacters_in_instance_id_are_escaped(
        self, _isolated_env, monkeypatch
    ):
        """The id arrives from the URL; an unescaped '*' would match foreign files."""
        manager = ProcessManager()
        (manager.log_dir / "alias_other_123.log").write_text("someone else\n")
        monkeypatch.setattr("solar_host.routes.instances.process_manager", manager)
        from fastapi import HTTPException

        from solar_host.routes.instances import get_instance_logs

        with pytest.raises(HTTPException) as excinfo:
            await get_instance_logs("*")
        assert excinfo.value.status_code == 404

    def test_mtime_of_missing_file_sorts_last(self, _isolated_env):
        """stat() on a file unlinked between glob and sort must not propagate."""
        from solar_host.routes.instances import _mtime_or_zero

        assert _mtime_or_zero(_isolated_env / "gone.log") == 0.0

    @pytest.mark.anyio
    async def test_file_unlinked_after_glob_does_not_error(
        self, _isolated_env, monkeypatch
    ):
        """A rotation racing the read yields empty logs, not a 500."""
        manager = ProcessManager()
        _make_instance("inst-1")
        vanished = manager.log_dir / "alias_inst-1_123.log"
        monkeypatch.setattr(
            type(manager.log_dir), "glob", lambda self, pattern: iter([vanished])
        )
        monkeypatch.setattr("solar_host.routes.instances.process_manager", manager)
        from solar_host.routes.instances import get_instance_logs

        logs = await get_instance_logs("inst-1")
        assert logs == []
