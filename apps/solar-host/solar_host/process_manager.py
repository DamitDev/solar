"""Process manager for solar-host with multi-backend support."""

import asyncio
import logging
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solar_host.backends.base import BackendRunner
from solar_host.backends.huggingface import HuggingFaceRunner
from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.config import config_manager, parse_instance_config, settings
from solar_host.models import (
    BackendType,
    GenerationMetrics,
    Instance,
    InstancePriority,
    InstanceRuntimeState,
    InstanceStateEvent,
    InstanceStatus,
    LogMessage,
)
from solar_host.ws_client import (
    broadcast_instance_state_batch,
    broadcast_instances_update,
    broadcast_log_batch,
    get_clients,
)

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 0.1
WATCHDOG_INTERVAL_S = 15.0
MAX_QUEUE_SIZE = 10_000
_HAS_STDBUF = shutil.which("stdbuf") is not None


def get_runner_for_config(config) -> BackendRunner:
    """Get the appropriate backend runner for a config type."""
    backend_type = getattr(config, "backend_type", "llamacpp")

    if backend_type == BackendType.LLAMACPP or backend_type == "llamacpp":
        return LlamaCppRunner()
    elif backend_type in (
        BackendType.HUGGINGFACE_CAUSAL,
        BackendType.HUGGINGFACE_CLASSIFICATION,
        BackendType.HUGGINGFACE_EMBEDDING,
        BackendType.HUGGINGFACE_VISION,
        "huggingface_causal",
        "huggingface_classification",
        "huggingface_embedding",
        "huggingface_vision",
    ):
        return HuggingFaceRunner()
    else:
        # Default to llama.cpp for backward compatibility
        return LlamaCppRunner()


