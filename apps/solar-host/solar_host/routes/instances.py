import glob as globlib
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from solar_host.config import config_manager, parse_instance_config, settings
from solar_host.models import (
    GenerationMetrics,
    Instance,
    InstanceCreate,
    InstancePriority,
    InstanceResponse,
    InstanceRuntimeState,
    InstanceStatus,
    InstanceUpdate,
    InstanceUsageSnapshot,
    LogMessage,
)
from solar_host.process_manager import process_manager

router = APIRouter(prefix="/instances", tags=["instances"])

_TAIL_CHUNK_BYTES = 64 * 1024


def _mtime_or_zero(path: Path) -> float:
    """Sort key that tolerates a file unlinked between glob and stat."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """Return at most *max_lines* lines from the end of *path*.

    Reads backwards in chunks rather than loading the file: retention is 24 h,
    so a chatty instance's log can be far larger than this process's memory
    budget, and one GET must not be able to exhaust it.
    """
    if max_lines <= 0:
        return []
    chunks: list[bytes] = []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            pos = handle.tell()
            newlines = 0
            # One extra newline over the budget guarantees the (possibly
            # partial) leading line is dropped by the slice below.
            while pos > 0 and newlines <= max_lines:
                read_size = min(_TAIL_CHUNK_BYTES, pos)
                pos -= read_size
                handle.seek(pos)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
    except OSError:
        return []
    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


@router.post("", response_model=InstanceResponse)
async def create_instance(data: InstanceCreate):
    """Create a new model instance (llama.cpp or HuggingFace)"""
    # Validate priority if provided (S-036)
    VALID_PRIORITIES = {p.value for p in InstancePriority}
    if data.priority is not None and data.priority not in VALID_PRIORITIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid priority '{data.priority}'. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}",
        )

    try:
        instance = process_manager.create_instance(
            data.config,
            priority=data.priority,
            managed_by=data.managed_by,
            intent_id=data.intent_id,
        )
        # Push the new instance so solar-control's Redis cache learns about
        # it immediately (flat WS shape); otherwise the gateway's HTTP poll
        # fallback re-seeds the cache with the nested /instances shape and
        # the reconciler's view of the instance lags (D-017).
        process_manager._push_instances_update()
        return InstanceResponse(
            instance=instance, message=f"Instance {instance.id} created successfully"
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=list[Instance])
async def list_instances():
    """List all instances"""
    return config_manager.get_all_instances()


@router.get("/{instance_id}", response_model=Instance)
async def get_instance(instance_id: str):
    """Get instance details"""
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return instance


@router.put("/{instance_id}", response_model=InstanceResponse)
async def update_instance(instance_id: str, data: InstanceUpdate):
    """Update instance configuration (only when stopped)"""
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    if instance.status not in (InstanceStatus.STOPPED, InstanceStatus.FAILED):
        raise HTTPException(
            status_code=400, detail="Cannot update running instance. Stop it first."
        )

    try:
        # Parse config if it's a dict (from FastAPI request body).
        # Only apply fields explicitly present in the payload, so a
        # config-only update never clobbers ownership markers and a
        # marker-clearing update can set them to null (S-037 disown).
        if data.config is not None:
            config = data.config
            if isinstance(config, dict):
                config = parse_instance_config(config)
            instance.config = config
        if "managed_by" in data.model_fields_set:
            instance.managed_by = data.managed_by
        if "intent_id" in data.model_fields_set:
            instance.intent_id = data.intent_id
        config_manager.update_instance(instance_id, instance)
        # Push the updated instance list so solar-control's Redis cache
        # reflects the authoritative post-update state. Without this, a
        # stale instances_update (e.g. the stop event emitted before a
        # disown) can re-populate ownership markers after the disown and
        # the intent reconciler will fight the instance again (surplus
        # STOP deletes it). (D-017)
        process_manager._push_instances_update()
        return InstanceResponse(
            instance=instance, message=f"Instance {instance_id} updated successfully"
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{instance_id}", response_model=InstanceResponse)
async def delete_instance(instance_id: str):
    """Delete an instance (must be stopped first)"""
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    if instance.status not in (InstanceStatus.STOPPED, InstanceStatus.FAILED):
        raise HTTPException(
            status_code=400, detail="Cannot delete running instance. Stop it first."
        )

    try:
        # Use process_manager to delete (notifies solar-control)
        process_manager.delete_instance(instance_id)
        return InstanceResponse(
            instance=instance, message=f"Instance {instance_id} deleted successfully"
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{instance_id}/start", response_model=InstanceResponse)
async def start_instance(instance_id: str) -> InstanceResponse | JSONResponse:
    """Start an instance"""
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    success = await process_manager.start_instance(instance_id)
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found after start")

    if success:
        return InstanceResponse(
            instance=instance, message=f"Instance {instance_id} started successfully"
        )
    else:
        # Structured failure body (C2): carries the instance id, the child
        # exit code (when the process died) and the tail of the retained log
        # buffer, so the error is diagnosable without a separate logs lookup.
        #
        # Returned as a JSONResponse rather than raised as an HTTPException:
        # FastAPI nests HTTPException.detail under its own "detail" key, which
        # would bury these fields one level deeper than solar-control reads.
        exit_code = process_manager.get_last_exit_code(instance_id)
        log_tail = [m.line for m in process_manager.get_log_buffer(instance_id)][
            -settings.start_failure_log_tail_lines :
        ]
        return JSONResponse(
            status_code=500,
            content={
                "detail": f"Failed to start instance: {instance.error_message}",
                "instance_id": instance_id,
                "exit_code": exit_code,
                "log_tail": log_tail,
            },
        )


@router.post("/{instance_id}/stop", response_model=InstanceResponse)
async def stop_instance(instance_id: str):
    """Stop an instance"""
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    success = await process_manager.stop_instance(instance_id)
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found after stop")

    if success:
        return InstanceResponse(
            instance=instance, message=f"Instance {instance_id} stopped successfully"
        )
    else:
        raise HTTPException(
            status_code=500, detail=f"Failed to stop instance: {instance.error_message}"
        )


@router.post("/{instance_id}/restart", response_model=InstanceResponse)
async def restart_instance(instance_id: str):
    """Restart an instance"""
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    success = await process_manager.restart_instance(instance_id)
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found after restart")

    if success:
        return InstanceResponse(
            instance=instance, message=f"Instance {instance_id} restarted successfully"
        )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to restart instance: {instance.error_message}",
        )


@router.get("/{instance_id}/state", response_model=InstanceRuntimeState)
async def get_instance_state(instance_id: str):
    """Get ephemeral runtime state for an instance"""
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Build current snapshot (ephemeral values default to safe values)
    now_iso = (
        __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat()
    )
    return InstanceRuntimeState(
        instance_id=instance_id,
        busy=getattr(instance, "busy", False),
        prefill_progress=getattr(instance, "prefill_progress", None),
        active_slots=getattr(instance, "active_slots", 0),
        timestamp=now_iso,
    )


@router.get("/{instance_id}/logs", response_model=list[LogMessage])
async def get_instance_logs(instance_id: str):
    """Get buffered logs for an instance.

    Returns the in-memory log buffer (last N log lines). When the buffer is
    empty — e.g. the process died and the reconciler recreated the instance,
    or the instance record is gone entirely — falls back to the newest
    on-disk log file matching the instance id, so post-mortem reads still
    work (C2). 404 only when neither buffer nor file exists.
    """
    logs = process_manager.get_log_buffer(instance_id)
    if logs:
        return logs

    # File fallback: log files are named {alias}_{instance_id}_{ts}.log
    # (C2), so the file is findable after the instance record is gone. The id
    # is escaped because it reaches us from the URL and glob metacharacters
    # would otherwise change which files match.
    pattern = f"*_{globlib.escape(instance_id)}_*.log"
    try:
        files = sorted(process_manager.log_dir.glob(pattern), key=_mtime_or_zero)
    except OSError:
        files = []
    if files:
        path = files[-1]
        tail = _tail_lines(path, settings.log_buffer_size)
        # The file has no per-line timestamps; synthesize seq from the line
        # index and use the file mtime as the event timestamp (C2).
        mtime = datetime.fromtimestamp(_mtime_or_zero(path), tz=UTC).isoformat()
        return [
            LogMessage(seq=i, timestamp=mtime, line=line) for i, line in enumerate(tail)
        ]

    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return []


@router.get("/{instance_id}/last-generation", response_model=GenerationMetrics)
async def get_last_generation(
    instance_id: str, after: str | None = None, within_s: int | None = None
):
    """Return most recent finished generation metrics for the instance.

    Optional filters:
    - after: ISO8601 timestamp; only return if finished_at >= after
    - within_s: only return if finished within the last N seconds
    """
    # Validate instance
    instance = config_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    metrics = process_manager.get_last_generation(instance_id)
    if not metrics:
        raise HTTPException(status_code=404, detail="No generation metrics available")

    from datetime import datetime

    def parse_iso(ts: str | None):
        if not ts:
            return None
        return datetime.fromisoformat(ts).astimezone(UTC)

    finished_dt = parse_iso(metrics.finished_at)

    if after:
        try:
            after_dt = parse_iso(after)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400, detail=f"Invalid 'after' timestamp: {after!r}"
            )
        if after_dt and finished_dt and finished_dt < after_dt:
            raise HTTPException(
                status_code=404,
                detail="No generation metrics after the specified timestamp",
            )

    if within_s is not None and within_s >= 0:
        now_dt = datetime.now(UTC)
        # A record without a finish time cannot be proven recent: treat it
        # as outside the window instead of passing (a tokenless TPS sample
        # with finished_at=None used to slip through and 200 with nulls).
        if finished_dt is None or (now_dt - finished_dt).total_seconds() > float(
            within_s
        ):
            raise HTTPException(
                status_code=404,
                detail="No recent generation metrics within the specified window",
            )

    return metrics


@router.get("/{instance_id}/usage", response_model=InstanceUsageSnapshot)
async def get_instance_usage(instance_id: str):
    """Return the latest backend /metrics snapshot for the instance.

    The cumulative counters (prompt/generated/cached token totals) exist for
    traffic aggregation; the gauges reflect the current backend state. Only
    present for instances whose backend exposes a Prometheus endpoint, and
    only once the metrics poll loop has fetched at least one snapshot.
    """
    if not config_manager.get_instance(instance_id):
        raise HTTPException(status_code=404, detail="Instance not found")

    snapshot = process_manager.get_instance_usage(instance_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No usage metrics available")
    return snapshot
