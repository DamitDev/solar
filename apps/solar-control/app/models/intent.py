"""Pydantic models for deployment intents (S-040)."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# ── Enums ──────────────────────────────────────────────────────


class IntentPhase(str, Enum):
    PENDING = "pending"
    RECONCILING = "reconciling"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class ReconcileState(str, Enum):
    IDLE = "idle"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


VALID_PRIORITIES: frozenset[str] = frozenset({"production", "staging", "ephemeral"})
VALID_STRATEGIES: frozenset[str] = frozenset({"rolling", "immediate"})
VALID_BACKEND_TYPES: frozenset[str] = frozenset(
    {
        "llamacpp",
        "huggingface_causal",
        "huggingface_classification",
        "huggingface_embedding",
        "huggingface_vision",
    }
)
VALID_MODEL_SOURCE_SCHEMES: frozenset[str] = frozenset({"repo", "huggingface", "local"})
FORBIDDEN_BACKEND_FIELDS: frozenset[str] = frozenset(
    {
        "alias",
        "model_source",
        "host",
        "port",
        "api_key",
    }
)


# ── Request models ─────────────────────────────────────────────


class PlacementConstraints(BaseModel):
    """Placement constraints for intent (S-039 §4.5)."""

    roles: list[str] = Field(default_factory=lambda: ["inference"])
    gpu_type: str | None = None
    host_allow: list[str] = Field(default_factory=list)
    host_deny: list[str] = Field(default_factory=list)


class ResourceRequirements(BaseModel):
    """Resource hints for placement (S-039 §4.6)."""

    vram_gb: float | None = None
    ram_gb: float | None = None


class IntentCreate(BaseModel):
    """Request body for POST /api/intents (S-039 §4.1)."""

    alias: str = Field(..., min_length=1)
    model_source: str = Field(..., min_length=1)
    replicas: int = Field(default=1, ge=0)
    priority: str = Field(default="production")
    strategy: str = Field(default="rolling")
    backend: dict[str, Any] = Field(...)
    placement: PlacementConstraints = Field(default_factory=PlacementConstraints)
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    metadata: dict[str, str] = Field(default_factory=dict)


class IntentUpdate(IntentCreate):
    """Request body for PUT /api/intents/{id} (S-039 §12.5, S-044).

    Same schema as create, with full-replace semantics: an omitted field is
    reset to its default, so clients send the complete spec rather than a
    diff. ``alias`` must match the stored one — it is the served name and
    the deployment's identity, not an editable field.
    """


# ── Response models ────────────────────────────────────────────


class ReplicaEntry(BaseModel):
    """Per-replica detail in status.replica_set (S-039 §10.1)."""

    host_id: str | None = None
    host_name: str | None = None
    instance_id: str | None = None
    state: str | None = None
    model_source: str | None = None
    healthy: bool = False
    message: str | None = None
    updated_at: str | None = None


class Condition(BaseModel):
    """Machine-readable condition (S-039 §10.3)."""

    type: str
    status: bool
    reason: str
    message: str
    last_transition: str


class StrategyProgress(BaseModel):
    """In-flight strategy progress (S-039 §11.4, extended for S-042 state machine).

    Persisted in intent status_json so strategy state survives reconciler
    restarts.  The ``phase`` field drives the strategy state machine;
    ``current_host_id`` / ``current_instance_id`` track which replacement
    is in flight; ``pending_hosts`` / ``failed_hosts`` track remaining
    and failed hosts across ticks.
    """

    strategy: str
    target_model_source: str | None = None
    drifted_instance_ids: list[str] | None = Field(
        default=None,
        description=(
            "Replicas this rollout is replacing. Identifies them by id rather "
            "than by model_source, because an edited spec (S-044) can change "
            "backend config alone, and an in-place replacement shares both "
            "host and source with the replica it replaces. None means the "
            "rollout predates the field and only model_source drift applies."
        ),
    )
    phase: str | None = None
    step: str | None = None
    updated: int = 0
    in_progress: int = 0
    failed: int = 0
    current_host_id: str | None = None
    current_instance_id: str | None = None
    current_old_instance_id: str | None = Field(
        default=None,
        description=(
            "Replica the current step replaces. Recorded because a step can "
            "end up placing its replacement on a different host than the "
            "replica it retires, when the first host cannot take it."
        ),
    )
    pending_hosts: list[str] = Field(default_factory=list)
    failed_hosts: list[str] = Field(default_factory=list)
    started_at: str | None = None
    message: str | None = None


class LastError(BaseModel):
    """Most recent reconciliation error (S-039 §10.2)."""

    code: str
    message: str
    host_id: str | None = None
    source_uri: str | None = None
    at: str
    # C2: which instance failed and what it printed. instance_id links the
    # error to the process logs endpoint; log_tail is the tail the host
    # attached to the start failure response.
    instance_id: str | None = None
    log_tail: list[str] | None = None
    # C4: True when the action gave up while the host was still making
    # progress (e.g. a cold-start pull that outlived the bound) — the webui
    # renders this as "still working" rather than a hard failure.
    recoverable: bool = False


class IntentStatus(BaseModel):
    """Server-managed status object (S-039 §10.1–10.2)."""

    phase: IntentPhase = IntentPhase.PENDING
    reconcile: ReconcileState = ReconcileState.IDLE
    desired_replicas: int = 0
    observed_replicas: int = 0
    ready_replicas: int = 0
    updated_replicas: int = 0
    available: bool = False
    shortfall: int = 0
    replica_set: list[ReplicaEntry] = Field(default_factory=list)
    conditions: list[Condition] = Field(default_factory=list)
    strategy_progress: StrategyProgress | None = None
    last_error: LastError | None = None
    spec_changed_at: str | None = Field(
        default=None,
        description=(
            "Set when the spec was updated (S-044) and cleared once the "
            "replicas match it again. While set, the reconciler compares the "
            "full instance configuration so backend-only edits roll out"
        ),
    )
    drift_replace_attempts: int = Field(
        default=0,
        description=(
            "Consecutive drift-driven REPLACE rounds for a pending spec "
            "change (C1). Reset to 0 when the spec settles or is edited; "
            "when it reaches max_drift_replace_attempts the reconciler stops "
            "planning REPLACE and records a BackendDriftUnsettled error"
        ),
    )
    drift_unsettled_keys: list[str] = Field(
        default_factory=list,
        description=(
            "Backend keys that stayed mismatched after "
            "max_drift_replace_attempts REPLACE rounds (C1). Persisted rather "
            "than derived per tick: only the diff path can detect the "
            "condition, so a tick routed through the settle window or a "
            "rollout strategy would otherwise let the phase flip back to "
            "Ready and flap. Cleared when the spec settles or is edited"
        ),
    )
    shortfall_reason: str | None = Field(
        default=None,
        description=(
            "C3: why the intent cannot be fully placed, as a specific "
            "message (e.g. 'no host matches gpu_type=apple_mps'); used as "
            "the Degraded condition message when a specific cause is known"
        ),
    )
    created_at: str | None = None
    updated_at: str | None = None
    last_reconciled_at: str | None = None
    ready_at: str | None = None


class IntentResponse(BaseModel):
    """Full intent record returned by GET/POST (S-039 §10.1)."""

    id: str
    alias: str
    model_source: str
    replicas: int
    priority: str
    strategy: str
    backend: dict[str, Any]
    placement: PlacementConstraints
    resources: ResourceRequirements
    metadata: dict[str, str] = Field(default_factory=dict)
    status: IntentStatus
    # C3: advisory warnings attached to the create/update response only —
    # never persisted and never emitted on intent_update.
    warnings: list[dict[str, str]] | None = None


class IntentDeletedResponse(BaseModel):
    """Response for DELETE /api/intents/{id} (S-039 §12.4)."""

    id: str
    alias: str
    phase: IntentPhase = IntentPhase.DELETING
    message: str = "Intent deletion initiated"
