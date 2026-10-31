"""Pydantic models for the public status summary."""

from typing import Literal

from pydantic import BaseModel, Field

from app.models.status_message import StatusMessageResponse

# ``unknown`` is the honest answer when the live state or the history of a
# component could not be read: an unreadable source must never look like a
# confident "down".
Availability = Literal["operational", "degraded", "down", "unknown"]


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
    # existed. The in-progress day is not counted. None when no complete
    # day has samples.
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
    # Instance counts and intent phases are deliberately not published: this
    # model is served unauthenticated. Operators read them from the management
    # API (/api/instances, /api/intents).
    alias: str


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
    messages: list[StatusMessageResponse] = Field(default_factory=list)
