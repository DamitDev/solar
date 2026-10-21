"""Pydantic models for the public status summary."""

from typing import Literal

from pydantic import BaseModel, Field

from app.models.status_message import StatusMessageResponse

Availability = Literal["operational", "degraded", "down"]


class UptimePoint(BaseModel):
    """One 1d bucket of the uptime history.

    ``uptime`` is the share of sampler ticks the component was up (0..1).
    A bucket with no samples is an outage gap: ``uptime`` 0 and ``samples``
    0, never interpolated from its neighbours.
    """

    ts: str
    uptime: float
    samples: int


class ComponentStatus(BaseModel):
    availability: Availability
    # Percentage over the 90-day window, counted from the first day with
    # samples so a young component is not penalised for days before it
    # existed. None when no samples exist at all.
    uptime_pct: float | None = None
    history: list[UptimePoint] = Field(default_factory=list)


class VirtualModelStatus(ComponentStatus):
    name: str
    description: str | None = None
    # Ordered as declared; the order is the routing preference.
    targets: list[str]
    # target alias -> "satisfied" | "unverified" | "violating: <reason>"
    # | "missing"
    target_status: dict[str, str]


class AliasStatus(ComponentStatus):
    alias: str
    instances_running: int
    instances_total: int
    # Reconciliation phase of the intent managing this alias (e.g. "ready",
    # "degraded", "failed"); None for aliases no intent manages.
    intent_phase: str | None = None


class InstanceStatus(ComponentStatus):
    instance_id: str
    alias: str


class StatusSummary(BaseModel):
    generated_at: str
    window_days: int
    bucket: str
    virtual_models: list[VirtualModelStatus]
    aliases: list[AliasStatus]
    instances: list[InstanceStatus]
    # Messages inside their start_time <= now < end_time window.
    messages: list[StatusMessageResponse] = Field(default_factory=list)
    # Planned-maintenance messages published ahead of their start_time.
    upcoming_messages: list[StatusMessageResponse] = Field(default_factory=list)
