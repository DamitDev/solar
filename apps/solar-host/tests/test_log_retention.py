"""C2 log retention: the log buffer survives process exit and manual stop,
is cleared only by delete, dead-instance buffers are evicted beyond the cap,
on-disk files honour log_file_retention_s (keeping the newest per alias),
and log filenames carry the instance id."""

import sys
import time
from collections import deque

import pytest

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.config import config_manager
from solar_host.models import InstanceStatus, LogMessage
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.process_manager import ProcessManager


class _ScriptRunner(LlamaCppRunner):
    """LlamaCpp runner whose command is a python one-liner and whose ready
    line is a sentinel substring, so tests drive a real subprocess."""

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
    """Point settings and the global config manager at a tmp workspace."""
    monkeypatch.setattr("solar_host.config.settings.log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 5.0)
    monkeypatch.setattr("solar_host.config.settings.retained_log_buffers", 20)
    monkeypatch.setattr("solar_host.config.settings.log_file_retention_s", 86400.0)
    config_manager.config_file = tmp_path / "config.json"
    config_manager.instances = {}
    return tmp_path


def _log_msg(seq: int, line: str) -> LogMessage:
    return LogMessage(seq=seq, timestamp="2026-08-06T00:00:00+00:00", line=line)


async def _run_to_failure(manager, instance_id="inst-1") -> ProcessManager:
    """Start an instance whose script prints known lines and exits non-zero."""
    _make_instance(instance_id)
    runner = _ScriptRunner(
        "print('line one', flush=True); print('line two', flush=True); "
        "import sys; sys.exit(3)"
    )
    import solar_host.process_manager as pm

    pm.get_runner_for_config = lambda cfg: runner
    manager.instance_runners[instance_id] = runner
    await manager.start_instance(instance_id)
    return manager


class TestBufferSurvival:
    @pytest.mark.anyio
    async def test_buffer_survives_child_exit(self, _isolated_env, monkeypatch):
        manager = ProcessManager()
        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config",
            lambda cfg: _ScriptRunner(
                "print('line one', flush=True); print('line two', flush=True); "
                "import sys; sys.exit(3)"
            ),
        )
        _make_instance("inst-1")
        result = await manager.start_instance("inst-1")
        assert result is False

        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.FAILED
        assert "exit code: 3" in (instance.error_message or "")

        # The exact logs that explain the exit are still readable.
        lines = [m.line for m in manager.get_log_buffer("inst-1")]
        assert "line one" in lines
        assert "line two" in lines
        # Exit code recorded for the structured failure payload.
        assert manager.get_last_exit_code("inst-1") == 3
        # The on-disk files carry the instance id (one per start attempt —
        # start retries after a fast exit, which is the designed behaviour).
        files = sorted(manager.log_dir.glob("*_inst-1_*.log"))
        assert len(files) >= 1
        assert "line one" in files[-1].read_text()

    @pytest.mark.anyio
    async def test_buffer_survives_manual_stop(self, _isolated_env, monkeypatch):
        manager = ProcessManager()
        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config",
            lambda cfg: _ScriptRunner(
                "import time; print('READY_MARKER', flush=True); time.sleep(30)"
            ),
        )
        _make_instance("inst-1")
        assert await manager.start_instance("inst-1") is True
        assert await manager.stop_instance("inst-1") is True

        lines = [m.line for m in manager.get_log_buffer("inst-1")]
        assert "READY_MARKER" in lines

    @pytest.mark.anyio
    async def test_delete_clears_buffer_and_exit_code(self, _isolated_env, monkeypatch):
        manager = ProcessManager()
        await _run_to_failure(manager)
        assert manager.get_log_buffer("inst-1")

        manager.delete_instance("inst-1")
        assert manager.get_log_buffer("inst-1") == []
        assert manager.get_last_exit_code("inst-1") is None


