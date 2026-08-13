"""Instance migration orchestrator (S-037).

Migrates an inference instance from a source host to a target host by:
1. Validating both hosts and checking for active training jobs
2. Capturing the instance configuration
3. Checking placement constraints (roles, GPU type, VRAM, disk)
4. Enforcing one-replica-per-host on the target
5. Ensuring the model is on the target via S-019 distribution
6. Stopping the source instance
7. Creating the target instance from the captured configuration

If stop or create fails, the partially-completed MigrationResult is returned
with status="failed" and per-step status so callers can inspect progress.

Active training jobs from S-032/S-033 are non-migratable workloads;
migration is rejected if the source host has any active job steps.

The stop-before-create ordering is explicit and documented. For rolling /
zero-downtime migrations, the S-042 strategy layer will orchestrate
create-then-stop on top of this primitive.

Evacuation (host draining, S-043 §4.2 / S-057) deliberately does NOT use
this ordering: ``execute_evacuation`` creates and starts the replacement
on the target before stopping and deleting the source, so the alias keeps
serving throughout a drain.
"""

import asyncio
import logging
import uuid
from typing import Any

import aiohttp
from fastapi import HTTPException

from app.database.hosts import host_db
from app.model_resolvers import resolve
from app.models import Host
from app.models.migration import MigrationResult, MigrationStep
from app.redis_state import host_store
from app.validation import validate_priority

logger = logging.getLogger(__name__)

# ── Shared: create an instance on a host (Option B refactor) ────


async def create_instance_on_host(
    host: Host, instance_data: dict[str, Any]
) -> dict[str, Any]:
    """Create an inference instance on *host* with the given config.

    Validates priority (S-036), resolves ``model_source`` (S-019), sets
    the derived ``model``/``model_id`` while preserving the original URI
    for cross-host operations (S-037), and POSTs to the host.
    """
    # Validate priority if present (S-036)
    validate_priority(instance_data)

    # Resolve model_source and set model/model_id while preserving the
    # original URI.  Support both flat and {config: {...}} payload shapes.
    # Skip re-resolution when the caller already set model/model_id
    # (migration passes the path returned by ensure_model_on_target).
    config = instance_data.get("config", instance_data)
    model_source = config.get("model_source")
    if model_source and not config.get("model") and not config.get("model_id"):
        backend_type = config.get("backend_type", "llamacpp")
        # Forward the backend so repo:// pulls for llama.cpp resolve to the
        # largest *.gguf in the artifact instead of the directory, and the
        # file filters so a HuggingFace snapshot only downloads what is needed.
        resolved = await resolve(
            model_source,
            host.url,
            host.api_key,
            backend_type=backend_type,
            file_filters=config.get("file_filters"),
        )
        # Extract filesystem path from local:// URI (scheme is 8 chars)
        if resolved.startswith("local://"):
            model_path = resolved[8:]
        else:
            model_path = resolved
        if backend_type.startswith("huggingface"):
            config["model_id"] = model_path
        else:
            config["model"] = model_path
        if "config" in instance_data:
            instance_data["config"] = config

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances"
            headers = {
                "X-API-Key": host.api_key,
                "Content-Type": "application/json",
            }
            async with session.post(
                url,
                json=instance_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502,
            detail=f"Host '{host.name}' is unreachable at {host.url}",
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach host '{host.name}': {e}",
        )


# ── Migration validation helpers ────────────────────────────────


