"""Log-gated instance readiness (starting -> running on the ready banner).

The lifecycle status must only move to ``running`` when the backend logs
that it is listening — a live process is not evidence that the model is
loaded. These tests cover the runner contracts (regex tables) and the
ProcessManager lifecycle against real subprocesses.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from solar_host.backends.huggingface import HuggingFaceRunner
from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.backends.sglang import SglangRunner
from solar_host.config import config_manager, settings
from solar_host.models.base import Instance, InstancePhase, InstanceStatus
from solar_host.models.llamacpp import LlamaCppConfig
from solar_host.process_manager import ProcessManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point settings and the global config manager at a tmp workspace."""
    monkeypatch.setattr(
        "solar_host.config.settings.config_file", str(tmp_path / "config.json")
    )
    monkeypatch.setattr("solar_host.config.settings.log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr("solar_host.config.settings.api_key", "test-key")
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 5.0)
    config_manager.config_file = tmp_path / "config.json"
    config_manager.instances = {}
    return tmp_path


def _make_instance(
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


# ---------------------------------------------------------------------------
# Runner readiness contracts
# ---------------------------------------------------------------------------


class TestLlamaCppReadyLine:
    @pytest.mark.parametrize(
        "line",
        [
            "llama_server: listening on http://0.0.0.0:8080",
            "main: server is listening on http://0.0.0.0:8080 - starting the main loop",
            "main: server is listening on http://127.0.0.1:9001",
            "llama server: listening on https://0.0.0.0:8443",
        ],
    )
    def test_accepts_listening_banners(self, line):
        assert LlamaCppRunner().is_ready_line(line)

    @pytest.mark.parametrize(
        "line",
        [
            "load_model: loading model",
            "slot launch_slot_: id 0",
            "this process is listening carefully",
            "main: starting the main loop",
        ],
    )
    def test_rejects_non_readiness(self, line):
        assert not LlamaCppRunner().is_ready_line(line)


class TestHuggingFaceReadyLine:
    def test_accepts_uvicorn_running(self):
        line = "INFO:     Uvicorn running on http://0.0.0.0:8100 (Press CTRL+C to quit)"
        assert HuggingFaceRunner().is_ready_line(line)

    @pytest.mark.parametrize(
        "line",
        [
            "INFO:     Application startup complete.",
            "INFO:     Started server process [123]",
            "INFO:     Waiting for application startup.",
        ],
    )
    def test_rejects_non_listening_banners(self, line):
        assert not HuggingFaceRunner().is_ready_line(line)


class TestSglangReadyLine:
    @pytest.mark.parametrize(
        "line",
        [
            "[2026-08-14 12:00:00] The server is fired up and ready to roll!",
            "INFO:     Uvicorn running on http://0.0.0.0:8100 (Press CTRL+C to quit)",
        ],
    )
    def test_accepts_the_warmup_banner_and_uvicorn(self, line):
        assert SglangRunner().is_ready_line(line)

    @pytest.mark.parametrize(
        "line",
        [
            "[2026-08-14 12:00:00] Load weight end. type=DeepseekV3ForCausalLM",
            "[2026-08-14 12:00:00] Capture cuda graph begin.",
            "[2026-08-14 12:00:00] Prefill batch. #new-seq: 1, #new-token: 512",
        ],
    )
    def test_rejects_startup_progress(self, line):
        assert not SglangRunner().is_ready_line(line)


class TestSglangLogParsing:
    def test_a_prefill_batch_marks_the_instance_busy(self):
        runner = SglangRunner()
        context = runner.initialize_context()

        update = runner.parse_log_line(
            "inst-1",
            "[2026-08-14 12:00:00] Prefill batch. #new-seq: 1, #new-token: 512, "
            "#cached-token: 0, #running-req: 1",
            context,
        )

        assert update is not None
        assert update.busy is True
        assert update.phase == InstancePhase.PREFILL

    def test_decode_throughput_is_reported(self):
        runner = SglangRunner()
        context = runner.initialize_context()

        update = runner.parse_log_line(
            "inst-1",
            "[2026-08-14 12:00:01] Decode batch. #running-req: 2, "
            "gen throughput (token/s): 84.21, #queue-req: 0",
            context,
        )

        assert update is not None
        assert update.busy is True
        assert update.phase == InstancePhase.GENERATING
        assert update.decode_tps == pytest.approx(84.21)
        assert runner.get_last_generation(context).decode_tps == pytest.approx(84.21)

    def test_a_drained_queue_goes_idle(self):
        runner = SglangRunner()
        context = runner.initialize_context()
        runner.parse_log_line(
            "inst-1",
            "[2026-08-14 12:00:01] Decode batch. #running-req: 2, "
            "gen throughput (token/s): 84.21",
            context,
        )

        update = runner.parse_log_line(
            "inst-1",
            "[2026-08-14 12:00:02] Decode batch. #running-req: 0, "
            "gen throughput (token/s): 0.00",
            context,
        )

        assert update is not None
        assert update.busy is False
        assert update.phase == InstancePhase.IDLE

    def test_unrelated_lines_produce_no_update(self):
        runner = SglangRunner()
        context = runner.initialize_context()

        assert runner.parse_log_line("inst-1", "Load weight end.", context) is None


# ---------------------------------------------------------------------------
# Lifecycle: real subprocess driven by a script runner
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_promotes_on_ready_line(_isolated_env, monkeypatch):
    """Sleeps, prints the ready line, then keeps running."""
    manager = ProcessManager()
    _make_instance()
    runner = _ScriptRunner(
        "import time; time.sleep(1); print('READY_MARKER', flush=True); time.sleep(30)"
    )
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
    )
    pushed = MagicMock()
    monkeypatch.setattr(manager, "_push_instances_update", pushed)

    task = asyncio.create_task(manager._try_start_instance("inst-1", attempt=0))

    # Mid-flight: spawned but the backend has not reported readiness yet.
    await asyncio.sleep(0.3)
    inst = config_manager.get_instance("inst-1")
    assert inst.status == InstanceStatus.STARTING
    assert "inst-1" in manager.processes

    result = await task
    assert result is True

    inst = config_manager.get_instance("inst-1")
    assert inst is not None
    assert inst.status == InstanceStatus.RUNNING
    assert inst.pid is not None
    assert inst.started_at is not None
    assert inst.retry_count == 0
    # Exactly one promotion: the ready line was the single authority.
    pushed.assert_called_once()

    # Clean up the still-sleeping child.
    proc = manager.processes.pop("inst-1", None)
    if proc is not None:
        proc.kill()


