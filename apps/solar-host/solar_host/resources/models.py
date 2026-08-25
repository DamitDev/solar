"""Pydantic models for resource reservations (S-034)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class GpuInfo(BaseModel):
    """Per-device GPU telemetry (S-058).

    L1: exactly the five spec fields — physical device index as reported by
    pynvml, name, total/used/available in GB. ``available_gb`` is live
    nvidia-smi free memory (any process), *before* reservation headroom.
    """

    index: int
    name: str
    total_gb: float
    used_gb: float
    available_gb: float


class WorkloadType(str, Enum):
    training = "training"
    inference = "inference"
    other = "other"


class ReservationRequest(BaseModel):
    """Input model for POST /resources/reservations."""

    job_id: str
    requester: str | None = None
    workload_type: WorkloadType = WorkloadType.training
    # S-058: vram_gb is a *per-GPU* footprint; gpu_count defaults to 1 so
    # single-GPU callers (and legacy SuperNova/step callers) are untouched.
    # gpu_ids is optional — when omitted the host self-assigns the
    # least-loaded devices (L4).
    vram_gb: float = Field(ge=0)
    ram_gb: float = Field(ge=0)
    disk_gb: float | None = Field(default=None, ge=0)
    gpu_count: int = Field(default=1, ge=1)
    gpu_ids: list[int] | None = None
    ttl_seconds: float | None = Field(default=None, gt=0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_expiry_and_gpus(self) -> ReservationRequest:
        if self.ttl_seconds is not None and self.expires_at is not None:
            raise ValueError(
                "Specify at most one of ttl_seconds or expires_at, not both"
            )
        if self.gpu_ids is not None:
            if len(self.gpu_ids) != self.gpu_count:
                raise ValueError(
                    f"gpu_ids has {len(self.gpu_ids)} entries but gpu_count is "
                    f"{self.gpu_count} — they must agree"
                )
            if len(set(self.gpu_ids)) != len(self.gpu_ids):
                raise ValueError("gpu_ids must not contain duplicates")
        return self


class Reservation(BaseModel):
    """Runtime state for an active reservation."""

    id: str
    job_id: str
    requester: str | None = None
    workload_type: WorkloadType
    vram_gb: float
    ram_gb: float
    disk_gb: float | None = None
    # S-058: the *resolved* device set (self-assigned by the host when the
    # request omitted gpu_ids, L4) and the device count.
    gpu_ids: list[int] = []
    gpu_count: int = 1
    created_at: datetime
    expires_at: datetime | None = None

    # Cached actual usage (updated by the poll loop for running reservations).
    actual_vram_gb: float | None = None
    actual_ram_gb: float | None = None
    actual_disk_gb: float | None = None
    usage_polled_at: datetime | None = None

    @classmethod
    def from_request(
        cls,
        req: ReservationRequest,
        now: datetime,
        default_ttl_seconds: float | None = None,
    ) -> Reservation:
        """Build a Reservation, resolving its expiry.

        Precedence for ``expires_at``:
        1. explicit ``req.expires_at``
        2. ``req.ttl_seconds`` relative to *now*
        3. ``default_ttl_seconds`` relative to *now* (a default, not a cap —
           callers that want a longer-lived reservation supply their own
           ttl_seconds/expires_at)
        """
        expires_at: datetime | None = None
        if req.expires_at is not None:
            expires_at = req.expires_at
        elif req.ttl_seconds is not None:
            expires_at = now + timedelta(seconds=req.ttl_seconds)
        elif default_ttl_seconds is not None:
            expires_at = now + timedelta(seconds=default_ttl_seconds)
        return cls(
            id=f"res-{uuid.uuid4().hex}",
            job_id=req.job_id,
            requester=req.requester,
            workload_type=req.workload_type,
            vram_gb=req.vram_gb,
            ram_gb=req.ram_gb,
            disk_gb=req.disk_gb,
            gpu_ids=list(req.gpu_ids) if req.gpu_ids else [],
            gpu_count=req.gpu_count,
            created_at=now,
            expires_at=expires_at,
        )


class ResourceDimensionSnapshot(BaseModel):
    """Capacity snapshot for a single resource dimension (VRAM/RAM/disk)."""

    total_gb: float
    system_used_gb: float
    reserved_headroom_gb: float
    reported_used_gb: float
    available_gb: float


class ReservationView(BaseModel):
    """Public view of a reservation (returned in API responses)."""

    id: str
    job_id: str
    workload_type: WorkloadType
    status: str  # "pending" | "running"
    vram_gb: float
    ram_gb: float
    disk_gb: float | None = None
    gpu_ids: list[int] | None = None
    gpu_count: int = 1
    actual_vram_gb: float | None = None
    actual_ram_gb: float | None = None
    actual_disk_gb: float | None = None
    expires_at: datetime | None = None


class ResourceSnapshot(BaseModel):
    """Full resource snapshot returned by GET /resources."""

    memory_type: str  # "VRAM" or "RAM" — primary dimension reported by get_memory_info
    vram: ResourceDimensionSnapshot | None = None
    ram: ResourceDimensionSnapshot | None = None
    disk: ResourceDimensionSnapshot | None = None
    # S-058: per-device GPU telemetry; empty on Mac/CPU hosts (D6).
    gpus: list[GpuInfo] = []
    reservations: list[ReservationView] = []