async def capture_instance_config(
    source_host: Host, instance_id: str
) -> dict[str, Any]:
    """Retrieve the full instance configuration from *source_host*.

    Tries Redis cache first (fast path), then falls back to an HTTP
    ``GET /instances`` call to the host.  The Redis cache only
    short-circuits when the entry includes a ``config`` key (i.e. a
    full dump from a prior HTTP call); the flat WebSocket notification
    format omits most config fields and is not used as a shortcut.
    """
    # Fast path: full config in Redis cache?
    instances = await host_store.get_host_instances(source_host.id)
    for inst in instances:
        iid = inst.get("instance_id") or inst.get("id")
        if iid == instance_id and "config" in inst:
            logger.debug(
                "Instance config for %s/%s found in Redis cache",
                source_host.name,
                instance_id,
            )
            return inst

    # Fallback / direct: fetch from source host via HTTP
    logger.info(
        "Instance %s not in Redis cache for host %s, falling back to HTTP",
        instance_id,
        source_host.name,
    )
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": source_host.api_key}
            url = f"{source_host.url}/instances"
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to fetch instances from source host: {text}",
                    )
                all_instances = await response.json()
                for inst in all_instances:
                    iid = inst.get("instance_id") or inst.get("id")
                    if iid == instance_id:
                        return inst
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Source host '{source_host.name}' is unreachable at {source_host.url}"
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach source host '{source_host.name}': {e}",
        )

    raise HTTPException(
        status_code=404,
        detail=(
            f"Instance '{instance_id}' not found on source host '{source_host.name}'"
        ),
    )


async def check_one_replica_per_host(target_host: Host, alias: str) -> None:
    """Ensure no instance with the same *alias* exists on *target_host*.

    Raises ``HTTPException(409)`` if a conflict is found.
    """
    instances = await host_store.get_host_instances(target_host.id)
    for inst in instances:
        config = inst.get("config", inst)
        inst_alias = config.get("alias") or inst.get("alias")
        if inst_alias == alias:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Instance with alias '{alias}' already exists on "
                    f"target host '{target_host.name}'. One replica per "
                    f"host per alias is enforced."
                ),
            )


async def validate_target_fitness(
    target_host: Host,
    instance_config: dict[str, Any],
    *,
    allow_production: bool = False,
    source_gpu_type: str | None = None,
) -> None:
    """Validate that *target_host* is suitable for *instance_config*.

    Checks:
    - Target has ``"inference"`` role
    - Production safeguard (requires explicit ``allow_production``)
    - GPU type difference is logged but not blocked (GGUF models are
      portable across architectures and pulled fresh via S-019)\n    - Sufficient VRAM (best-effort, ≥ 2 GB threshold)
    - Sufficient disk space (best-effort, ≥ 5 GB threshold)
    """
    # Role check
    roles = target_host.roles or []
    if "inference" not in roles:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Target host '{target_host.name}' does not have the "
                f"'inference' role. Roles: {roles}"
            ),
        )

    # Production safeguard
    config = instance_config.get("config", instance_config)
    priority = config.get("priority") or instance_config.get("priority")
    if priority == "production" and not allow_production:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot migrate a 'production' instance without "
                "explicit allow_production=true. Production instances "
                "require an explicit policy decision to migrate."
            ),
        )

    # GPU type difference is logged but not rejected. GGUF models are
    # portable across GPU architectures and the model is pulled fresh
    # on the target host via S-019 distribution.  Placement constraints
    # (deployment-intent §4.5) default gpu_type to null (any).
    if (
        source_gpu_type
        and target_host.gpu_type
        and source_gpu_type != target_host.gpu_type
    ):
        logger.info(
            "GPU type differs — source '%s' (%s) → target '%s' (%s)",
            source_gpu_type,
            source_gpu_type,
            target_host.name,
            target_host.gpu_type,
        )

    # Resource check via /health (disk + VRAM)
    MIN_VRAM_GB = 2.0
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{target_host.url.rstrip('/')}/health"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()

                    # Disk check
                    available = data.get("disk", {}).get("available_gb")
                    if available is not None and available < 5.0:
                        raise HTTPException(
                            status_code=507,
                            detail=(
                                f"Insufficient disk on target host "
                                f"'{target_host.name}': "
                                f"{available:.2f} GB available"
                            ),
                        )

                    # VRAM check
                    memory_list = data.get("memory", [])
                    for mem in memory_list:
                        if mem.get("memory_type") == "VRAM":
                            vram_available = mem.get("available_gb")
                            if (
                                vram_available is not None
                                and vram_available < MIN_VRAM_GB
                            ):
                                raise HTTPException(
                                    status_code=507,
                                    detail=(
                                        f"Insufficient VRAM on target host "
                                        f"'{target_host.name}': "
                                        f"{vram_available:.2f} GB available "
                                        f"(minimum {MIN_VRAM_GB} GB required)"
                                    ),
                                )
                            break
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to check resources on target host %s: %s",
            target_host.id,
            e,
        )