class TestRetainedBufferEviction:
    def test_oldest_evicted_beyond_cap(self, _isolated_env, monkeypatch):
        monkeypatch.setattr("solar_host.config.settings.retained_log_buffers", 2)
        manager = ProcessManager()
        for i in range(4):
            manager.log_buffers[f"inst-{i}"] = deque(
                [_log_msg(0, f"log-{i}")], maxlen=5
            )
            manager.log_sequences[f"inst-{i}"] = 1
            manager._purge_instance_resources(f"inst-{i}", keep_logs=True)

        # Only the two newest survive.
        assert "inst-0" not in manager.log_buffers
        assert "inst-1" not in manager.log_buffers
        assert "inst-2" in manager.log_buffers
        assert "inst-3" in manager.log_buffers

    def test_live_instance_buffer_is_never_evicted(self, _isolated_env, monkeypatch):
        """A stop/start cycle leaves the id in the retention registry.

        Evicting it would drop a *running* instance's buffer, and the next log
        line would reset log_sequences to 0 — breaking the seq:timestamp dedup
        the webui LogViewer relies on.
        """
        monkeypatch.setattr("solar_host.config.settings.retained_log_buffers", 2)
        manager = ProcessManager()

        # inst-live was stopped once (so it is registered as dead), then
        # started again — it now has a live process.
        manager.log_buffers["inst-live"] = deque([_log_msg(0, "live")], maxlen=5)
        manager.log_sequences["inst-live"] = 1
        manager._purge_instance_resources("inst-live", keep_logs=True)
        manager.processes["inst-live"] = object()  # type: ignore[assignment]

        for i in range(3):
            manager.log_buffers[f"inst-{i}"] = deque(
                [_log_msg(0, f"log-{i}")], maxlen=5
            )
            manager.log_sequences[f"inst-{i}"] = 1
            manager._purge_instance_resources(f"inst-{i}", keep_logs=True)

        assert "inst-live" in manager.log_buffers
        assert manager.log_sequences["inst-live"] == 1

    @pytest.mark.anyio
    async def test_successful_start_releases_retention_slot(
        self, _isolated_env, monkeypatch
    ):
        """A restarted instance stops occupying a dead-log retention slot."""
        manager = ProcessManager()
        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config",
            lambda cfg: _ScriptRunner(
                "import time; print('READY_MARKER', flush=True); time.sleep(30)"
            ),
        )
        _make_instance("inst-1")
        assert await manager.start_instance("inst-1") is True
        assert await manager.stop_instance("inst-1") is True
        assert "inst-1" in manager._retained_log_ids

        assert await manager.start_instance("inst-1") is True
        assert "inst-1" not in manager._retained_log_ids
        await manager.stop_instance("inst-1")


class TestCleanupOldLogs:
    @pytest.mark.anyio
    async def test_honours_retention_and_keeps_newest(self, _isolated_env, monkeypatch):
        """Within one instance, expired files go and the newest is protected."""
        monkeypatch.setattr("solar_host.config.settings.log_file_retention_s", 300.0)
        manager = ProcessManager()
        old = manager.log_dir / "alias_inst-1_1000000000.log"
        newest = manager.log_dir / "alias_inst-1_2000000000.log"
        old.write_text("old")
        newest.write_text("new")
        # Both are older than the 300 s cutoff; the newest file is protected.
        old_mtime = time.time() - 1000
        newest_mtime = time.time() - 500
        import os

        os.utime(old, (old_mtime, old_mtime))
        os.utime(newest, (newest_mtime, newest_mtime))

        await manager._cleanup_old_logs("alias")

        assert not old.exists()
        assert newest.exists()

    @pytest.mark.anyio
    async def test_keeps_recent_files(self, _isolated_env, monkeypatch):
        monkeypatch.setattr("solar_host.config.settings.log_file_retention_s", 300.0)
        manager = ProcessManager()
        recent = manager.log_dir / "alias_inst-3_3000000000.log"
        recent.write_text("recent")
        await manager._cleanup_old_logs("alias")
        assert recent.exists()

    @pytest.mark.anyio
    async def test_keeps_newest_per_instance_not_per_alias(
        self, _isolated_env, monkeypatch
    ):
        """A multi-replica intent must not lose every replica's post-mortem.

        Keeping only the newest file per alias leaves exactly one replica's
        logs; each replica needs its own last file.
        """
        import os

        monkeypatch.setattr("solar_host.config.settings.log_file_retention_s", 300.0)
        manager = ProcessManager()
        files = {
            "a-old": manager.log_dir / "alias_inst-a_1000.log",
            "a-new": manager.log_dir / "alias_inst-a_2000.log",
            "b-old": manager.log_dir / "alias_inst-b_1000.log",
            "b-new": manager.log_dir / "alias_inst-b_2000.log",
        }
        # All four are past the cutoff; mtimes order them within each replica.
        mtimes = {"a-old": 4000, "a-new": 3000, "b-old": 2000, "b-new": 1000}
        for key, path in files.items():
            path.write_text(key)
            stamp = time.time() - mtimes[key]
            os.utime(path, (stamp, stamp))

        await manager._cleanup_old_logs("alias")

        assert files["a-new"].exists()
        assert files["b-new"].exists()
        assert not files["a-old"].exists()
        assert not files["b-old"].exists()