class ProcessManager:
    """Manages model server processes across multiple backends."""

    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
        self.log_buffers: dict[str, deque] = {}
        self.log_sequences: dict[str, int] = {}
        self.log_threads: dict[str, threading.Thread] = {}
        self.log_dir = Path(settings.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Runtime state streaming (ephemeral)
        self.state_buffers: dict[str, deque] = {}
        self.state_sequences: dict[str, int] = {}

        # Per-instance parsing context (managed by backend runners)
        self.instance_contexts: dict[str, dict[str, Any]] = {}

        # Per-instance runner reference
        self.instance_runners: dict[str, BackendRunner] = {}

        # Batched emission queues (thread-safe, drained by _flush_loop).
        # Bounded to prevent OOM if the flush loop or WS broadcast stalls.
        self._log_queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._state_queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._flush_task: asyncio.Task | None = None
        self._child_exit_lock = threading.Lock()

        # C2: log buffers of dead instances are retained (keep_logs=True from
        # _handle_child_exit / stop_instance) so the logs that explain a start
        # failure survive the process death. Ordered registry of retained ids
        # so the oldest buffers beyond settings.retained_log_buffers can be
        # evicted — each buffer is maxlen-bounded, so the worst case is
        # bounded at retained_log_buffers * log_buffer_size lines.
        self._retained_log_ids: OrderedDict[str, None] = OrderedDict()
        # Last child exit code per instance (C2 structured failure payload).
        self.last_exit_codes: dict[str, int] = {}

        # Readiness signalling: a start attempt parks on an asyncio.Event
        # that the log thread sets when the backend logs its ready line.
        self.ready_events: dict[str, asyncio.Event] = {}
        self._ready_loop: asyncio.AbstractEventLoop | None = None

    def _is_port_available(self, port: int) -> bool:
        """Check if a port is available (not bound by any process)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    def _get_assigned_ports(self) -> set:
        """Get ports assigned to currently active instances.

        Counts both RUNNING and STARTING instances: with log-gated
        readiness an instance can stay ``starting`` for minutes while the
        model loads, and its port is already reserved for it.
        """
        assigned = set()
        for instance in config_manager.get_all_instances():
            if instance.port is not None and instance.status in (
                InstanceStatus.RUNNING,
                InstanceStatus.STARTING,
            ):
                assigned.add(instance.port)
        return assigned

    def _get_available_port(self) -> int:
        """Get the lowest available port starting from settings.start_port.

        Finds the first port (starting from start_port) that is:
        1. Not assigned to a currently running instance
        2. Not currently bound by any process
        """
        assigned_ports = self._get_assigned_ports()
        port = settings.start_port

        while port in assigned_ports or not self._is_port_available(port):
            port += 1

        return port

    def _purge_instance_resources(
        self,
        instance_id: str,
        *,
        call_runner_on_stop: bool = True,
        keep_logs: bool = False,
    ) -> None:
        """Remove in-memory runners, buffers, and threads for an instance.

        With *keep_logs* the log buffer and sequence survive (C2): the
        logs that explain a start failure are exactly what the user needs
        at the moment of failure, and the retained buffers are bounded by
        ``settings.retained_log_buffers`` via :meth:`_retain_dead_logs`.
        """
        runner = self.instance_runners.get(instance_id)
        if runner and call_runner_on_stop:
            context = self.instance_contexts.get(instance_id, {})
            runner.on_process_stopped(instance_id, context)
        self.instance_runners.pop(instance_id, None)
        self.instance_contexts.pop(instance_id, None)
        if not keep_logs:
            self.log_buffers.pop(instance_id, None)
            self.log_sequences.pop(instance_id, None)
        else:
            self._retain_dead_logs(instance_id)
        self.log_threads.pop(instance_id, None)
        self.state_buffers.pop(instance_id, None)
        self.state_sequences.pop(instance_id, None)
        self.ready_events.pop(instance_id, None)

    def _retain_dead_logs(self, instance_id: str) -> None:
        """Register a retained dead-instance log buffer, evicting the oldest."""
        self._retained_log_ids.pop(instance_id, None)
        self._retained_log_ids[instance_id] = None
        for candidate in list(self._retained_log_ids):
            if len(self._retained_log_ids) <= settings.retained_log_buffers:
                break
            # A stop/start cycle leaves the id registered here while the
            # instance runs again. Dropping a live buffer would also reset
            # log_sequences to 0 on the next line, breaking the
            # seq:timestamp dedup the webui LogViewer relies on.
            if candidate in self.processes:
                continue
            self._retained_log_ids.pop(candidate, None)
            self.log_buffers.pop(candidate, None)
            self.log_sequences.pop(candidate, None)

    def _handle_child_exit(self, instance_id: str, process: subprocess.Popen) -> None:
        """Mark instance FAILED when the child process exits unexpectedly.

        Idempotent: safe from log thread EOF and watchdog; skips intentional stops.
        """
        with self._child_exit_lock:
            instance = config_manager.get_instance(instance_id)
            if not instance:
                self.processes.pop(instance_id, None)
                # A concurrent delete removed the instance: wake any
                # parked awaiter so it re-reads instead of timing out.
                self._signal_ready(instance_id)
                return

            if instance.status not in (
                InstanceStatus.RUNNING,
                InstanceStatus.STARTING,
            ):
                self.processes.pop(instance_id, None)
                # Intentional stop (STOPPING/STOPPED) while a start is
                # parked: wake the awaiter so it re-reads the stopped
                # status instead of burning the whole ready timeout.
                self._signal_ready(instance_id)
                return

            tracked = self.processes.get(instance_id)
            if tracked is not process:
                return

            exit_code = process.poll()
            if exit_code is None:
                return

            del self.processes[instance_id]
            self.last_exit_codes[instance_id] = exit_code

            instance.status = InstanceStatus.FAILED
            instance.error_message = (
                f"Process exited unexpectedly (exit code: {exit_code})"
            )
            instance.pid = None
            instance.started_at = None
            config_manager.update_instance(instance_id, instance)

            # Wake an awaiter parked in _try_start_instance so it re-reads
            # the failed status instead of burning the whole ready timeout.
            # MUST happen before the purge: _purge_instance_resources pops
            # the ready event, which would lose the wake (the start would
            # then report the timeout message instead of the exit reason).
            self._signal_ready(instance_id)

            # Keep the log buffer: the exact lines that explain the exit are
            # the ones the user needs (C2). delete_instance purges it.
            self._purge_instance_resources(
                instance_id, call_runner_on_stop=True, keep_logs=True
            )
            self._push_instances_update()

    def _signal_ready(self, instance_id: str) -> None:
        """Wake an awaiter parked in _try_start_instance (thread-safe)."""
        event = self.ready_events.get(instance_id)
        if event is None:
            return
        loop = self._ready_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()

    def _mark_instance_ready(self, instance_id: str, process: subprocess.Popen) -> None:
        """Promote an instance from ``starting`` to ``running`` (thread-safe).

        Called from the log-reader thread when the backend logs its ready
        line; the log thread is the single authority for this transition.
        Guarded by the child-exit lock so it cannot race a concurrent exit
        handling. Signals the ready event either way so an awaiter parked
        in ``_try_start_instance`` wakes with the current status.
        """
        with self._child_exit_lock:
            instance = config_manager.get_instance(instance_id)
            if instance and instance.status == InstanceStatus.STARTING:
                tracked = self.processes.get(instance_id)
                if tracked is process:
                    instance.status = InstanceStatus.RUNNING
                    instance.pid = process.pid
                    instance.started_at = datetime.now(UTC)
                    instance.retry_count = 0
                    config_manager.update_instance(instance_id, instance)

                    self.state_buffers[instance_id] = deque(
                        maxlen=settings.log_buffer_size
                    )
                    self.state_sequences[instance_id] = 0
                    config_manager.update_instance_runtime(
                        instance_id, busy=False, prefill_progress=None, active_slots=0
                    )

                    runner = self.instance_runners.get(instance_id)
                    if runner is not None:
                        runner.on_process_started(
                            instance_id, self.instance_contexts.get(instance_id, {})
                        )

                    self._push_instances_update()

        self._signal_ready(instance_id)

    def _read_logs(
        self,
        instance_id: str,
        process: subprocess.Popen,
        log_file: Path,
        runner: BackendRunner,
    ):
        """Read logs from process and store in buffer."""
        if not process.stdout:
            return

        try:
            with open(log_file, "a") as f:
                for line in iter(process.stdout.readline, b""):
                    if not line:
                        break

                    decoded_line = line.decode("utf-8", errors="replace").rstrip()

                    # Write to file
                    f.write(decoded_line + "\n")
                    f.flush()

                    # Store in buffer
                    if instance_id not in self.log_buffers:
                        self.log_buffers[instance_id] = deque(
                            maxlen=settings.log_buffer_size
                        )
                        self.log_sequences[instance_id] = 0

                    seq = self.log_sequences[instance_id]
                    self.log_sequences[instance_id] += 1

                    timestamp = datetime.now(UTC).isoformat()
                    log_msg = LogMessage(
                        seq=seq, timestamp=timestamp, line=decoded_line
                    )
                    self.log_buffers[instance_id].append(log_msg)

                    # Push log to solar-control via WebSocket
                    self._push_log_event(instance_id, seq, decoded_line, timestamp)

                    # Promote starting -> running once the backend logs that
                    # it is accepting requests. The log thread is the single
                    # authority for this transition; a live process is not
                    # proof that the model is loaded.
                    instance = config_manager.get_instance(instance_id)
                    if (
                        instance is not None
                        and instance.status == InstanceStatus.STARTING
                        and runner.is_ready_line(decoded_line)
                    ):
                        self._mark_instance_ready(instance_id, process)

                    # Parse log line using backend runner
                    try:
                        context = self.instance_contexts.get(instance_id, {})
                        state_update = runner.parse_log_line(
                            instance_id, decoded_line, context
                        )
                        if state_update:
                            self._emit_state_event(instance_id, state_update)
                    except Exception:  # noqa: S110, BLE001
                        # Parsing errors should not break logging
                        pass
        except Exception as e:  # noqa: BLE001
            logger.warning("Error reading logs for %s: %s", instance_id, e)
        finally:
            # stdout closed or reader error: child likely exited — reconcile state
            self._handle_child_exit(instance_id, process)

    def ensure_flush_loop(self, loop: asyncio.AbstractEventLoop):
        """Start the batched emission flush loop on the given event loop.

        Called once from main.py after the event loop is running.
        """
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.run_coroutine_threadsafe(
                self._flush_loop(), loop
            )

    async def _flush_loop(self):
        """Periodically drain queued log/state events and emit as batches."""
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_S)
                await self._flush_pending()
            except asyncio.CancelledError:
                await self._flush_pending()
                break
            except Exception:
                logger.exception("Error in flush loop")
                await asyncio.sleep(1)

    async def _flush_pending(self):
        """Drain both queues and emit batched events."""
        log_entries: list[dict] = []
        while True:
            try:
                log_entries.append(self._log_queue.get_nowait())
            except queue.Empty:
                break

        latest_states: dict[str, dict] = {}
        while True:
            try:
                entry = self._state_queue.get_nowait()
                latest_states[entry["instance_id"]] = entry
            except queue.Empty:
                break

        if log_entries:
            await broadcast_log_batch(log_entries)

        if latest_states:
            await broadcast_instance_state_batch(list(latest_states.values()))

    async def watchdog_loop(self):
        """Periodically poll child processes; mark FAILED if any exited unexpectedly."""
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                for instance_id, process in list(self.processes.items()):
                    if process.poll() is not None:
                        await asyncio.to_thread(
                            self._handle_child_exit, instance_id, process
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in watchdog loop")
                await asyncio.sleep(1)

    def _push_log_event(self, instance_id: str, seq: int, line: str, timestamp: str):
        """Queue a log event for batched emission (thread-safe, non-blocking).

        Silently discards events when the queue is full to prevent OOM.
        """
        try:
            self._log_queue.put_nowait(
                {
                    "instance_id": instance_id,
                    "seq": seq,
                    "line": line,
                    "timestamp": timestamp,
                }
            )
        except queue.Full:
            pass

    def _emit_state_event(self, instance_id: str, update):
        """Emit a state event from a RuntimeStateUpdate."""
        # Update in-memory instance runtime fields
        config_manager.update_instance_runtime(
            instance_id,
            busy=update.busy,
            prefill_progress=update.prefill_progress,
            active_slots=update.active_slots,
        )

        # Initialize state buffer/seq lazily
        if instance_id not in self.state_buffers:
            self.state_buffers[instance_id] = deque(maxlen=settings.log_buffer_size)
            self.state_sequences[instance_id] = 0

        seq = self.state_sequences[instance_id]
        self.state_sequences[instance_id] += 1

        now_ts = datetime.now(UTC).isoformat()
        state = InstanceRuntimeState(
            instance_id=instance_id,
            busy=update.busy,
            phase=update.phase,
            prefill_progress=update.prefill_progress,
            active_slots=update.active_slots,
            slot_id=update.slot_id,
            task_id=update.task_id,
            prefill_prompt_tokens=update.prefill_prompt_tokens,
            generated_tokens=update.generated_tokens,
            decode_tps=update.decode_tps,
            decode_ms_per_token=update.decode_ms_per_token,
            checkpoint_index=update.checkpoint_index,
            checkpoint_total=update.checkpoint_total,
            timestamp=now_ts,
        )
        event = InstanceStateEvent(
            seq=seq,
            timestamp=now_ts,
            data=state,
        )
        self.state_buffers[instance_id].append(event)

        # Push state to solar-control via WebSocket
        self._push_state_event(instance_id, state)

    def _push_state_event(self, instance_id: str, state: InstanceRuntimeState):
        """Queue a state event for batched emission (thread-safe, non-blocking).

        Silently discards events when the queue is full to prevent OOM.
        """
        try:
            self._state_queue.put_nowait(
                {
                    "instance_id": instance_id,
                    "timestamp": state.timestamp,
                    "data": {
                        "busy": state.busy,
                        "phase": state.phase.value if state.phase else None,
                        "prefill_progress": state.prefill_progress,
                        "active_slots": state.active_slots,
                        "slot_id": state.slot_id,
                        "task_id": state.task_id,
                        "prefill_prompt_tokens": state.prefill_prompt_tokens,
                        "generated_tokens": state.generated_tokens,
                        "decode_tps": state.decode_tps,
                        "decode_ms_per_token": state.decode_ms_per_token,
                        "checkpoint_index": state.checkpoint_index,
                        "checkpoint_total": state.checkpoint_total,
                    },
                }
            )
        except queue.Full:
            pass

    def get_last_generation(self, instance_id: str) -> GenerationMetrics | None:
        """Get the last generation metrics for an instance."""
        runner = self.instance_runners.get(instance_id)
        context = self.instance_contexts.get(instance_id, {})

        if runner and hasattr(runner, "get_last_generation"):
            return runner.get_last_generation(context)
        return None

    async def start_instance(self, instance_id: str) -> bool:
        """Start a model server instance with iterative retry."""
        for attempt in range(1 + settings.max_retries):
            result = await self._try_start_instance(instance_id, attempt)
            if result is not None:
                return result
            # result is None means "retry"
            await asyncio.sleep(1)
        return False

    async def _try_start_instance(self, instance_id: str, attempt: int) -> bool | None:
        """Single start attempt. Returns True/False for final result, None to retry."""
        instance = config_manager.get_instance(instance_id)
        if not instance:
            return False

        # Check if already running or starting (verify subprocess is alive).
        # A start blocks while the server comes up, so a caller that gave up
        # waiting (a client timeout) and retried must not launch a second
        # process for the same instance: the first one would be orphaned,
        # still holding its port and its share of the GPU.
        if instance.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING):
            proc = self.processes.get(instance_id)
            if proc and proc.poll() is None:
                return True
            # Stale status: process missing or dead -- reset and continue start
            if proc is not None:
                self.processes.pop(instance_id, None)
            self._purge_instance_resources(instance_id, call_runner_on_stop=True)
            instance.status = InstanceStatus.STOPPED
            instance.pid = None
            instance.started_at = None
            instance.error_message = None
            config_manager.update_instance(instance_id, instance)

        instance.port = self._get_available_port()

        # A readiness timeout records no exit code of its own, so a stale one
        # from an earlier attempt would be reported as this failure's cause
        # (C2).
        self.last_exit_codes.pop(instance_id, None)
        # On a retry, drop the previous attempt's lines so log_tail describes
        # the attempt that actually failed. Only on a retry: a first attempt
        # after a stop would otherwise discard the retained buffer the user
        # may still be reading.
        if attempt > 0:
            self.log_buffers.pop(instance_id, None)
            self.log_sequences.pop(instance_id, None)

        runner = get_runner_for_config(instance.config)
        self.instance_runners[instance_id] = runner
        self.instance_contexts[instance_id] = runner.initialize_context()

        instance.status = InstanceStatus.STARTING
        instance.error_message = None

        if hasattr(runner, "get_supported_endpoints_for_model_type"):
            model_type = getattr(instance.config, "model_type", "llm")
            instance.supported_endpoints = (
                runner.get_supported_endpoints_for_model_type(model_type)
            )
        elif hasattr(runner, "get_supported_endpoints_for_type"):
            backend_type = getattr(instance.config, "backend_type", "llamacpp")
            instance.supported_endpoints = runner.get_supported_endpoints_for_type(
                backend_type
            )
        else:
            instance.supported_endpoints = runner.get_supported_endpoints()

        config_manager.update_instance(instance_id, instance)

        # Park on the ready event. The log thread promotes
        # starting -> running when the backend logs its ready line and
        # wakes us; a dying process also wakes us (via _handle_child_exit)
        # so the failure is re-read immediately instead of burning the
        # whole timeout. Registered before the spawn so a fast exit cannot
        # signal a not-yet-registered event.
        self._ready_loop = asyncio.get_running_loop()
        ready_event = asyncio.Event()
        self.ready_events[instance_id] = ready_event

        try:
            cmd = runner.build_command(instance)

            alias_safe = instance.config.alias.replace(":", "-").replace("/", "-")
            # Instance-addressable log file (C2): the id in the name makes
            # the file findable after the instance record is gone.
            log_file = self.log_dir / (
                f"{alias_safe}_{instance_id}_{int(time.time())}.log"
            )

            run_env = os.environ.copy()
            run_env["PYTHONUNBUFFERED"] = "1"
            run_cmd = ["stdbuf", "-oL"] + cmd if _HAS_STDBUF else cmd

            process = subprocess.Popen(  # noqa: ASYNC220 — spawn is non-blocking; logs are drained by a dedicated thread (_read_logs), never in the event loop
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=run_env,
            )

            self.processes[instance_id] = process
            # No longer a dead instance: holding a retention slot would let a
            # later eviction drop this live instance's log buffer. A failure
            # from here on re-retains it via _purge_instance_resources.
            self._retained_log_ids.pop(instance_id, None)

            log_thread = threading.Thread(
                target=self._read_logs,
                args=(instance_id, process, log_file, runner),
                daemon=True,
            )
            log_thread.start()
            self.log_threads[instance_id] = log_thread

            try:
                await asyncio.wait_for(
                    ready_event.wait(),
                    timeout=settings.instance_ready_timeout_s,
                )
            except TimeoutError:
                # The backend never reported readiness — kill it and fail.
                proc = self.processes.pop(instance_id, None)
                exit_code: int | None = None
                if proc is not None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    try:
                        exit_code = await asyncio.to_thread(proc.wait, 10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        exit_code = await asyncio.to_thread(proc.wait)
                if exit_code is not None:
                    # Popping the process above stops _handle_child_exit from
                    # claiming this one, so without recording it here the
                    # start-failure payload reports exit_code: null (C2). The
                    # value is negative — killed by our own signal, not a crash
                    # of the backend's own making — which is exactly what
                    # distinguishes a readiness timeout from a failed start.
                    self.last_exit_codes[instance_id] = exit_code

                instance = config_manager.get_instance(instance_id)
                if instance is None:
                    return False
                instance.status = InstanceStatus.FAILED
                instance.error_message = (
                    f"Backend did not report readiness within "
                    f"{settings.instance_ready_timeout_s:.0f}s"
                )
                instance.retry_count = attempt + 1
                config_manager.update_instance(instance_id, instance)

                if instance.retry_count < settings.max_retries:
                    return None  # signal retry
                return False
            finally:
                self.ready_events.pop(instance_id, None)

            # Re-read: the log thread may have promoted us to running, or
            # the child may have died while we waited.
            instance = config_manager.get_instance(instance_id)
            if not instance:
                return False
            if instance.status == InstanceStatus.RUNNING:
                return True
            if instance.status == InstanceStatus.FAILED:
                instance.retry_count = attempt + 1
                config_manager.update_instance(instance_id, instance)
                if instance.retry_count < settings.max_retries:
                    return None  # signal retry
                return False
            # Stopped/stopping from a concurrent stop, or any other state.
            return False

        except Exception as e:  # noqa: BLE001
            self.ready_events.pop(instance_id, None)
            instance = config_manager.get_instance(instance_id)
            if instance:
                instance.status = InstanceStatus.FAILED
                instance.error_message = str(e)
                instance.retry_count = attempt + 1
                config_manager.update_instance(instance_id, instance)

                if instance.retry_count < settings.max_retries:
                    return None  # signal retry
            return False

    async def stop_instance(self, instance_id: str) -> bool:
        """Stop a model server instance."""
        instance = config_manager.get_instance(instance_id)
        if not instance:
            return False

        if instance.status == InstanceStatus.STOPPED:
            return True

        # For failed instances, just transition to stopped cleanly
        if instance.status == InstanceStatus.FAILED:
            instance.status = InstanceStatus.STOPPED
            instance.pid = None
            instance.started_at = None
            instance.error_message = None
            config_manager.update_instance(instance_id, instance)
            self._push_instances_update()
            return True

        instance.status = InstanceStatus.STOPPING
        config_manager.update_instance(instance_id, instance)

        try:
            if instance_id in self.processes:
                process = self.processes[instance_id]
                process.terminate()

                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)

                self.processes.pop(instance_id, None)

            # Wait for log thread to finish before purging resources
            log_thread = self.log_threads.get(instance_id)
            if log_thread and log_thread.is_alive():
                log_thread.join(timeout=5)

            # Keep the logs across a manual stop (C2): a stopped instance's
            # buffer stays readable until the instance is deleted.
            self._purge_instance_resources(
                instance_id, call_runner_on_stop=True, keep_logs=True
            )

            instance.status = InstanceStatus.STOPPED
            instance.pid = None
            instance.started_at = None
            config_manager.update_instance(instance_id, instance)

            await self._cleanup_old_logs(instance.config.alias)

            self._push_instances_update()

            return True

        except Exception as e:  # noqa: BLE001
            instance.status = InstanceStatus.FAILED
            instance.error_message = f"Failed to stop: {e!s}"
            config_manager.update_instance(instance_id, instance)
            return False

    async def _cleanup_old_logs(self, alias: str):
        """Clean up old log files for stopped instances (C2).

        Files older than ``settings.log_file_retention_s`` are unlinked. The
        most recent file per *(alias, instance_id)* is always kept regardless
        of age: keeping only the newest per alias would leave a multi-replica
        intent with a single post-mortem, discarding every other replica's.
        """
        try:
            alias_safe = alias.replace(":", "-").replace("/", "-")
            pattern = f"{alias_safe}_*.log"
            files: list[tuple[Path, float, str]] = []
            for log_file in self.log_dir.glob(pattern):
                try:
                    mtime = log_file.stat().st_mtime
                except OSError:
                    continue
                # {alias_safe}_{instance_id}_{ts}.log — the alias prefix is
                # known, and the timestamp is the last component, so what is
                # left in between is the instance id. Files predating the
                # instance-id naming yield "" and share one group.
                remainder = log_file.stem[len(alias_safe) + 1 :]
                instance_id = remainder.rpartition("_")[0]
                files.append((log_file, mtime, instance_id))

            newest_per_instance: dict[str, Path] = {}
            newest_mtimes: dict[str, float] = {}
            for log_file, mtime, instance_id in files:
                if mtime > newest_mtimes.get(instance_id, -1.0):
                    newest_per_instance[instance_id] = log_file
                    newest_mtimes[instance_id] = mtime

            cutoff = time.time() - settings.log_file_retention_s
            for log_file, mtime, instance_id in files:
                # Path equality by value: glob() yields fresh Path objects on
                # every call, so identity comparison would never match.
                if log_file == newest_per_instance.get(instance_id):
                    continue
                if mtime >= cutoff:
                    continue
                try:
                    log_file.unlink()
                except OSError:
                    continue
        except Exception as e:  # noqa: BLE001
            logger.warning("Error cleaning up logs: %s", e)

    async def restart_instance(self, instance_id: str) -> bool:
        """Restart a model server instance."""
        stopped = await self.stop_instance(instance_id)
        if not stopped:
            instance = config_manager.get_instance(instance_id)
            if instance and instance.status in (
                InstanceStatus.RUNNING,
                InstanceStatus.STARTING,
            ):
                logger.error(
                    "Cannot restart %s: stop failed and instance is still active",
                    instance_id,
                )
                return False
        await asyncio.sleep(1)
        return await self.start_instance(instance_id)

    def create_instance(
        self,
        config,
        priority: str | None = None,
        managed_by: str | None = None,
        intent_id: str | None = None,
    ) -> Instance:
        """Create a new instance."""
        # Parse config if it's a dict (from FastAPI request body)
        if isinstance(config, dict):
            config = parse_instance_config(config)

        instance_id = str(uuid.uuid4())

        runner = get_runner_for_config(config)

        if hasattr(runner, "get_supported_endpoints_for_model_type"):
            model_type = getattr(config, "model_type", "llm")
            supported_endpoints = runner.get_supported_endpoints_for_model_type(
                model_type
            )
        elif hasattr(runner, "get_supported_endpoints_for_type"):
            backend_type = getattr(config, "backend_type", "llamacpp")
            supported_endpoints = runner.get_supported_endpoints_for_type(backend_type)
        else:
            supported_endpoints = runner.get_supported_endpoints()

        instance = Instance(
            id=instance_id,
            config=config,
            status=InstanceStatus.STOPPED,
            supported_endpoints=supported_endpoints,
            priority=(
                InstancePriority(priority) if priority else InstancePriority.PRODUCTION
            ),
            managed_by=managed_by,
            intent_id=intent_id,
        )
        config_manager.add_instance(instance)

        # Notify solar-control of instance update
        self._push_instances_update()

        return instance

    def get_log_buffer(self, instance_id: str) -> list[LogMessage]:
        """Get log buffer for an instance."""
        if instance_id in self.log_buffers:
            return list(self.log_buffers[instance_id])
        return []

    def get_last_exit_code(self, instance_id: str) -> int | None:
        """Return the last recorded child exit code for *instance_id* (C2).

        Set by :meth:`_handle_child_exit`; cleared by ``delete_instance``.
        """
        return self.last_exit_codes.get(instance_id)

    def get_next_sequence(self, instance_id: str) -> int:
        """Get next sequence number for an instance."""
        return self.log_sequences.get(instance_id, 0)

    def get_state_buffer(self, instance_id: str) -> list[InstanceStateEvent]:
        """Get state buffer for an instance."""
        if instance_id in self.state_buffers:
            return list(self.state_buffers[instance_id])
        return []

    def get_state_next_sequence(self, instance_id: str) -> int:
        """Get next state sequence number for an instance."""
        return self.state_sequences.get(instance_id, 0)

    def _push_instances_update(self):
        """Push instance list update to all connected solar-controls (thread-safe)."""
        try:
            clients = get_clients()
            for client in clients:
                if client.is_connected:
                    loop = getattr(client, "_main_loop", None)
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            broadcast_instances_update(), loop
                        )
                        break
        except Exception:
            logger.debug(
                "Failed to push instances update to solar-control", exc_info=True
            )

    def delete_instance(self, instance_id: str) -> bool:
        """Delete an instance and notify solar-control."""
        instance = config_manager.get_instance(instance_id)
        if not instance:
            return False

        # Defensively terminate any lingering process
        process = self.processes.pop(instance_id, None)
        if process and process.poll() is None:
            logger.warning(
                "Terminating orphan process for deleted instance %s", instance_id
            )
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                process.kill()
                process.wait()

        config_manager.remove_instance(instance_id)

        # Wait for log thread to finish before removing buffers
        log_thread = self.log_threads.pop(instance_id, None)
        if log_thread and log_thread.is_alive():
            log_thread.join(timeout=3)

        self._purge_instance_resources(instance_id, call_runner_on_stop=False)
        # Delete is the one operation that genuinely discards the logs
        # (C2): the retained buffer registry and exit-code record go too.
        self._retained_log_ids.pop(instance_id, None)
        self.last_exit_codes.pop(instance_id, None)

        self._push_instances_update()

        return True

    async def auto_restart_running_instances(self):
        """Auto-restart instances that were running before shutdown.
        Also resolves intermediate states (starting/stopping) left over
        from an interrupted shutdown.
        """
        for instance in config_manager.get_all_instances():
            if instance.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING):
                logger.info(
                    "Auto-restarting instance: %s (%s)",
                    instance.id,
                    instance.config.alias,
                )
                self._kill_stale_pid(instance.pid)
                instance.status = InstanceStatus.STOPPED
                instance.pid = None
                config_manager.update_instance(instance.id, instance)
                await self.start_instance(instance.id)
            elif instance.status == InstanceStatus.STOPPING:
                logger.info(
                    "Resolving interrupted stop for instance: %s (%s)",
                    instance.id,
                    instance.config.alias,
                )
                self._kill_stale_pid(instance.pid)
                instance.status = InstanceStatus.STOPPED
                instance.pid = None
                config_manager.update_instance(instance.id, instance)

    @staticmethod
    def _kill_stale_pid(pid: int | None) -> None:
        """Best-effort kill of a stale PID left from a previous run."""
        if pid is None:
            return
        try:
            import signal

            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to stale PID %d", pid)
        except ProcessLookupError:
            pass
        except OSError as e:
            logger.debug("Could not kill stale PID %d: %s", pid, e)


# Global process manager instance
process_manager = ProcessManager()