async def ensure_model_on_target(
    target_host: Host, model_source: str, file_filters: list[str] | None = None
) -> tuple[str, bool]:
    """Ensure *model_source* is pulled on *target_host* via S-019.

    ``file_filters`` carries the migrating instance's HuggingFace download
    filters so the target snapshot matches the source one.

    Returns ``(local_path, cached)`` on success.
    Raises ``HTTPException`` on failure.
    """
    from app.model_resolvers.parser import parse
    from app.routes.management.models import _pull_on_host

    parsed = parse(model_source)
    result = await _pull_on_host(parsed, model_source, target_host, file_filters)

    from app.routes.management.models import _StructuredPullError

    if isinstance(result, _StructuredPullError):
        raise HTTPException(
            status_code=result.status_code,
            detail=(
                f"Failed to pull model '{result.source_uri}' on target "
                f"host '{target_host.name}': [{result.error}] "
                f"{result.detail}"
            ),
        )

    return result  # (path, cached)


async def active_job_ids_on_host(host: Host) -> list[str]:
    """Return the ids of non-terminal job steps running on *host*.

    Asks the host directly (``GET /jobs``) rather than the jobs table, so
    the answer reflects what is actually executing. An empty list also
    covers hosts that do not implement the endpoint.

    Raises ``HTTPException(502)`` when the host cannot be reached: callers
    gate destructive operations on this, so an unknown answer must not
    read as "no jobs".
    """
    TERMINAL_STATES = {"completed", "failed", "cancelled", "error"}

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url.rstrip('/')}/jobs"
            headers = {"X-API-Key": host.api_key}
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                if response.status == 200:
                    jobs = await response.json()
                elif response.status == 404:
                    logger.debug(
                        "Host '%s' returned 404 for GET /jobs, "
                        "assuming no training jobs",
                        host.name,
                    )
                    return []
                else:
                    text = await response.text()
                    logger.warning(
                        "Host '%s' returned %d for GET /jobs: %s",
                        host.name,
                        response.status,
                        text,
                    )
                    return []

        active_ids: list[str] = []
        for job in jobs if isinstance(jobs, list) else []:
            status = job.get("status") or job.get("state", "")
            if status not in TERMINAL_STATES:
                job_id = job.get("job_id") or job.get("id", "unknown")
                active_ids.append(job_id)
        return active_ids
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Host '{host.name}' is unreachable for the training job "
                f"check at {host.url}. Cannot verify that no training jobs "
                f"are active."
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(
                f"Cannot reach host '{host.name}' for the training job " f"check: {e}."
            ),
        )


async def check_no_active_training(host: Host) -> None:
    """Verify *host* has no active training job steps.

    Raises ``HTTPException(409)`` if any job step is in an active
    (non-terminal) state, per the S-037 requirement that active training
    jobs from S-032/S-033 are non-migratable workloads.
    """
    active_ids = await active_job_ids_on_host(host)
    if active_ids:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Source host '{host.name}' has {len(active_ids)} "
                f"active training job(s): {', '.join(active_ids[:5])}"
                f"{'...' if len(active_ids) > 5 else ''}. "
                f"Training jobs are non-migratable workloads. "
                f"Stop or wait for training jobs to complete before "
                f"migrating instances from this host."
            ),
        )


