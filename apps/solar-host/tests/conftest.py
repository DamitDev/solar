"""Shared pytest fixtures for solar-host tests."""

import os
import queue
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from solar_host.config import Settings, config_manager, settings
from solar_host.process_manager import process_manager

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_ffmpeg(monkeypatch, tmp_path):
    """Put a fake ``ffmpeg`` first on PATH.

    The HuggingFace audio route decodes every upload by executing ``ffmpeg``,
    which the CI runner does not have. The fake implements the route's exact
    argv for the 16 kHz mono PCM16 WAV the tests upload, so the route keeps
    exercising its real ``shutil.which`` gate, subprocess plumbing and
    decode-failure envelope.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    launcher = bin_dir / "ffmpeg"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(FIXTURES / 'fake_ffmpeg.py'))} \"$@\"\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return launcher


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch, tmp_path):
    """Reset every Settings field to its declared default.

    ``config.py`` skips ``.env`` under pytest, but exported variables still
    reach ``Settings`` — including ``SGLANG_PROMPT_CACHE_DIR`` (the boot
    detach pass rmtrees recursively), ``SOLAR_CONTROL_URL`` (starts real WS
    clients and background loops in every lifespan test), ``LOG_DIR``,
    ``MODELS_DIR``, ``JOBS_DIR``, ``MAX_RETRIES``,
    ``INSTANCE_READY_TIMEOUT_S``, ``HF_TOKEN`` and ``HARBOR_PASSWORD``.
    A no-op in CI, where no such variables exist; locally it makes every
    run match CI. Tests that need a non-default value override it on their
    own fixture, which runs after this autouse one and wins.
    """
    for name, field in Settings.model_fields.items():
        monkeypatch.setattr(
            settings, name, field.get_default(call_default_factory=True)
        )
    # SGLang key config files (IT Sec #97) live under tempfile.gettempdir();
    # point it at tmp_path so no test writes to or sweeps the real
    # /tmp/solar-host-<uid>.
    test_tmp = tmp_path / "tmp"
    test_tmp.mkdir(exist_ok=True)
    monkeypatch.setattr("tempfile.tempdir", str(test_tmp))


_PER_INSTANCE_STATE = (
    "processes",
    "log_buffers",
    "log_sequences",
    "log_threads",
    "state_buffers",
    "state_sequences",
    "instance_contexts",
    "instance_runners",
    "instance_usage_snapshots",
    "ready_events",
    "last_exit_codes",
)


def _reset_process_manager() -> None:
    """Return the process_manager singleton to its post-__init__ state.

    The singleton survives the whole pytest session, so state accumulated
    by one test must never leak into the next: leaked children are reaped,
    per-instance dicts cleared, queued events drained, and references to a
    possibly-closed event loop dropped. The child-exit lock is never
    replaced — instead it must be acquirable, so a leaked hold fails
    loudly in the test that caused it rather than deadlocking a later one.
    """
    # 1. Reap leaked children first. The reap iterates the *singleton*, so
    #    tests that build a local ProcessManager (e.g.
    #    test_readiness_timeout_detaches_cache_dir) are not covered by it —
    #    that test is safe because its timeout handler kills its own child.
    #    Best-effort: a child that already exited, or refuses to die, must
    #    not fail the reset.
    for proc in list(process_manager.processes.values()):
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # noqa: S110, BLE001 — best-effort reap
                pass
        except Exception:  # noqa: S110, BLE001 — best-effort reap
            pass

    # The log-reader threads finish with a final _handle_child_exit, which
    # briefly holds the child-exit lock and re-reads the per-instance
    # dicts. Let them drain (bounded) so the clear() below sticks and the
    # lock assertion at the end cannot false-positive on a thread that is
    # legitimately finishing, not leaking.
    for thread in list(process_manager.log_threads.values()):
        if thread.is_alive():
            thread.join(timeout=5)

    # 2. Drop every per-instance dict, including the retained dead-log
    #    registry.
    for name in _PER_INSTANCE_STATE:
        getattr(process_manager, name).clear()
    process_manager._retained_log_ids.clear()

    # 3. Drain both emission queues so events queued by one test cannot
    #    surface in a later test's flush.
    for event_queue in (process_manager._log_queue, process_manager._state_queue):
        while True:
            try:
                event_queue.get_nowait()
            except queue.Empty:
                break

    # 4. Both can otherwise reference an event loop the previous
    #    TestClient already closed.
    process_manager._ready_loop = None
    process_manager._flush_task = None

    # 5. The lock must be free: a leaked hold would deadlock a later
    #    _handle_child_exit / _mark_instance_ready. Replacing the lock
    #    would silently drop the leaked one instead of failing here.
    child_exit_lock = process_manager._child_exit_lock
    assert child_exit_lock.acquire(blocking=False), (
        "process_manager._child_exit_lock still held after a test — "
        "something leaked it (a thread that never released it?)"
    )
    child_exit_lock.release()


@pytest.fixture(autouse=True)
def _clean_process_manager(tmp_path, monkeypatch):
    """Keep the process_manager/config_manager singletons per-test.

    Both survive the session, and ProcessManager cached log_dir at import
    time, so route-driven tests write into apps/solar-host/logs and
    _cleanup_old_logs globs whatever earlier runs left there. Rooting the
    singleton's log_dir and config_manager's config_file in tmp_path, and
    resetting the singleton state on both sides of the test, makes every
    test start clean regardless of order.
    """
    host_logs = tmp_path / "host-logs"
    host_logs.mkdir()
    monkeypatch.setattr(process_manager, "log_dir", host_logs)
    monkeypatch.setattr(config_manager, "config_file", tmp_path / "host-config.json")
    config_manager.instances = {}
    _reset_process_manager()
    yield
    _reset_process_manager()