class TestStartAttemptState:
    @pytest.mark.anyio
    async def test_stale_exit_code_not_reported_on_readiness_timeout(
        self, _isolated_env, monkeypatch
    ):
        """A readiness timeout reports its own kill, never the previous run's.

        The backend was alive and simply never reported ready, so the host
        signals it: the recorded code is negative (killed by SIGTERM), which is
        what distinguishes a hung start from a backend that died on its own.
        Reporting the earlier attempt's 42 would blame the wrong failure, and
        reporting nothing leaves the payload's exit_code null.
        """
        import signal

        monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 0.5)
        monkeypatch.setattr("solar_host.config.settings.max_retries", 1)
        manager = ProcessManager()
        _make_instance("inst-1")
        # A previous run left an exit code behind.
        manager.last_exit_codes["inst-1"] = 42

        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config",
            # Never prints the ready marker, stays alive past the timeout.
            lambda cfg: _ScriptRunner("import time; time.sleep(30)"),
        )
        assert await manager.start_instance("inst-1") is False
        assert manager.get_last_exit_code("inst-1") == -signal.SIGTERM

    @pytest.mark.anyio
    async def test_retry_buffer_reflects_final_attempt_only(
        self, _isolated_env, monkeypatch
    ):
        """log_tail must describe the attempt that actually failed."""
        monkeypatch.setattr("solar_host.config.settings.max_retries", 2)
        manager = ProcessManager()
        _make_instance("inst-1")

        attempts: list[int] = []

        def _runner(_cfg):
            attempts.append(len(attempts))
            return _ScriptRunner(
                f"print('attempt {len(attempts) - 1}', flush=True); "
                "import sys; sys.exit(1)"
            )

        monkeypatch.setattr("solar_host.process_manager.get_runner_for_config", _runner)
        assert await manager.start_instance("inst-1") is False
        assert len(attempts) > 1

        lines = [m.line for m in manager.get_log_buffer("inst-1")]
        assert f"attempt {len(attempts) - 1}" in lines
        assert "attempt 0" not in lines

    @pytest.mark.anyio
    async def test_first_attempt_keeps_retained_buffer(
        self, _isolated_env, monkeypatch
    ):
        """Starting a stopped instance must not discard its retained logs."""
        manager = ProcessManager()
        _make_instance("inst-1")
        manager.log_buffers["inst-1"] = deque([_log_msg(0, "from before")], maxlen=5)
        manager.log_sequences["inst-1"] = 1

        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config",
            lambda cfg: _ScriptRunner(
                "import time; print('READY_MARKER', flush=True); time.sleep(30)"
            ),
        )
        assert await manager.start_instance("inst-1") is True
        lines = [m.line for m in manager.get_log_buffer("inst-1")]
        assert "from before" in lines
        await manager.stop_instance("inst-1")
