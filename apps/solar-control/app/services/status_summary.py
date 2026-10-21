"""Public status summary: current availability and 90-day uptime.

Two independent sources feed every component:

- *current availability* is live: the Redis registry plus host snapshot
  freshness, via the same observation the sampler records;
- *uptime history* comes from ``instance_state_history`` only, never from
  ``gateway_requests``, so a requestless outage still counts as downtime.

Components are virtual models, aliases (e.g. ``core``, ``wind``) and
instances of aliases no intent manages. An intent-managed alias carries the
intent's reconciliation phase instead of listing each replica.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.database.intents import intent_db
from app.database.logs import TIMESERIES_BUCKETS, fill_buckets, resolve_bucket
from app.database.state_history import STATE_RUNNING, StateSample, state_history_db
from app.database.status_messages import status_message_db
from app.database.virtual_models import virtual_model_db
from app.models.gateway import RegistryEntry
from app.models.status import (
    AliasStatus,
    Availability,
    InstanceStatus,
    StatusSummary,
    UptimePoint,
    VirtualModelStatus,
)
from app.models.status_message import StatusMessageResponse
from app.models.virtual_model import VirtualModelResponse
from app.redis_state import registry_store
from app.services.state_sampler import state_sampler
from app.services.virtual_model_cache import virtual_model_cache
from app.services.virtual_model_contract import enrich_target_status

WINDOW_DAYS = 90
BUCKET = "1d"
# Intent listing page size; far above any realistic number of intents.
_INTENT_LIMIT = 10_000
# target_status values under which a target can actually serve requests.
_SERVING_TARGET_STATUS = ("satisfied", "unverified")

# The only message type published ahead of its start_time.
UPCOMING_TYPE = "planned maintenance"

Buckets = dict[int, tuple[int, int]]


def availability_of(running: int, total: int) -> Availability:
    """``operational`` when all up, ``down`` when none, else ``degraded``."""
    if total <= 0 or running <= 0:
        return "down"
    return "operational" if running >= total else "degraded"


def window_start(now: datetime) -> datetime:
    """Start of the oldest 1d bucket so the window spans exactly 90 buckets."""
    bucket_s = TIMESERIES_BUCKETS[resolve_bucket(now, now, BUCKET)]
    today = int(now.timestamp()) // bucket_s * bucket_s
    return datetime.fromtimestamp(today - (WINDOW_DAYS - 1) * bucket_s, tz=timezone.utc)


def uptime_series(
    start: datetime, end: datetime, buckets: Buckets
) -> tuple[list[UptimePoint], float | None]:
    """Gap-free daily uptime series and the window uptime percentage.

    ``buckets`` maps bucket epoch -> ``(ticks, ticks_up)`` and is sparse: a
    day without samples is an outage and is emitted as zero, never
    interpolated. The percentage averages the daily ratios from the first
    day that has samples, so gaps after it count as downtime while days
    before the component existed do not.
    """
    bucket_s = TIMESERIES_BUCKETS[resolve_bucket(start, end, BUCKET)]
    aggregates: dict[int, dict[str, Any]] = {
        epoch: {
            "success": up,
            "error": total - up,
            "missed": 0,
            "token_in": 0,
            "token_cached": 0,
            "token_out": 0,
            "avg_duration_s": None,
        }
        for epoch, (total, up) in buckets.items()
    }
    filled = fill_buckets(start, end, bucket_s, aggregates)
    points = [
        UptimePoint(
            ts=p["ts"],
            uptime=(
                p["success"] / (p["success"] + p["error"])
                if p["success"] + p["error"]
                else 0.0
            ),
            samples=p["success"] + p["error"],
        )
        for p in filled
    ]
    first = next((i for i, p in enumerate(points) if p.samples), None)
    if first is None:
        return points, None
    counted = points[first:]
    pct = sum(p.uptime for p in counted) / len(counted) * 100
    return points, round(pct, 3)


def visible_messages(
    messages: list[StatusMessageResponse], now: datetime
) -> list[StatusMessageResponse]:
    """Messages the public read exposes at ``now``, purely from their windows.

    Ended messages (``end_time <= now``) are dropped for every type. A message
    whose ``start_time`` is still in the future is hidden, except planned
    maintenance, which is published ahead of time. Telling an upcoming message
    from an active one is left to the client (``start_time`` vs
    ``generated_at``).
    """
    return [
        message
        for message in messages
        if (message.end_time is None or message.end_time > now)
        and (message.start_time <= now or message.type == UPCOMING_TYPE)
    ]


class StatusSummaryService:
    def __init__(self) -> None:
        self._cache: dict[Any, Any] = {}
        self._expires_at = 0.0

    def clear_cache(self) -> None:
        self._cache = {}
        self._expires_at = 0.0

    async def _cached(self, key: Any, loader: Callable[[], Awaitable[Any]]) -> Any:
        """Reuse a history aggregation for ``status_history_cache_ttl_s``."""
        now = time.monotonic()
        if now >= self._expires_at:
            self._cache = {}
            self._expires_at = now + settings.status_history_cache_ttl_s
        if key not in self._cache:
            self._cache[key] = await loader()
        return self._cache[key]

    async def _buckets(
        self,
        start: datetime,
        end: datetime,
        *,
        aliases: list[str] | None = None,
        instance_id: str | None = None,
    ) -> Buckets:
        bucket_s = TIMESERIES_BUCKETS[resolve_bucket(start, end, BUCKET)]
        key = (
            "buckets",
            start,
            tuple(aliases) if aliases is not None else None,
            instance_id,
        )
        return await self._cached(
            key,
            lambda: state_history_db.availability_buckets(
                start, end, bucket_s, aliases=aliases, instance_id=instance_id
            ),
        )

    async def _virtual_models(self) -> list[VirtualModelResponse]:
        entries = virtual_model_cache.get_all()
        if entries is None:
            entries = await virtual_model_db.list_all()
            virtual_model_cache.set_all(entries)
        return entries

    async def build(self) -> StatusSummary:
        now = datetime.now(timezone.utc)
        start = window_start(now)

        registry = await registry_store.get_registry()
        live = await state_sampler.observe(now, registry)
        intent_phase = {
            intent.alias: intent.status.phase.value
            for intent in await intent_db.list_intents(limit=_INTENT_LIMIT)
        }
        history = await self._cached(
            ("components", start),
            lambda: state_history_db.list_components(start, now),
        )

        running_by_alias, total_by_alias = _count_by_alias(live)

        virtual_models = [
            await self._virtual_model(vm, registry, running_by_alias, start, now)
            for vm in await self._virtual_models()
        ]

        alias_names = sorted(set(registry) | {alias for alias, _ in history})
        aliases = []
        for name in alias_names:
            running = running_by_alias.get(name, 0)
            total = total_by_alias.get(name, 0)
            points, pct = uptime_series(
                start, now, await self._buckets(start, now, aliases=[name])
            )
            aliases.append(
                AliasStatus(
                    alias=name,
                    availability=availability_of(running, total),
                    instances_running=running,
                    instances_total=total,
                    intent_phase=intent_phase.get(name),
                    uptime_pct=pct,
                    history=points,
                )
            )

        instances = await self._instances(
            live, history, managed_aliases=set(intent_phase), start=start, end=now
        )

        messages = visible_messages(await status_message_db.list_not_ended(now), now)

        return StatusSummary(
            generated_at=now.isoformat(),
            window_days=WINDOW_DAYS,
            bucket=BUCKET,
            virtual_models=virtual_models,
            aliases=aliases,
            instances=instances,
            messages=messages,
        )

    async def _virtual_model(
        self,
        vm: VirtualModelResponse,
        registry: dict[str, list[RegistryEntry]],
        running_by_alias: dict[str, int],
        start: datetime,
        end: datetime,
    ) -> VirtualModelStatus:
        enriched = enrich_target_status(vm, registry)
        target_status = enriched.target_status or {}
        serving = sum(
            1
            for target in vm.targets
            if running_by_alias.get(target, 0) > 0
            and target_status.get(target) in _SERVING_TARGET_STATUS
        )
        points, pct = uptime_series(
            start, end, await self._buckets(start, end, aliases=list(vm.targets))
        )
        return VirtualModelStatus(
            name=vm.name,
            description=vm.description,
            targets=list(vm.targets),
            target_status=target_status,
            availability=availability_of(serving, len(vm.targets)),
            uptime_pct=pct,
            history=points,
        )

    async def _instances(
        self,
        live: list[StateSample],
        history: list[tuple[str, str]],
        *,
        managed_aliases: set[str],
        start: datetime,
        end: datetime,
    ) -> list[InstanceStatus]:
        """Instances whose alias no intent manages, live or in the window."""
        alias_of: dict[str, str] = {}
        for alias, instance_id in history:
            alias_of[instance_id] = alias
        running: dict[str, bool] = {}
        for sample in live:
            alias_of[sample.instance_id] = sample.alias
            running[sample.instance_id] = sample.state == STATE_RUNNING

        result = []
        for instance_id in sorted(alias_of):
            alias = alias_of[instance_id]
            if alias in managed_aliases:
                continue
            points, pct = uptime_series(
                start, end, await self._buckets(start, end, instance_id=instance_id)
            )
            result.append(
                InstanceStatus(
                    instance_id=instance_id,
                    alias=alias,
                    availability="operational" if running.get(instance_id) else "down",
                    uptime_pct=pct,
                    history=points,
                )
            )
        return result


def _count_by_alias(
    samples: list[StateSample],
) -> tuple[dict[str, int], dict[str, int]]:
    running: dict[str, int] = {}
    total: dict[str, int] = {}
    for sample in samples:
        total[sample.alias] = total.get(sample.alias, 0) + 1
        if sample.state == STATE_RUNNING:
            running[sample.alias] = running.get(sample.alias, 0) + 1
    return running, total


status_summary_service = StatusSummaryService()