async def disown_source_instance(
    source_host: Host, instance_id: str, config: dict[str, Any]
) -> None:
    """Clear intent ownership markers from *instance_id* on *source_host*.

    The instance is left stopped — it is not deleted — but stops being
    managed by the intent reconciler (S-037/D-017).  Both the host-side
    instance record and the Redis cache are updated so the reconciler
    stops tracking it immediately and does not try to recreate it.
    """
    # 1. Host-side: clear markers via PUT (instance must be stopped).
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{source_host.url}/instances/{instance_id}"
            headers = {
                "X-API-Key": source_host.api_key,
                "Content-Type": "application/json",
            }
            payload = {"config": config, "managed_by": None, "intent_id": None}
            async with session.put(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise HTTPException(
                        status_code=response.status,
                        detail=f"Failed to disown source instance: {text}",
                    )
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Source host '{source_host.name}' is unreachable at {source_host.url}"
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach source host '{source_host.name}': {e}",
        )

    # 2. Redis: clear markers so the reconciler stops observing it now.
    # Must never escape as a raw 500: a failure here would leave the
    # intent markers in Redis while the host-side markers are already
    # cleared, and the reconciler would fight the stopped instance
    # (RECREATE -> /stop spam) forever.
    try:
        instances = await host_store.get_host_instances(source_host.id)
        for inst in instances:
            iid = inst.get("instance_id") or inst.get("id")
            if iid == instance_id:
                # Redis host_store entries are the flat WS format: markers
                # live at top level, and there is no nested "config" key.
                # Never fall back to `inst` itself (would self-reference).
                cfg = inst.get("config")
                if isinstance(cfg, dict):
                    cfg.pop("managed_by", None)
                    cfg.pop("intent_id", None)
                inst.pop("managed_by", None)
                inst.pop("intent_id", None)
                break
        await host_store.set_host_instances(source_host.id, instances)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(
                f"Failed to clear intent markers from Redis for source "
                f"instance '{instance_id}': {e}"
            ),
        )


async def stop_source_instance(source_host: Host, instance_id: str) -> dict[str, Any]:
    """Stop *instance_id* on *source_host*.

    Returns the host response on success. Raises ``HTTPException`` on
    failure.
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{source_host.url}/instances/{instance_id}/stop"
            headers = {
                "X-API-Key": source_host.api_key,
                "Content-Type": "application/json",
            }
            async with session.post(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Source host '{source_host.name}' is unreachable at {source_host.url}"
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach source host '{source_host.name}': {e}",
        )


async def start_instance_on_host(host: Host, instance_id: str) -> None:
    """Start *instance_id* on *host*, blocking until the backend is ready.

    The host parks the start call on its log-gated ready event, so a
    successful return means the instance is serving. Raises
    ``HTTPException`` on failure.
    """
    from app.config import settings

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url.rstrip('/')}/instances/{instance_id}/start"
            headers = {"X-API-Key": host.api_key}
            async with session.post(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=settings.host_start_timeout_s),
            ) as response:
                if response.status == 200:
                    return
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Host '{host.name}' is unreachable at {host.url} "
                f"for the start call"
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach host '{host.name}': {e}",
        )


async def delete_instance_on_host(host: Host, instance_id: str) -> None:
    """Delete *instance_id* from *host* (the instance must be stopped).

    Raises ``HTTPException`` on failure.
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url.rstrip('/')}/instances/{instance_id}"
            headers = {"X-API-Key": host.api_key}
            async with session.delete(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    return
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Host '{host.name}' is unreachable at {host.url} "
                f"for the delete call"
            ),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach host '{host.name}': {e}",
        )


# ── Orchestrator ────────────────────────────────────────────────


def _config_field(instance: dict[str, Any], key: str) -> Any:
    """Read a field from the instance dict, checking nested config first."""
    config = instance.get("config", {})
    if isinstance(config, dict) and key in config:
        return config[key]
    return instance.get(key)


def _build_result(
    migration_id: str,
    source_host: Host,
    target_host: Host,
    source_instance_id: str,
    alias: str,
    model_source: str,
    priority: str,
    target_instance_id: str | None,
    steps: list[MigrationStep],
    *,
    status: str = "completed",
    error: str | None = None,
) -> MigrationResult:
    """Build a MigrationResult with consistent field population."""
    return MigrationResult(
        migration_id=migration_id,
        status=status,
        source_host_id=source_host.id,
        source_host_name=source_host.name,
        target_host_id=target_host.id,
        target_host_name=target_host.name,
        source_instance_id=source_instance_id,
        target_instance_id=target_instance_id,
        alias=alias,
        model_source=model_source,
        priority=priority,
        steps=steps,
        error=error,
    )


