"""SGLang prompt-cache cleanup: detach/purge helper behaviour (tmp_path) and
process-manager teardown coverage — failed starts, stop, delete, child exit,
the live-run guard, and the boot sweep."""

import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.backends.sglang import (
    SglangRunner,
    detach_all_prompt_cache_dirs,
    detach_instance_prompt_cache,
    discard_orphan_prompt_caches,
    purge_in_background,
)
from solar_host.config import config_manager
from solar_host.models import InstanceStatus, LogMessage
from solar_host.models.base import Instance
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.models.sglang import SglangConfig
from solar_host.process_manager import ProcessManager

# uuid4-shaped suffixes matching the shape ProcessManager.create_instance
# generates; the detach-pass guard accepts these and nothing else.
_UUID_A = "11111111-1111-1111-1111-111111111111"
_UUID_B = "22222222-2222-2222-2222-222222222222"
_UUID_C = "33333333-3333-3333-3333-333333333333"


def _log_msg(seq: int, line: str) -> LogMessage:
    return LogMessage(seq=seq, timestamp="2026-08-06T00:00:00+00:00", line=line)


def _failing_path_cls(
    method: str, *, only: Callable[[Path], bool] | None = None
) -> type[Path]:
    """A ``Path`` subclass whose *method* raises ``OSError``, for *only* if given.

    Patch it over ``solar_host.backends.sglang.Path`` so just the paths the
    module constructs are affected — patching ``pathlib.Path`` itself breaks
    the method for the whole interpreter, including other tests' purge
    threads. Python 3.12 propagates the subclass through ``/``, ``iterdir``
    and ``with_name``, so every path the module derives inherits it.
    """

    def _raise(self, *args: object, **kwargs: object):
        if only is None or only(self):
            raise OSError("permission denied")
        return getattr(Path, method)(self, *args, **kwargs)

    return type("FailingPath", (Path,), {method: _raise})


def _make_sglang_instance(
    instance_id: str = "inst-1", status=InstanceStatus.STOPPED
) -> Instance:
    instance = Instance(
        id=instance_id,
        config=SglangConfig(model_path="/models/test", alias="deepseek:flash"),
        status=status,
    )
    config_manager.add_instance(instance)
    return instance


def _make_llamacpp_instance(
    instance_id: str = "inst-1", status=InstanceStatus.STOPPED
) -> Instance:
    instance = Instance(
        id=instance_id,
        config=LlamaCppConfig(model="/tmp/test.gguf", alias="test"),
        status=status,
    )
    config_manager.add_instance(instance)
    return instance


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


class _SglangEnvScriptRunner(_ScriptRunner):
    """Script runner that builds the real SGLang environment, so a start
    (re)creates the instance's prompt-cache dir the way SGLang's own start
    does while the command stays a harmless python one-liner."""

    def build_env(self, instance) -> dict[str, str]:
        return SglangRunner().build_env(instance)