@pytest.mark.anyio
async def test_start_fails_when_backend_never_reports_ready(_isolated_env, monkeypatch):
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 1.0)
    manager = ProcessManager()
    _make_instance()
    runner = _ScriptRunner("import time; time.sleep(60)")
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
    )

    result = await manager.start_instance("inst-1")

    assert result is False
    inst = config_manager.get_instance("inst-1")
    assert inst is not None
    assert inst.status == InstanceStatus.FAILED
    assert "readiness" in (inst.error_message or "")
    assert "inst-1" not in manager.processes  # the timed-out process was killed
    assert inst.retry_count == settings.max_retries  # retry accounting honoured


@pytest.mark.anyio
async def test_start_fails_on_immediate_exit(_isolated_env, monkeypatch):
    """A process that dies instantly fails with the same retry accounting."""
    manager = ProcessManager()
    _make_instance()
    runner = _ScriptRunner("import sys; sys.exit(3)")
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
    )

    result = await manager.start_instance("inst-1")

    assert result is False
    inst = config_manager.get_instance("inst-1")
    assert inst is not None
    assert inst.status == InstanceStatus.FAILED
    assert inst.retry_count == settings.max_retries


@pytest.mark.anyio
async def test_start_aborts_when_instance_deleted_mid_flight(
    _isolated_env, monkeypatch
):
    """A record removed under a parked start ends the start in ~a second.

    This must hold with no wake at all: ``_signal_ready`` looks the event up
    in ``ready_events``, which a concurrent stop or delete purges, so the wake
    is lost whenever it is raised in that window. solar-control awaits the
    start one reconciler action at a time, so a start that instead parks for
    the full readiness timeout stalls every intent on the fleet — observed as
    a 600 s reconciler stall in the integration suite.
    """
    monkeypatch.setattr("solar_host.config.settings.instance_ready_timeout_s", 30.0)
    manager = ProcessManager()
    _make_instance()
    runner = _ScriptRunner("import time; time.sleep(60)")  # never reports ready
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
    )

    task = asyncio.create_task(manager._try_start_instance("inst-1", attempt=0))
    await asyncio.sleep(0.3)
    inst = config_manager.get_instance("inst-1")
    assert inst is not None and inst.status == InstanceStatus.STARTING

    # Drop the record only — no stop, no signal: the lost-wake shape exactly.
    config_manager.remove_instance("inst-1")

    # Fails loudly if the start parks on the readiness timeout instead.
    result = await asyncio.wait_for(task, timeout=10.0)
    assert result is False

    proc = manager.processes.pop("inst-1", None)
    if proc is not None:
        proc.kill()


@pytest.mark.anyio
async def test_ready_line_twice_promotes_once(_isolated_env, monkeypatch):
    manager = ProcessManager()
    _make_instance()
    runner = _ScriptRunner(
        "import time; print('READY_MARKER', flush=True); "
        "print('READY_MARKER', flush=True); time.sleep(30)"
    )
    monkeypatch.setattr(
        "solar_host.process_manager.get_runner_for_config", lambda cfg: runner
    )
    pushed = MagicMock()
    monkeypatch.setattr(manager, "_push_instances_update", pushed)

    result = await manager.start_instance("inst-1")

    assert result is True
    inst = config_manager.get_instance("inst-1")
    assert inst is not None
    assert inst.status == InstanceStatus.RUNNING
    pushed.assert_called_once()  # idempotent: promotion happens once
    proc = manager.processes.pop("inst-1", None)
    if proc is not None:
        proc.kill()


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------


def test_assigned_ports_include_starting(_isolated_env):
    manager = ProcessManager()

    running = _make_instance(instance_id="inst-running")
    running.port = 3500
    running.status = InstanceStatus.RUNNING
    config_manager.update_instance("inst-running", running)

    starting = _make_instance(instance_id="inst-starting")
    starting.port = 3501
    starting.status = InstanceStatus.STARTING
    config_manager.update_instance("inst-starting", starting)

    stopped = _make_instance(instance_id="inst-stopped")
    stopped.port = 3502
    stopped.status = InstanceStatus.STOPPED
    config_manager.update_instance("inst-stopped", stopped)

    assert manager._get_assigned_ports() == {3500, 3501}
