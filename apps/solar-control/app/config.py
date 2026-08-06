import sys

from pydantic_settings import BaseSettings, SettingsConfigDict

# Under pytest, never read the developer-local .env — the test suite must
# be hermetic and unaffected by local overrides (a real MANAGEMENT_API_KEY
# in .env used to turn hardcoded-key route tests into 401s locally while
# CI stayed green). Env vars still take precedence, so spawned processes
# (integration suite) are unaffected.
_TESTING = "pytest" in sys.modules


class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(
        env_file=None if _TESTING else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000

    management_api_key: str = "change-me-management"

    registry_refresh_interval_s: float = 2.0
    health_check_interval_s: float = 1.0
    health_ttl_s: float = 3.0
    health_cooldown_s: float = 5.0
    route_connect_timeout_s: float = 0.5
    route_total_timeout_s: float = 600.0
    route_max_attempts: int = 3
    route_retry_delay_s: float = 0.15

    health_probe_use_http: bool = False
    health_probe_http_path: str = "/v1/models"

    disconnect_grace_period_s: float = 15.0
    reconnect_request_interval_s: float = 30.0

    database_url: str = "postgresql://solar:solar@localhost:5432/solar_gateway"
    redis_url: str = "redis://localhost:6379/0"

    data_repository_url: str = ""
    data_repository_api_key: str = ""
    data_repository_timeout_s: float = 10.0

    # Artifact upload relay (S-047)
    harbor_url: str = ""
    harbor_username: str = ""
    harbor_password: str = ""
    # Chunk size for the streaming OCI blob upload. 8 MiB is above the 5 MiB
    # minimum that object-storage registry drivers impose (spec §4.4).
    upload_chunk_size_bytes: int = 8 * 1024 * 1024
    # Redis TTL for an upload session; refreshed on each file completion.
    upload_session_ttl_s: int = 86400

    db_pool_size: int = 20
    db_max_overflow: int = 10

    # Job step execution
    job_min_disk_gb: float = 50.0
    job_submission_timeout_s: float = 30.0
    # Fallback container image registry/tag for pipeline steps when the
    # SuperNova intent does not specify an explicit `image` per step.
    # When empty, an explicit image is required for every step.
    job_step_image_registry: str = ""
    job_step_image_tag: str = "latest"

    # Reconciliation (S-041)
    reconcile_interval_s: float = 10.0

    # Strategy health gate timeout (S-042)
    # Maximum seconds to wait for a replacement instance to become healthy
    # before the rolling strategy holds and reports failure. Raised now that
    # an instance is only ``running`` when its backend genuinely serves
    # requests: a cold large model legitimately needs longer than 2 minutes.
    reconcile_health_gate_timeout_s: float = 600.0

    # Host instance start timeout: POST /instances/{id}/start blocks until
    # the backend reports readiness (log-gated), so a cold model load must
    # fit inside this window on every hop of the call chain.
    host_start_timeout_s: float = 900.0

    # Model pull timeout: the resolver's POST /models/pull hop on the host.
    # A multi-GB HuggingFace/Harbor download can legitimately take minutes;
    # the reconciler's cold-start action bound derives from this value.
    model_pull_timeout_s: float = 1800.0

    # C1 churn circuit breaker: consecutive drift-driven REPLACE rounds
    # before the reconciler stops planning REPLACE for the intent and
    # records a BackendDriftUnsettled error instead of looping forever.
    max_drift_replace_attempts: int = 3

    # C5 WS-first resource read model: how old a Redis-cached host resource
    # snapshot may be (seconds) before _fetch_host_resource_snapshot falls
    # back to an HTTP call. The host pushes health every 10 s, so 30 s is
    # three health ticks.
    host_snapshot_max_age_s: float = 30.0

    # C4 progress-aware cold-start bound: the action wait slices into
    # action_progress_slice_s chunks and keeps waiting while the host's pull
    # progress is newer than pull_progress_stale_after_s.
    action_progress_slice_s: float = 120.0
    pull_progress_stale_after_s: float = 180.0


settings = Settings()