@pytest.fixture(autouse=True)
def _isolated_env(_hermetic_settings, tmp_path, monkeypatch):
    """Point settings and the global config manager at a tmp workspace.

    Declares the conftest settings guard so this fixture's tmp-path
    override of ``sglang_prompt_cache_dir`` deterministically wins over it.
    """
    monkeypatch.setattr("solar_host.config.settings.log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 5.0)
    monkeypatch.setattr("solar_host.config.settings.retained_log_buffers", 20)
    monkeypatch.setattr(
        "solar_host.config.settings.sglang_prompt_cache_dir",
        str(tmp_path / "prompt-cache"),
    )
    config_manager.config_file = tmp_path / "config.json"
    config_manager.instances = {}
    return tmp_path


def _wait_for_removal(path: Path, timeout: float = 5.0) -> bool:
    """Deadline-poll *path*'s disappearance.

    The purge loop runs on a daemon thread, so its effect is asynchronous.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not path.exists():
            return True
        time.sleep(0.05)
    return False


class TestDetachPurgePromptCache:
    def _cache_dir(self, _isolated_env, name: str = "deepseek-flash-inst-1") -> Path:
        cache_dir = Path(_isolated_env / "prompt-cache" / name)
        cache_dir.mkdir(parents=True)
        (cache_dir / "cache.bin").write_bytes(b"x")
        return cache_dir

    def test_detach_renames_dir_aside_and_returns_trash_path(
        self, _isolated_env
    ) -> None:
        cache_dir = self._cache_dir(_isolated_env)

        trash_dir = detach_instance_prompt_cache("deepseek:flash", "inst-1")

        assert trash_dir is not None
        assert trash_dir.name.startswith(".trash-")
        assert not cache_dir.exists()
        assert trash_dir.is_dir()
        assert (trash_dir / "cache.bin").read_bytes() == b"x"
        # The instance's dir is gone, so a second detach has nothing to do.
        assert detach_instance_prompt_cache("deepseek:flash", "inst-1") is None

    def test_detach_missing_path_is_a_noop(self, _isolated_env) -> None:
        assert detach_instance_prompt_cache("deepseek:flash", "inst-1") is None

    def test_detach_symlink_is_left_alone(self, _isolated_env, tmp_path) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        cache_root.mkdir(parents=True)
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (cache_root / "deepseek-flash-inst-1").symlink_to(
            outside, target_is_directory=True
        )

        assert detach_instance_prompt_cache("deepseek:flash", "inst-1") is None
        assert (cache_root / "deepseek-flash-inst-1").is_symlink()
        assert outside.is_dir()

    def test_detach_unset_root_is_a_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("solar_host.config.settings.sglang_prompt_cache_dir", "")

        assert detach_instance_prompt_cache("deepseek:flash", "inst-1") is None

    def test_detach_probe_oserror_is_swallowed(
        self, _isolated_env, monkeypatch
    ) -> None:
        self._cache_dir(_isolated_env)

        monkeypatch.setattr(
            "solar_host.backends.sglang.Path", _failing_path_cls("is_symlink")
        )
        assert detach_instance_prompt_cache("deepseek:flash", "inst-1") is None

    def test_purge_removes_trash_dir(self, _isolated_env) -> None:
        trash_dir = Path(_isolated_env / "prompt-cache" / f".trash-{_UUID_A}")
        trash_dir.mkdir(parents=True)

        purge_in_background([trash_dir])

        assert _wait_for_removal(trash_dir)

    def test_purge_missing_dir_is_a_noop(self, _isolated_env) -> None:
        purge_in_background(
            [Path(_isolated_env / "prompt-cache" / ".trash-never-created")]
        )  # must not raise

    def test_purge_oserror_is_swallowed(self, _isolated_env, monkeypatch) -> None:
        trash_dir = Path(_isolated_env / "prompt-cache" / f".trash-{_UUID_A}")
        trash_dir.mkdir(parents=True)
        purge_entered = threading.Event()

        def _raise(path) -> None:
            purge_entered.set()
            raise OSError("permission denied")

        monkeypatch.setattr("solar_host.backends.sglang.shutil.rmtree", _raise)
        purge_in_background([trash_dir])  # must not raise
        assert purge_entered.wait(5)
        assert trash_dir.is_dir()


class TestDetachAllPromptCacheDirs:
    def test_removes_every_orphan(self, _isolated_env) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        names = (
            f"qwen3.6-{_UUID_A}",
            f"qwen3.8-{_UUID_B}",
            f"deepseek-v4-flash-{_UUID_C}",
        )
        for name in names:
            (cache_root / name).mkdir(parents=True)

        detached = detach_all_prompt_cache_dirs()

        # All three are renamed aside to `.trash-*` and returned...
        assert len(detached) == 3
        assert all(p.name.startswith(".trash-") for p in detached)
        assert not any((cache_root / name).exists() for name in names)
        # ...and purging the returned list empties the root.
        purge_in_background(detached)
        assert all(_wait_for_removal(p) for p in detached)
        assert list(cache_root.iterdir()) == []

    def test_trash_leftovers_collected_without_second_rename(
        self, _isolated_env
    ) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        trash = cache_root / f".trash-{_UUID_A}"
        trash.mkdir(parents=True)

        detached = detach_all_prompt_cache_dirs()

        # An interrupted purge's leftover is collected as-is, same path.
        assert detached == [trash]
        assert trash.is_dir()
        purge_in_background(detached)
        assert _wait_for_removal(trash)

    def test_leaves_non_host_shaped_dirs_alone(self, _isolated_env) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        foreign = cache_root / "shared-data"
        foreign.mkdir(parents=True)

        assert detach_all_prompt_cache_dirs() == []

        assert foreign.is_dir()

    def test_missing_cache_root_is_a_noop(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "solar_host.config.settings.sglang_prompt_cache_dir",
            str(tmp_path / "does-not-exist"),
        )
        assert detach_all_prompt_cache_dirs() == []

    def test_empty_cache_root_setting_is_a_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("solar_host.config.settings.sglang_prompt_cache_dir", "")
        assert detach_all_prompt_cache_dirs() == []

    def test_purge_oserror_is_swallowed(self, _isolated_env, monkeypatch) -> None:
        trash_dir = Path(_isolated_env / "prompt-cache" / f".trash-{_UUID_A}")
        trash_dir.mkdir(parents=True)
        purge_entered = threading.Event()

        def _raise(path) -> None:
            purge_entered.set()
            raise OSError("permission denied")

        monkeypatch.setattr("solar_host.backends.sglang.shutil.rmtree", _raise)
        purge_in_background([trash_dir])  # must not raise
        assert purge_entered.wait(5)
        assert trash_dir.is_dir()

    def test_purge_continues_after_unexpected_error(
        self, _isolated_env, monkeypatch
    ) -> None:
        """The loop runs on a daemon thread nobody joins, so one dir failing
        unexpectedly must not strand the rest of the batch until next boot."""
        cache_root = Path(_isolated_env / "prompt-cache")
        first = cache_root / f".trash-{_UUID_A}"
        second = cache_root / f".trash-{_UUID_B}"
        first.mkdir(parents=True)
        second.mkdir()
        real_rmtree = shutil.rmtree

        def _raise_on_first(path, *args: object, **kwargs: object):
            if Path(path) == first:
                raise RuntimeError("helper regression")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr("solar_host.backends.sglang.shutil.rmtree", _raise_on_first)

        purge_in_background([first, second])  # must not raise

        assert first.is_dir()
        assert _wait_for_removal(second)

    def test_rename_oserror_leaves_remaining_dirs_detached(
        self, _isolated_env, monkeypatch
    ) -> None:
        """A child whose rename fails is logged and skipped, while the
        remaining children are still detached."""
        cache_root = Path(_isolated_env / "prompt-cache")
        doomed = cache_root / f"qwen3.6-{_UUID_A}"
        survivor = cache_root / f"qwen3.8-{_UUID_B}"
        doomed.mkdir(parents=True)
        survivor.mkdir()

        monkeypatch.setattr(
            "solar_host.backends.sglang.Path",
            _failing_path_cls("rename", only=lambda p: p == doomed),
        )

        detached = detach_all_prompt_cache_dirs()

        assert doomed.is_dir()  # rename failed: logged and skipped
        assert not survivor.exists()  # still detached
        assert len(detached) == 1
        assert detached[0].name.startswith(".trash-")

    def test_root_probe_oserror_is_swallowed(self, _isolated_env, monkeypatch) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        # A detachable orphan: without the raise the pass would return it,
        # so an empty result proves the probe error aborted the scan.
        orphan = cache_root / f"orphan-{_UUID_A}"
        orphan.mkdir(parents=True)

        monkeypatch.setattr(
            "solar_host.backends.sglang.Path", _failing_path_cls("is_dir")
        )
        assert detach_all_prompt_cache_dirs() == []
        assert orphan.is_dir()

    def test_iterdir_oserror_is_swallowed(self, _isolated_env, monkeypatch) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        orphan = cache_root / f"orphan-{_UUID_A}"
        orphan.mkdir(parents=True)

        monkeypatch.setattr(
            "solar_host.backends.sglang.Path", _failing_path_cls("iterdir")
        )
        assert detach_all_prompt_cache_dirs() == []
        assert orphan.is_dir()

    def test_child_probe_oserror_is_swallowed(self, _isolated_env, monkeypatch) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        orphan = cache_root / f"orphan-{_UUID_A}"
        orphan.mkdir(parents=True)

        # The root's own probe has to succeed for the scan to reach a child.
        monkeypatch.setattr(
            "solar_host.backends.sglang.Path",
            _failing_path_cls("is_dir", only=lambda p: p != cache_root),
        )
        assert detach_all_prompt_cache_dirs() == []
        assert orphan.is_dir()

    def test_loose_files_and_symlinks_are_left_alone(
        self, _isolated_env, tmp_path
    ) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        cache_root.mkdir(parents=True)
        (cache_root / f"orphan-dir-{_UUID_A}").mkdir()
        (cache_root / "loose-file.bin").write_bytes(b"x")
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (cache_root / "symlink").symlink_to(outside, target_is_directory=True)

        detached = detach_all_prompt_cache_dirs()

        assert len(detached) == 1
        assert not (cache_root / f"orphan-dir-{_UUID_A}").exists()
        assert (cache_root / "loose-file.bin").exists()
        assert (cache_root / "symlink").is_symlink()
        assert outside.is_dir()


class TestDiscardOrphanPromptCaches:
    def test_detaches_synchronously_and_purges_in_background(
        self, _isolated_env, monkeypatch
    ) -> None:
        cache_root = Path(_isolated_env / "prompt-cache")
        orphan = cache_root / f"deepseek-flash-{_UUID_A}"
        orphan.mkdir(parents=True)
        purge_entered = threading.Event()
        release_purge = threading.Event()
        real_rmtree = shutil.rmtree

        def _blocking_rmtree(path, *args, **kwargs):
            purge_entered.set()
            release_purge.wait(5)
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "solar_host.backends.sglang.shutil.rmtree", _blocking_rmtree
        )

        discard_orphan_prompt_caches()

        # The detach pass is synchronous: the orphan is already renamed
        # aside when the call returns, while the purge thread is still
        # blocked.
        assert not orphan.exists()
        assert purge_entered.wait(5)
        trash_dirs = [p for p in cache_root.iterdir() if p.name.startswith(".trash-")]
        assert len(trash_dirs) == 1
        # The rmtree is backgrounded: it finishes asynchronously after the
        # daemon thread is released.
        release_purge.set()
        assert _wait_for_removal(trash_dirs[0])
        assert list(cache_root.iterdir()) == []


class TestProcessManagerTeardown:
    def _cache_dir(self, _isolated_env, instance_id: str) -> Path:
        cache_dir = Path(
            _isolated_env / "prompt-cache" / f"deepseek-flash-{instance_id}"
        )
        cache_dir.mkdir(parents=True)
        (cache_dir / "cache.bin").write_bytes(b"x")
        return cache_dir

    @pytest.mark.anyio
    async def test_stop_running_instance_detaches_cache_dir(
        self, _isolated_env
    ) -> None:
        _make_sglang_instance("inst-1", status=InstanceStatus.RUNNING)
        cache_dir = self._cache_dir(_isolated_env, "inst-1")
        manager = ProcessManager()
        # A RUNNING record with no tracked process: stop_instance skips
        # terminate/join and goes straight to the purge + cache detach.
        manager.log_buffers["inst-1"] = deque([_log_msg(0, "line one")], maxlen=5)
        manager.log_sequences["inst-1"] = 1

        assert await manager.stop_instance("inst-1") is True

        assert not cache_dir.exists()
        assert "inst-1" in manager.log_buffers
        assert manager.log_sequences["inst-1"] == 1
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.STOPPED

    def test_child_exit_detaches_cache_dir(self, _isolated_env) -> None:
        _make_sglang_instance("inst-1", status=InstanceStatus.RUNNING)
        cache_dir = self._cache_dir(_isolated_env, "inst-1")
        manager = ProcessManager()
        proc = SimpleNamespace(poll=lambda: 1)
        manager.processes["inst-1"] = proc  # type: ignore[assignment]

        manager._handle_child_exit("inst-1", proc)  # type: ignore[arg-type]

        assert not cache_dir.exists()
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.FAILED

    def test_child_exit_discard_runs_outside_lock(
        self, _isolated_env, monkeypatch
    ) -> None:
        _make_sglang_instance("inst-1", status=InstanceStatus.RUNNING)
        self._cache_dir(_isolated_env, "inst-1")
        manager = ProcessManager()
        checked = False

        def _checking_detach(alias: str, instance_id: str) -> None:
            nonlocal checked
            # No teardown work may run while _child_exit_lock is held —
            # _mark_instance_ready contends on it from other instances' log
            # threads, so a non-blocking acquire succeeding here proves a
            # concurrent promotion would not be blocked by the discard.
            assert manager._child_exit_lock.acquire(blocking=False)
            manager._child_exit_lock.release()
            checked = True

        monkeypatch.setattr(
            "solar_host.process_manager.detach_instance_prompt_cache", _checking_detach
        )
        proc = SimpleNamespace(poll=lambda: 1)
        manager.processes["inst-1"] = proc  # type: ignore[assignment]

        manager._handle_child_exit("inst-1", proc)  # type: ignore[arg-type]

        assert checked

    def test_delete_instance_detaches_cache_dir(self, _isolated_env) -> None:
        _make_sglang_instance("inst-1")
        cache_dir = self._cache_dir(_isolated_env, "inst-1")

        manager = ProcessManager()
        assert manager.delete_instance("inst-1") is True
        # call_runner_on_stop=False must not skip the cache removal.
        assert not cache_dir.exists()
        assert config_manager.get_instance("inst-1") is None

    @pytest.mark.anyio
    async def test_stop_failed_detaches_cache_dir(self, _isolated_env) -> None:
        _make_sglang_instance("inst-1", status=InstanceStatus.FAILED)
        cache_dir = self._cache_dir(_isolated_env, "inst-1")

        manager = ProcessManager()
        assert await manager.stop_instance("inst-1") is True

        assert not cache_dir.exists()
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.STOPPED

    @pytest.mark.anyio
    async def test_rmtree_oserror_does_not_fail_stop(
        self, _isolated_env, monkeypatch
    ) -> None:
        _make_sglang_instance("inst-1", status=InstanceStatus.FAILED)
        self._cache_dir(_isolated_env, "inst-1")

        def _raise(path) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr("solar_host.backends.sglang.shutil.rmtree", _raise)

        manager = ProcessManager()
        assert await manager.stop_instance("inst-1") is True
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.STOPPED

    def test_delete_instance_succeeds_when_cache_helper_raises(
        self, _isolated_env, monkeypatch
    ) -> None:
        """delete_instance is the one synchronous cache call site; a helper
        regression must not fail it."""
        _make_sglang_instance("inst-1")
        self._cache_dir(_isolated_env, "inst-1")

        def _boom(alias: str, instance_id: str) -> None:
            raise RuntimeError("helper regression")

        monkeypatch.setattr(
            "solar_host.process_manager.detach_instance_prompt_cache", _boom
        )

        manager = ProcessManager()
        assert manager.delete_instance("inst-1") is True
        assert config_manager.get_instance("inst-1") is None

    @pytest.mark.anyio
    async def test_failed_start_detaches_cache_dir(
        self, _isolated_env, monkeypatch
    ) -> None:
        """A start that fails before spawning must not strand its cache dir."""
        monkeypatch.setattr("solar_host.config.settings.max_retries", 0)
        _make_sglang_instance("inst-1")
        cache_dir = self._cache_dir(_isolated_env, "inst-1")

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("never spawn")

        monkeypatch.setattr(
            "solar_host.backends.sglang.SglangRunner.build_command", _boom
        )

        manager = ProcessManager()
        assert await manager.start_instance("inst-1") is False

        assert not cache_dir.exists()
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.FAILED

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "prior_status",
        [InstanceStatus.RUNNING, InstanceStatus.FAILED],
        ids=["stale-running-record", "crashed-record"],
    )
    async def test_start_never_inherits_a_previous_runs_cache_dir(
        self, _isolated_env, monkeypatch, prior_status
    ) -> None:
        """A start detaches whatever survived, before build_env re-creates it.

        ``build_env`` only mkdirs(exist_ok=True), so without the start-path
        detach the new run would inherit the old contents. Both entry states
        matter: a stale RUNNING record (reset in place) and a FAILED one
        whose teardown discard never ran — either because the stop failed or
        because the live-record guard skipped it when this start published
        STARTING first.

        The start is driven all the way through ``build_env``, which
        re-creates the dir — so the dir existing again proves nothing and
        its *contents* are the assertion.
        """
        _make_sglang_instance("inst-1", status=prior_status)
        cache_dir = self._cache_dir(_isolated_env, "inst-1")
        runner = _SglangEnvScriptRunner(
            "print('READY_MARKER', flush=True); import time; time.sleep(30)"
        )
        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
        )

        manager = ProcessManager()
        try:
            assert await manager.start_instance("inst-1") is True

            # build_env re-created the dir for the new run; what must be
            # gone is everything the previous run left in it.
            assert cache_dir.is_dir()
            assert not (cache_dir / "cache.bin").exists()
            instance = config_manager.get_instance("inst-1")
            assert instance is not None
            assert instance.status == InstanceStatus.RUNNING
        finally:
            # Local manager, so the conftest reset does not reap this child
            # or join its log thread. Popping the process first also makes
            # the reader's final _handle_child_exit a no-op, so it cannot
            # touch the shared config_manager after the test.
            proc = manager.processes.pop("inst-1", None)
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log_thread = manager.log_threads.get("inst-1")
            if log_thread is not None:
                log_thread.join(timeout=5)

    def test_discard_is_noop_while_process_is_tracked(self, _isolated_env) -> None:
        """A live child owns the dir: _discard_sglang_prompt_cache must not
        detach it. The boot detach pass is the backstop for one we
        deliberately leave behind here."""
        _make_sglang_instance("inst-1", status=InstanceStatus.RUNNING)
        cache_dir = self._cache_dir(_isolated_env, "inst-1")
        manager = ProcessManager()
        manager.processes["inst-1"] = object()  # type: ignore[assignment]

        manager._discard_sglang_prompt_cache(config_manager.get_instance("inst-1"))

        assert cache_dir.is_dir()
        assert (cache_dir / "cache.bin").read_bytes() == b"x"

    def test_discard_is_noop_while_record_is_starting(self, _isolated_env) -> None:
        """A STARTING record with no tracked process yet still owns the dir:
        the discard races the fresh start's build_env, which publishes
        STARTING before it (re)creates the dir."""
        _make_sglang_instance("inst-1", status=InstanceStatus.STARTING)
        cache_dir = self._cache_dir(_isolated_env, "inst-1")
        manager = ProcessManager()

        manager._discard_sglang_prompt_cache(config_manager.get_instance("inst-1"))

        assert cache_dir.is_dir()
        assert (cache_dir / "cache.bin").read_bytes() == b"x"

    @pytest.mark.anyio
    async def test_readiness_timeout_detaches_cache_dir(
        self, _isolated_env, monkeypatch
    ) -> None:
        """A start that times out must not strand its cache dir.

        The record carries an SglangConfig so the detach is not gated out;
        the timed-out child is killed and the record published FAILED
        first, then the dir is detached so a retry (or a later start) does
        not inherit this attempt's cache dir.
        """
        monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 1.0)
        monkeypatch.setattr("solar_host.config.settings.max_retries", 0)
        _make_sglang_instance("inst-1")
        cache_dir = self._cache_dir(_isolated_env, "inst-1")
        runner = _ScriptRunner("import time; time.sleep(60)")

        monkeypatch.setattr(
            "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
        )

        manager = ProcessManager()
        assert await manager.start_instance("inst-1") is False

        assert not cache_dir.exists()
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.FAILED

    @pytest.mark.anyio
    async def test_stop_succeeds_when_cache_helper_raises(
        self, _isolated_env, monkeypatch
    ) -> None:
        _make_sglang_instance("inst-1", status=InstanceStatus.FAILED)
        self._cache_dir(_isolated_env, "inst-1")

        def _boom(alias: str, instance_id: str) -> None:
            raise RuntimeError("helper regression")

        monkeypatch.setattr(
            "solar_host.process_manager.detach_instance_prompt_cache", _boom
        )

        manager = ProcessManager()
        assert await manager.stop_instance("inst-1") is True
        instance = config_manager.get_instance("inst-1")
        assert instance is not None
        assert instance.status == InstanceStatus.STOPPED

    @pytest.mark.anyio
    async def test_stop_llamacpp_touches_nothing(self, _isolated_env) -> None:
        _make_llamacpp_instance("inst-1", status=InstanceStatus.RUNNING)
        cache_root = Path(_isolated_env / "prompt-cache")
        cache_root.mkdir(parents=True)
        # A same-shaped dir that would collide with this alias if the gating
        # were missing, plus an unrelated dir.
        llama_shaped = cache_root / "test-inst-1"
        llama_shaped.mkdir()
        unrelated = cache_root / "someone-elses"
        unrelated.mkdir()

        manager = ProcessManager()
        assert await manager.stop_instance("inst-1") is True

        assert llama_shaped.exists()
        assert unrelated.exists()

    @pytest.mark.anyio
    async def test_stop_returns_while_purge_is_still_running(
        self, _isolated_env, monkeypatch
    ) -> None:
        """The seam is ``rmtree``, not ``purge_in_background``.

        The helper owns the daemon thread, so a blocking fake in its place
        would run on the caller's thread and the stop would wait for it —
        the very regression this test exists to catch.
        """
        _make_sglang_instance("inst-1", status=InstanceStatus.FAILED)
        cache_dir = self._cache_dir(_isolated_env, "inst-1")
        rmtree_entered = threading.Event()
        release_rmtree = threading.Event()
        real_rmtree = shutil.rmtree

        def _blocking_rmtree(path, *args: object, **kwargs: object):
            rmtree_entered.set()
            release_rmtree.wait(5)
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "solar_host.backends.sglang.shutil.rmtree", _blocking_rmtree
        )

        manager = ProcessManager()
        started_at = time.monotonic()
        assert await manager.stop_instance("inst-1") is True
        elapsed = time.monotonic() - started_at

        # Stopping a FAILED record does no process work at all, so anything
        # near the fake's 5s park means the stop waited on the purge.
        assert elapsed < 1.0
        assert rmtree_entered.wait(5)
        # The detach is synchronous, so the instance's path is already gone
        # while the purge thread is still parked inside rmtree.
        assert not cache_dir.exists()
        cache_root = Path(_isolated_env / "prompt-cache")
        (trash_dir,) = [p for p in cache_root.iterdir() if p.name.startswith(".trash-")]

        release_rmtree.set()
        assert _wait_for_removal(trash_dir)


class TestLifespanOrdering:
    def test_orphans_detached_before_init_clients(
        self, _isolated_env, monkeypatch
    ) -> None:
        """The boot detach runs before the control clients start, so no
        start can be in flight while it scans.

        That ordering is the whole reason the pass needs no live-instance
        bookkeeping: at this point nothing can own a cache dir yet.
        """
        from starlette.testclient import TestClient

        import solar_host.main as main_module

        cache_root = Path(_isolated_env / "prompt-cache")
        orphan = cache_root / f"deepseek-v4-flash-{_UUID_A}"
        orphan.mkdir(parents=True)
        still_present: list[bool] = []
        real_init_clients = main_module.init_clients

        def _recording_init_clients(settings_):
            still_present.append(orphan.exists())
            return real_init_clients(settings_)

        monkeypatch.setattr("solar_host.main.init_clients", _recording_init_clients)

        with TestClient(main_module.app, raise_server_exceptions=True):
            assert still_present == [False]
