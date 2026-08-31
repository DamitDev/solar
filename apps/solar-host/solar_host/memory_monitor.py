"""
Memory monitoring for GPU VRAM (NVIDIA) and system RAM (macOS),
GPU type detection, and disk usage reporting.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import psutil

if TYPE_CHECKING:
    from solar_host.resources.models import GpuInfo

logger = logging.getLogger(__name__)


class _OverrideDevice(TypedDict):
    """Shape of one ``GPU_TELEMETRY_OVERRIDE`` entry (L2)."""

    index: int
    name: str
    total_gb: float
    used_gb: float


# Cache for memory info to avoid excessive polling
_memory_cache: dict | None = None
_cache_timestamp: float = 0
CACHE_DURATION = 5.0  # seconds

# GPU type is constant for the lifetime of the process
_gpu_type_cache: str | None = None

# Per-device GPU list cache (own pair, same clock as _memory_cache). The
# health loop runs every 10 s and /resources on demand; caching keeps the
# NVML init per call at the current call rate (S-058).
_gpu_list_cache: list | None = None
_gpu_list_timestamp: float = 0


def get_memory_info() -> dict[str, float | str] | None:
    """
    Get memory information based on platform.

    Returns dict with:
    - used_gb: Used memory in GB
    - total_gb: Total memory in GB
    - available_gb: Memory available for new workloads (total - used)
    - percent: Usage percentage
    - memory_type: "VRAM" or "RAM"

    Returns None if memory info cannot be obtained.
    """
    global _memory_cache, _cache_timestamp

    # Return cached data if still valid
    current_time = time.time()
    if _memory_cache and (current_time - _cache_timestamp) < CACHE_DURATION:
        return _memory_cache

    system = platform.system()

    if system == "Darwin":
        result = _get_mac_memory()
    else:
        result = _get_nvidia_memory()
        if result is None:
            result = _get_system_memory()

    # Update cache
    if result:
        _memory_cache = result
        _cache_timestamp = current_time

    return result


def detect_gpu_type() -> str:
    """Detect the acceleration backend available on this host.

    Returns one of: "nvidia_cuda", "apple_mps", "cpu".
    Result is cached for the lifetime of the process.
    """
    global _gpu_type_cache
    if _gpu_type_cache is not None:
        return _gpu_type_cache

    # L2 dev/test hook: a fake device list makes the host indistinguishable
    # from a real NVIDIA host for the whole telemetry chain.
    if _gpu_telemetry_override() is not None:
        _gpu_type_cache = "nvidia_cuda"
        return _gpu_type_cache

    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        if pynvml.nvmlDeviceGetCount() > 0:
            pynvml.nvmlShutdown()
            _gpu_type_cache = "nvidia_cuda"
            return _gpu_type_cache
        pynvml.nvmlShutdown()
    except Exception:  # noqa: S110, BLE001
        pass

    if platform.system() == "Darwin":
        _gpu_type_cache = "apple_mps"
        return _gpu_type_cache

    _gpu_type_cache = "cpu"
    return _gpu_type_cache


def _gpu_telemetry_override() -> list[_OverrideDevice] | None:
    """Return the parsed ``GPU_TELEMETRY_OVERRIDE`` device list, or None.

    The override is a JSON array of ``{"index", "name", "total_gb",
    "used_gb"}`` (dev/test only, L2). When set it drives
    ``get_gpu_list()``, ``verify_gpu_capacity()``, ``_get_nvidia_memory()``
    and ``detect_gpu_type()`` so a fake host behaves like a real one end to
    end. Invalid JSON or malformed rows are skipped defensively.
    """
    from solar_host.config import settings

    raw = settings.gpu_telemetry_override.strip()
    if not raw:
        return None
    try:
        devices = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("GPU_TELEMETRY_OVERRIDE is not valid JSON; ignoring")
        return None
    if not isinstance(devices, list):
        logger.warning("GPU_TELEMETRY_OVERRIDE is not a JSON array; ignoring")
        return None
    result: list[_OverrideDevice] = []
    for item in devices:
        try:
            index = int(item["index"])
            total_gb = float(item["total_gb"])
            used_gb = float(item["used_gb"])
        except (KeyError, TypeError, ValueError):
            logger.warning("GPU_TELEMETRY_OVERRIDE entry %r skipped", item)
            continue
        result.append(
            {
                "index": index,
                "name": str(item.get("name", "GPU")),
                "total_gb": total_gb,
                "used_gb": used_gb,
            }
        )
    return result


def _get_nvidia_memory() -> dict[str, float | str] | None:
    """Get combined VRAM from all NVIDIA GPUs."""
    try:
        override = _gpu_telemetry_override()
        if override is not None:
            # L2 dev/test hook: sum the fake devices so the aggregate
            # numbers stay consistent with the per-device list.
            total_used = sum(float(d["used_gb"]) for d in override)
            total_capacity = sum(float(d["total_gb"]) for d in override)
            if total_capacity <= 0:
                return None
            used_gb = total_used
            total_gb = total_capacity
            percent = total_used / total_capacity * 100
            return {
                "used_gb": round(used_gb, 2),
                "total_gb": round(total_gb, 2),
                "available_gb": round(total_gb - used_gb, 2),
                "percent": round(percent, 2),
                "memory_type": "VRAM",
            }

        import pynvml  # type: ignore

        pynvml.nvmlInit()
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count == 0:
                return None

            total_used = 0
            total_capacity = 0
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                total_used += info.used
                total_capacity += info.total
        finally:
            pynvml.nvmlShutdown()

        used_gb = total_used / (1024**3)
        total_gb = total_capacity / (1024**3)
        percent = (total_used / total_capacity * 100) if total_capacity > 0 else 0
        available_gb = total_gb - used_gb

        return {
            "used_gb": round(used_gb, 2),
            "total_gb": round(total_gb, 2),
            "available_gb": round(available_gb, 2),
            "percent": round(percent, 2),
            "memory_type": "VRAM",
        }
    except Exception:  # noqa: BLE001
        return None


def _get_system_memory() -> dict[str, float | str] | None:
    """Get system RAM info via psutil (fallback for Linux without NVIDIA)."""
    try:
        mem = psutil.virtual_memory()
        used_gb = mem.used / (1024**3)
        total_gb = mem.total / (1024**3)
        available_gb = total_gb - used_gb
        return {
            "used_gb": round(used_gb, 2),
            "total_gb": round(total_gb, 2),
            "available_gb": round(available_gb, 2),
            "percent": round(mem.percent, 2),
            "memory_type": "RAM",
        }
    except Exception:  # noqa: BLE001
        return None


def _get_mac_memory() -> dict[str, float | str] | None:
    """Get unified memory info on macOS."""
    try:
        mem = psutil.virtual_memory()

        # Convert bytes to GB
        used_gb = mem.used / (1024**3)
        total_gb = mem.total / (1024**3)
        percent = mem.percent

        available_gb = total_gb - used_gb

        return {
            "used_gb": round(used_gb, 2),
            "total_gb": round(total_gb, 2),
            "available_gb": round(available_gb, 2),
            "percent": round(percent, 2),
            "memory_type": "RAM",
        }
    except Exception:  # noqa: BLE001
        return None


def get_gpu_devices() -> list[dict[str, object]]:
    """Return per-device GPU info via pynvml.

    Each entry: {"index": int, "uuid": str, "name": str,
                 "total_gb": float, "used_gb": float}
    Returns an empty list if pynvml is unavailable or no GPUs are detected.
    """
    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            devices: list[dict[str, object]] = []
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                uuid: str = pynvml.nvmlDeviceGetUUID(handle)
                name: str = pynvml.nvmlDeviceGetName(handle)
                devices.append(
                    {
                        "index": i,
                        "uuid": uuid,
                        "name": name,
                        "total_gb": round(mem.total / (1024**3), 2),
                        "used_gb": round(mem.used / (1024**3), 2),
                    }
                )
        finally:
            pynvml.nvmlShutdown()
        return devices
    except Exception:  # noqa: BLE001
        return []


def get_gpu_list() -> list[GpuInfo]:
    """Return typed per-device GPU telemetry (S-058).

    Each entry is a ``GpuInfo`` with ``index``/``name``/``total_gb``/
    ``used_gb``/``available_gb`` (L1: exactly the five spec fields; the
    raw ``uuid`` from pynvml is dropped). Honors the ``GPU_TELEMETRY_OVERRIDE``
    dev hook (L2) and is cached on the same ``CACHE_DURATION`` clock as
    ``get_memory_info()``. Returns an empty list on Mac/CPU hosts or when
    NVML is unavailable — that empty list is D6's graceful-degradation
    signal to the control plane.
    """
    global _gpu_list_cache, _gpu_list_timestamp

    current_time = time.time()
    if (
        _gpu_list_cache is not None
        and (current_time - _gpu_list_timestamp) < CACHE_DURATION
    ):
        return _gpu_list_cache

    from solar_host.resources.models import GpuInfo

    override = _gpu_telemetry_override()
    if override is not None:
        devices: list[dict[str, object]] = [
            {
                "index": d["index"],
                "name": d["name"],
                "total_gb": d["total_gb"],
                "used_gb": d["used_gb"],
            }
            for d in override
        ]
    else:
        devices = get_gpu_devices()

    result = [
        GpuInfo(
            index=int(d["index"]),
            name=str(d["name"]),
            total_gb=float(d["total_gb"]),
            used_gb=float(d["used_gb"]),
            available_gb=max(0.0, float(d["total_gb"]) - float(d["used_gb"])),
        )
        for d in devices
    ]
    _gpu_list_cache = result
    _gpu_list_timestamp = current_time
    return result


def verify_gpu_capacity(
    gpu_ids: list[int], required_gb: float
) -> tuple[int, float] | None:
    """Live per-device re-read for spawn-time verification (S-058).

    Returns the first ``(index, available_gb)`` whose device cannot fit
    ``required_gb`` (missing device counts as 0 free); ``None`` when every
    device fits or when per-device telemetry is unavailable. Never blocks a
    start on a missing NVML — an unverifiable host degrades to no check.
    """
    if not gpu_ids or required_gb <= 0:
        return None

    override = _gpu_telemetry_override()
    if override is not None:
        devices: list[dict[str, object]] = [
            {
                "index": d["index"],
                "name": d["name"],
                "total_gb": d["total_gb"],
                "used_gb": d["used_gb"],
            }
            for d in override
        ]
    else:
        # Not the cached path: this is the TOCTOU guard, it must read NVML
        # fresh (nvidia-smi level) rather than a snapshot up to 5 s old.
        devices = get_gpu_devices()

    if not devices:
        # D6: an unverifiable host degrades to no check rather than
        # failing every GPU-assigned start. An empty ledger means NVML is
        # unavailable (driver reload, lost device mapping, manual restart
        # of a stopped instance whose gpu_ids persisted in config.json) —
        # never block the spawn on it.
        return None
    by_index = {int(d["index"]): d for d in devices}  # type: ignore[arg-type]
    for idx in gpu_ids:
        device = by_index.get(idx)
        if device is None:
            return (idx, 0.0)
        total_gb = float(device["total_gb"])  # type: ignore[arg-type]
        used_gb = float(device["used_gb"])  # type: ignore[arg-type]
        available = total_gb - used_gb
        if available < required_gb:
            return (idx, available)
    return None


def get_gpu_process_memory() -> dict[int, int]:
    """Return per-PID GPU memory usage in bytes across all NVIDIA devices.

    Aggregates both compute and graphics processes via pynvml.
    Returns an empty dict when pynvml is unavailable or no GPUs are detected.
    """

    # NVML reports NVML_VALUE_NOT_AVAILABLE ((unsigned long long)-1) when a
    # process's per-PID GPU memory can't be read (e.g. insufficient permissions
    # or MIG). The Python binding surfaces this as a huge sentinel or None;
    # accumulating it would wildly inflate usage, so we discard such values.
    def _valid(used: object) -> int | None:
        if used is None:
            return None
        try:
            value = int(used)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if value < 0 or value >= 2**63:
            return None
        return value

    try:
        import pynvml  # type: ignore

        pynvml.nvmlInit()
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            pid_bytes: dict[int, int] = {}
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                for proc in pynvml.nvmlDeviceGetComputeRunningProcesses(handle):
                    used = _valid(proc.usedGpuMemory)
                    if used is not None:
                        pid_bytes[proc.pid] = pid_bytes.get(proc.pid, 0) + used
                try:
                    for proc in pynvml.nvmlDeviceGetGraphicsRunningProcesses(handle):
                        used = _valid(proc.usedGpuMemory)
                        if used is not None:
                            pid_bytes[proc.pid] = pid_bytes.get(proc.pid, 0) + used
                except Exception:  # noqa: S110, BLE001
                    pass
        finally:
            pynvml.nvmlShutdown()
        return pid_bytes
    except Exception:  # noqa: BLE001
        return {}


def get_disk_info(path: str) -> dict[str, float] | None:
    """Return disk usage stats (in GB) for the filesystem containing *path*.

    Walks up to the nearest existing parent if *path* itself doesn't exist.
    Returns None if usage cannot be determined.
    """
    try:
        target = Path(path).resolve()
        while not target.exists():
            target = target.parent
        usage = shutil.disk_usage(target)
        return {
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "available_gb": round(usage.free / (1024**3), 2),
        }
    except Exception:  # noqa: BLE001
        return None