async def _settle_owning_intent(alias: str) -> None:
    """Keep the reconciler off the intent owning *alias* for a settle window.

    The reconciler's own MIGRATE/EVACUATE actions settle the displaced
    intent before moving its instance; the API migration path had no such
    protection. A tick that lands between the source's stop and its
    disown would RECREATE the source, and one between the disown and the
    target's WS push landing in the instance cache observes a shortfall
    and races a duplicate CREATE against the target being placed — the
    duplicate then surplus-stops (and deletes) the freshly placed target
    (the D-017 regression seen in CI). Mirrors the reconciler's
    ``_MIGRATE_SETTLE_S`` for the API-driven path.

    Best-effort: the settle is race protection, never a migration
    dependency — a lookup failure must not fail the migration.
    """
    from app.database.intents import intent_db
    from app.services.reconciliation import _MIGRATE_SETTLE_S, reconciler

    try:
        intent = await intent_db.get_intent_by_alias(alias)
    except Exception:
        logger.warning(
            "Could not look up intent for alias '%s' to settle it",
            alias,
            exc_info=True,
        )
        return
    if intent is not None:
        reconciler.settle_intent(intent.id, _MIGRATE_SETTLE_S)


def _build_target_create(instance_config: dict[str, Any], path: str) -> dict[str, Any]:
    """Build the create wrapper for a migration/evacuation target.

    Shared by ``execute_migration`` and ``execute_evacuation`` so the
    target payload can never drift between the two paths: resolved model
    path from ``ensure_model_on_target``, instance-level fields stripped,
    and the ownership markers (``managed_by``/``intent_id``, S-037/D-017
    G3) plus ``priority`` preserved at top level — they are host-level
    instance fields (S-036), not config keys.
    """
    config = instance_config.get("config", instance_config)

    # Remove host-assigned and instance-level fields from the config dict.
    # managed_by and intent_id are ownership markers that survive migration.
    _INSTANCE_FIELDS = frozenset(
        {
            "id",
            "status",
            "port",
            "pid",
            "api_key",
            "supported_endpoints",
            "created_at",
            "started_at",
            "error_message",
            "retry_count",
            "busy",
            "prefill_progress",
            "active_slots",
        }
    )
    create_payload: dict[str, Any] = {
        k: v for k, v in config.items() if k not in _INSTANCE_FIELDS
    }
    # Set model to the path resolved by ensure_model_on_target so the
    # host does not need to resolve model_source itself (which would
    # reject repo:// URIs without the companion host-side fix).
    # Preserve model_source alongside the resolved path for intent
    # linking and cross-host operations (S-037/D-017).
    backend_type = str(create_payload.get("backend_type", "llamacpp"))
    if backend_type.startswith("huggingface"):
        create_payload["model_id"] = path
    else:
        create_payload["model"] = path
    # Ensure key fields from captured config are present.
    for key in ("alias", "backend_type"):
        if key not in create_payload:
            val = _config_field(instance_config, key)
            if val is not None:
                create_payload[key] = val

    create_wrapper: dict[str, Any] = {"config": create_payload}
    for field in ("managed_by", "intent_id", "priority"):
        val = _config_field(instance_config, field)
        if val is not None:
            create_wrapper[field] = val
    return create_wrapper


