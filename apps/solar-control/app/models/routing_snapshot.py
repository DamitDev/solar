"""Typed, versioned snapshot of fleet runtime state.

Composed by :func:`app.services.routing_snapshot_builder.build_routing_snapshot` from the
Redis stores and the endpoints/hosts tables. The same payload is shared by the
WebUI ``routing_snapshot`` WS event and the ``GET /api/routing/state`` REST
mirror, so both paths code against one stable schema.

Field contract to the WebUI (which maps snapshot fields onto the Maps it
initializes):

- ``active_requests`` → the ``requests`` Map (a ``RequestState``-compatible
  list of in-flight requests)
- ``instance_states`` → the ``instanceStates`` Map (keyed ``host:instance``)
- ``endpoints`` → the ``endpoints`` list of ``ApiEndpoint`` records
- ``aggregates`` → server-computed inFlight/totals used by the workload/graph
  logic instead of re-deriving them client-side from the request list
- ``hosts`` / ``pending_hosts`` → host status + pending-approval hosts
"""

from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class RoutingHost(BaseModel):
    """One host as surfaced in the snapshot (status + drain + health)."""

    host_id: str
    name: str | None = None
    status: str = "offline"
    drain_state: str | None = None
    connected: bool = False
    url: str | None = None
    last_seen: str | None = None
    gpu_type: str | None = None
    roles: list[str] = Field(default_factory=list)
    memory: dict[str, Any] | None = None
    disk_total_gb: float | None = None
    disk_used_gb: float | None = None
    disk_available_gb: float | None = None
    memory_available_gb: float | None = None
    version: str | None = None
    instances: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The host's instance cache (instances_update shape)",
    )


class InstanceStateEntry(BaseModel):
    """Latest runtime state for a ``host:instance`` (InstanceStatePayload)."""

    host_id: str
    instance_id: str
    timestamp: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ActiveRequestEntry(BaseModel):
    """One in-flight request from the per-request active registry."""

    request_id: str
    model: str | None = None
    resolved_model: str | None = None
    endpoint: str | None = None
    host_id: str | None = None
    host_name: str | None = None
    instance_id: str | None = None
    attempt: int | None = None
    timestamp: str | None = None
    # Derived server-side: 'queued' when not yet routed, else 'processing'.
    status: str = "queued"


class RequestAggregates(BaseModel):
    """Server-computed request tallies (replaces client-side re-derivation).

    Keyed maps use the ``host:instance`` / ``host_id`` / model / endpoint
    identifiers the WebUI's workload/graph logic expects.
    """

    by_instance: dict[str, int] = Field(
        default_factory=dict, description='"host:instance" -> in-flight count'
    )
    by_host: dict[str, int] = Field(default_factory=dict)
    by_model: dict[str, int] = Field(default_factory=dict)
    by_endpoint: dict[str, int] = Field(default_factory=dict)

    queued: int = 0
    processing: int = 0
    errored: int = 0


class RoutingSnapshot(BaseModel):
    """The authoritative snapshot of fleet runtime state.

    ``schema_version`` lets future changes to the payload shape migrate
    consumers without guessing; ``generated_at`` makes freshness measurable.
    """

    schema_version: int = SCHEMA_VERSION
    generated_at: str = Field(
        ..., description="ISO 8601 timestamp when the snapshot was composed"
    )
    hosts: list[RoutingHost] = Field(default_factory=list)
    instance_states: list[InstanceStateEntry] = Field(default_factory=list)
    active_requests: list[ActiveRequestEntry] = Field(default_factory=list)
    aggregates: RequestAggregates = Field(default_factory=RequestAggregates)
    endpoints: list[dict[str, Any]] = Field(
        default_factory=list,
        description="ApiEndpoint records (same shape as the endpoints_update event)",
    )
    pending_hosts: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Pending-approval hosts (same shape as the host_pending event)",
    )