async def execute_migration(
    *,
    instance_id: str,
    source_host_id: str,
    target_host_id: str,
    allow_production: bool = False,
) -> MigrationResult:
    """Execute a full migration of *instance_id* from source to target host.

    Orchestrates all validation, model distribution, stop, and create
    steps. Returns a ``MigrationResult`` with per-step status on success.
    Raises ``HTTPException`` for fatal errors.
    """
    migration_id = str(uuid.uuid4())
    steps: list[MigrationStep] = []

    # ── 1. Validate hosts ───────────────────────────────────────
    source_host = await host_db.get_host(source_host_id)
    if not source_host:
        raise HTTPException(
            status_code=404,
            detail=f"Source host '{source_host_id}' not found",
        )

    target_host = await host_db.get_host(target_host_id)
    if not target_host:
        raise HTTPException(
            status_code=404,
            detail=f"Target host '{target_host_id}' not found",
        )

    if source_host_id == target_host_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Source and target host are the same ('{source_host.name}'). "
                f"Migration requires two distinct hosts."
            ),
        )

    steps.append(MigrationStep(step="validate_hosts", status="ok"))

    # ── 1.5. Check source host has no active training jobs ──────
    await check_no_active_training(source_host)
    steps.append(MigrationStep(step="check_training_jobs", status="ok"))

    # ── 2. Capture instance configuration ───────────────────────
    instance_config = await capture_instance_config(source_host, instance_id)

    alias = _config_field(instance_config, "alias")
    model_source = _config_field(instance_config, "model_source")
    priority = _config_field(instance_config, "priority") or "production"
    source_gpu_type = source_host.gpu_type

    if not alias:
        raise HTTPException(
            status_code=422,
            detail="Instance configuration is missing required 'alias' field",
        )
    if not model_source:
        raise HTTPException(
            status_code=422,
            detail="Instance configuration is missing required 'model_source' field",
        )

    # Validate captured priority before any destructive operations (S-036/S-037).
    # Legacy instances may have invalid priorities that would fail at create_target
    # step (step 7) after the source instance has already been stopped.
    from app.validation import VALID_PRIORITIES

    if priority not in VALID_PRIORITIES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Captured instance has invalid priority '{priority}'. "
                f"Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"
            ),
        )

    steps.append(
        MigrationStep(
            step="capture_config",
            status="ok",
            detail={
                "alias": alias,
                "model_source": model_source,
                "priority": priority,
            },
        )
    )

    # Keep the reconciler off this intent while the migration runs: a tick
    # between the source's stop and its disown would RECREATE the source,
    # and one between the disown and the target's WS push landing would
    # observe a shortfall and duplicate-CREATE the target (D-017).
    await _settle_owning_intent(alias)

    # ── 3. Validate target fitness ──────────────────────────────
    await validate_target_fitness(
        target_host,
        instance_config,
        allow_production=allow_production,
        source_gpu_type=source_gpu_type,
    )
    steps.append(MigrationStep(step="validate_target", status="ok"))

    # ── 4. Check one-replica-per-host ───────────────────────────
    await check_one_replica_per_host(target_host, alias)
    steps.append(MigrationStep(step="check_anti_affinity", status="ok"))

    # ── 5. Ensure model on target ───────────────────────────────
    try:
        path, cached = await ensure_model_on_target(
            target_host,
            model_source,
            _config_field(instance_config, "file_filters"),
        )
        steps.append(
            MigrationStep(
                step="ensure_model",
                status="ok",
                detail={"path": path, "cached": cached},
            )
        )
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="ensure_model",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            None,
            steps,
            status="failed",
            error=f"Ensure model failed: {e.detail}",
        )

    # ── 6. Stop source instance ─────────────────────────────────
    try:
        await stop_source_instance(source_host, instance_id)
        steps.append(MigrationStep(step="stop_source", status="ok"))
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="stop_source",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            None,
            steps,
            status="failed",
            error=f"Stop source failed: {e.detail}",
        )

    # ── 6.5 Disown source instance ──────────────────────────────
    # S-037/D-017: the source stays stopped but is released from the
    # intent so the reconciler no longer manages (or recreates) it.
    try:
        await disown_source_instance(
            source_host,
            instance_id,
            instance_config.get("config", instance_config),
        )
        steps.append(MigrationStep(step="disown_source", status="ok"))
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="disown_source",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            None,
            steps,
            status="failed",
            error=f"Disown source failed: {e.detail}",
        )

    # ── 7. Create target instance ───────────────────────────────
    target_instance: dict[str, Any]
    try:
        create_wrapper = _build_target_create(instance_config, path)
        target_instance = await create_instance_on_host(target_host, create_wrapper)
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="create_target",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            None,
            steps,
            status="failed",
            error=f"Create target failed: {e.detail}",
        )

    # The host wraps created instances in {"instance": {...}, "message": "..."}
    created = target_instance.get("instance", target_instance)
    target_instance_id = created.get("id") or created.get("instance_id") or ""
    steps.append(
        MigrationStep(
            step="create_target",
            status="ok",
            detail={"target_instance_id": target_instance_id},
        )
    )

    # Refresh the settle so the target's WS push lands in the instance
    # cache before the next diff — a slow pull can outlast the
    # pre-migration window, and the post-create gap is where the
    # duplicate-CREATE race lives.
    await _settle_owning_intent(alias)

    logger.info(
        "Migration %s completed: %s/%s (%s) → %s/%s",
        migration_id,
        source_host.name,
        instance_id,
        alias,
        target_host.name,
        target_instance_id,
    )

    return _build_result(
        migration_id,
        source_host,
        target_host,
        instance_id,
        alias,
        model_source,
        priority,
        target_instance_id,
        steps,
    )


async def execute_evacuation(
    *,
    instance_id: str,
    source_host_id: str,
    target_host_id: str,
) -> MigrationResult:
    """Evacuate *instance_id* off a draining *source_host* (S-043 §4.2).

    Create-then-stop ordering, unlike ``execute_migration``: the
    replacement is created and started on the target (blocking on
    log-gated readiness) BEFORE the source is stopped, so the alias keeps
    serving throughout — a drain never reduces serving capacity on its
    own (host-draining.md §1). The source is then stopped and deleted.
    There is no disown step: the source is removed entirely, and staying
    owned until the delete succeeds keeps a failed delete retryable — the
    next drain tick's STOP action (stop + delete) picks up the stopped
    managed replica and finishes the job. A disowned leftover would be
    invisible to the reconciler and become a permanent manual-instance
    conflict (host-draining.md §4.2).

    Returns a ``MigrationResult`` with per-step status. On any step
    failure the result is ``status=\"failed\"`` with the failing step
    marked; the source is only touched after the target is confirmed
    running.
    """
    migration_id = str(uuid.uuid4())
    steps: list[MigrationStep] = []

    # ── 1. Validate hosts ───────────────────────────────────────
    source_host = await host_db.get_host(source_host_id)
    if not source_host:
        raise HTTPException(
            status_code=404,
            detail=f"Source host '{source_host_id}' not found",
        )

    target_host = await host_db.get_host(target_host_id)
    if not target_host:
        raise HTTPException(
            status_code=404,
            detail=f"Target host '{target_host_id}' not found",
        )

    if source_host_id == target_host_id:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Source and target host are the same ('{source_host.name}'). "
                f"Evacuation requires two distinct hosts."
            ),
        )

    steps.append(MigrationStep(step="validate_hosts", status="ok"))

    # ── 1.5. Check source host has no active training jobs ──────
    await check_no_active_training(source_host)
    steps.append(MigrationStep(step="check_training_jobs", status="ok"))

    # ── 2. Capture instance configuration ───────────────────────
    instance_config = await capture_instance_config(source_host, instance_id)

    alias = _config_field(instance_config, "alias")
    model_source = _config_field(instance_config, "model_source")
    priority = _config_field(instance_config, "priority") or "production"
    source_gpu_type = source_host.gpu_type

    if not alias:
        raise HTTPException(
            status_code=422,
            detail="Instance configuration is missing required 'alias' field",
        )
    if not model_source:
        raise HTTPException(
            status_code=422,
            detail="Instance configuration is missing required 'model_source' field",
        )

    # Validate captured priority before any destructive operations
    # (S-036/S-037 pattern; a legacy instance with an invalid priority
    # must fail here, not after the target is up).
    from app.validation import VALID_PRIORITIES

    if priority not in VALID_PRIORITIES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Captured instance has invalid priority '{priority}'. "
                f"Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"
            ),
        )

    steps.append(
        MigrationStep(
            step="capture_config",
            status="ok",
            detail={
                "alias": alias,
                "model_source": model_source,
                "priority": priority,
            },
        )
    )

    # Keep the reconciler off this intent while the evacuation runs: a
    # tick between the target's create and its WS push would observe a
    # shortfall and race a duplicate CREATE (D-017 pattern).
    await _settle_owning_intent(alias)

    # ── 3. Validate target fitness ──────────────────────────────
    # allow_production=True: an operator's drain request is the explicit
    # policy decision the S-037 production safeguard asks for (§4.2).
    await validate_target_fitness(
        target_host,
        instance_config,
        allow_production=True,
        source_gpu_type=source_gpu_type,
    )
    steps.append(MigrationStep(step="validate_target", status="ok"))

    # ── 4. Check one-replica-per-host ───────────────────────────
    await check_one_replica_per_host(target_host, alias)
    steps.append(MigrationStep(step="check_anti_affinity", status="ok"))

    # ── 5. Ensure model on target ───────────────────────────────
    # The long step — the source keeps serving throughout it.
    try:
        path, cached = await ensure_model_on_target(
            target_host,
            model_source,
            _config_field(instance_config, "file_filters"),
        )
        steps.append(
            MigrationStep(
                step="ensure_model",
                status="ok",
                detail={"path": path, "cached": cached},
            )
        )
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="ensure_model",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            None,
            steps,
            status="failed",
            error=f"Ensure model failed: {e.detail}",
        )

    # ── 6. Create target instance FIRST (create-then-stop) ──────
    # The source is still serving at this point; it only goes down after
    # the target is confirmed running (step 7).
    try:
        create_wrapper = _build_target_create(instance_config, path)
        target_instance = await create_instance_on_host(target_host, create_wrapper)
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="create_target",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            None,
            steps,
            status="failed",
            error=f"Create target failed: {e.detail}",
        )

    # The host wraps created instances in {"instance": {...}, "message": "..."}
    created = target_instance.get("instance", target_instance)
    target_instance_id = created.get("id") or created.get("instance_id") or ""
    steps.append(
        MigrationStep(
            step="create_target",
            status="ok",
            detail={"target_instance_id": target_instance_id},
        )
    )

    # Refresh the settle so the target's WS push lands in the instance
    # cache before the next diff observes the two-replica state.
    await _settle_owning_intent(alias)

    # ── 7. Start target (blocking on log-gated readiness) ───────
    try:
        await start_instance_on_host(target_host, target_instance_id)
        steps.append(MigrationStep(step="start_target", status="ok"))
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="start_target",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        # A replica the host refused to start is not a replica: delete it
        # so no dead instance piles up and none counts as observed (CREATE
        # executor pattern). The source never went down — the drain
        # stalls (§4.3) and the alias keeps serving.
        try:
            await delete_instance_on_host(target_host, target_instance_id)
        except Exception:
            logger.warning(
                "Failed to delete target instance %s on %s after failed start",
                target_instance_id,
                target_host.name,
                exc_info=True,
            )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            None,
            steps,
            status="failed",
            error=f"Start target failed: {e.detail}",
        )

    # The replacement is running; the source can now be retired. Refresh
    # the settle once more so a mid-overlap tick cannot surplus-stop the
    # fresh target (the surplus logic prefers draining-host replicas, but
    # the settle makes even that unnecessary).
    await _settle_owning_intent(alias)

    # ── 8. Stop source ──────────────────────────────────────────
    try:
        await stop_source_instance(source_host, instance_id)
        steps.append(MigrationStep(step="stop_source", status="ok"))
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="stop_source",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            target_instance_id,
            steps,
            status="failed",
            error=f"Stop source failed: {e.detail}",
        )

    # ── 9. Delete source ────────────────────────────────────────
    # The drain contract is that the host ends up genuinely empty
    # (host-draining.md §4.2). No disown: see the docstring — the stopped
    # managed replica must stay visible so a failed delete is retried.
    try:
        await delete_instance_on_host(source_host, instance_id)
        steps.append(MigrationStep(step="delete_source", status="ok"))
    except HTTPException as e:
        steps.append(
            MigrationStep(
                step="delete_source",
                status="failed",
                detail={"error": str(e.detail), "status_code": e.status_code},
            )
        )
        return _build_result(
            migration_id,
            source_host,
            target_host,
            instance_id,
            alias,
            model_source,
            priority,
            target_instance_id,
            steps,
            status="failed",
            error=f"Delete source failed: {e.detail}",
        )

    # Refresh the settle so the source's disappearance lands before the
    # next diff.
    await _settle_owning_intent(alias)

    logger.info(
        "Evacuation %s completed: %s/%s (%s) → %s/%s",
        migration_id,
        source_host.name,
        instance_id,
        alias,
        target_host.name,
        target_instance_id,
    )

    return _build_result(
        migration_id,
        source_host,
        target_host,
        instance_id,
        alias,
        model_source,
        priority,
        target_instance_id,
        steps,
    )
