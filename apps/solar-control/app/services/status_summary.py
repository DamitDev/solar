"""Public status summary: current availability and 90-day uptime.

Two independent sources feed every component:

- *current availability* is live: the Redis registry plus host snapshot
  freshness, via the same observation the sampler records;
- *uptime history* comes from ``instance_state_history`` only, never from
  ``gateway_requests``, so a requestless outage still counts as downtime.

Components are virtual models, aliases (e.g. ``core``, ``wind``) and
instances of aliases no intent manages. An intent-managed alias carries the
intent's reconciliation phase instead of listing each replica.

The public page is read during incidents, so every source degrades on its own:
an unreadable registry, history or message table yields empty data and an
``unknown`` availability instead of a failed response.
"""

import logging
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

logger = logging.getLogger(__name__)

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
    ) -> Buckets | None:
        """Cached per-component buckets, ``None`` when history is unreadable."""
        bucket_s = TIMESERIES_BUCKETS[resolve_bucket(start, end, BUCKET)]
        key = (
            "buckets",
            start,
            tuple(aliases) if aliases is not None else None,
            instance_id,
        )
        try:
            return await self._cached(
                key,
                lambda: state_history_db.availability_buckets(
                    start, end, bucket_s, aliases=aliases, instance_id=instance_id
                ),
            )
        except Exception:
            logger.warning("Status summary: uptime history read failed", exc_info=True)
            return None

    async def _uptime(
        self,
        start: datetime,
        end: datetime,
        *,
        aliases: list[str] | None = None,
        instance_id: str | None = None,
    ) -> tuple[list[UptimePoint], float | None] | None:
        """Uptime series for one component, ``None`` when unreadable."""
        buckets = await self._buckets(
            start, end, aliases=aliases, instance_id=instance_id
        )
        if buckets is None:
            return None
        return uptime_series(start, end, buckets)

    async def _registry(self) -> dict[str, list[RegistryEntry]] | None:
        """Live registry, ``None`` when Redis cannot be read."""
        try:
            return await registry_store.get_registry()
        except Exception:
            logger.warning("Status summary: registry read failed", exc_info=True)
            return None

    async def _live(
        self, now: datetime, registry: dict[str, list[RegistryEntry]] | None
    ) -> list[StateSample] | None:
        """Live observations, ``None`` when the registry is unreadable."""
        if registry is None:
            return None
        try:
            return await state_sampler.observe(now, registry)
        except Exception:
            logger.warning("Status summary: live state read failed", exc_info=True)
            return None

    async def _intent_phase(self) -> dict[str, str]:
        """Alias -> reconciliation phase; empty when intents are unreadable."""
        try:
            intents = await intent_db.list_intents(limit=_INTENT_LIMIT)
        except Exception:
            logger.warning("Status summary: intent listing failed", exc_info=True)
            return {}
        return {intent.alias: intent.status.phase.value for intent in intents}

    async def _components(
        self, start: datetime, end: datetime
    ) -> list[tuple[str, str]] | None:
        """Sampled ``(alias, instance_id)`` pairs, ``None`` when unreadable."""
        try:
            return await self._cached(
                ("components", start),
                lambda: state_history_db.list_components(start, end),
            )
        except Exception:
            logger.warning(
                "Status summary: component history read failed", exc_info=True
            )
            return None

    async def _messages(self, now: datetime) -> list[StatusMessageResponse]:
        """Visible status messages, empty when Postgres cannot be read."""
        try:
            return visible_messages(await status_message_db.list_not_ended(now), now)
        except Exception:
            logger.warning("Status summary: status message read failed", exc_info=True)
            return []

    async def _virtual_models(self) -> list[VirtualModelResponse]:
        """Virtual models, empty when the registry table cannot be read."""
        try:
            entries = virtual_model_cache.get_all()
            if entries is None:
                entries = await virtual_model_db.list_all()
                virtual_model_cache.set_all(entries)
            return entries
        except Exception:
            logger.warning("Status summary: virtual model read failed", exc_info=True)
            return []

    async def build(self) -> StatusSummary:
        now = datetime.now(timezone.utc)
        start = window_start(now)

        # Every external source degrades on its own: the public status page must
        # stay up (and say "unknown", never a confident "down") during an
        # infrastructure incident.
        registry = await self._registry()
        live = await self._live(now, registry)
        intent_phase = await self._intent_phase()
        history = await self._components(start, now)

        running_by_alias, total_by_alias = _count_by_alias(live or [])

        virtual_models = [
            await self._virtual_model(
                vm, registry, running_by_alias, live is not None, start, now
            )
            for vm in await self._virtual_models()
        ]

        alias_names = sorted(
            set(registry or {}) | {alias for alias, _ in history or []}
        )
        aliases = []
        for name in alias_names:
            running = running_by_alias.get(name, 0)
            total = total_by_alias.get(name, 0)
            uptime = await self._uptime(start, now, aliases=[name])
            points, pct = uptime if uptime is not None else ([], None)
            aliases.append(
                AliasStatus(
                    alias=name,
                    availability=_availability(
                        running,
                        total,
                        known=live is not None,
                    ),
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

        messages = await self._messages(now)

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
        registry: dict[str, list[RegistryEntry]] | None,
        running_by_alias: dict[str, int],
        live_ok: bool,
        start: datetime,
        end: datetime,
    ) -> VirtualModelStatus:
        # An unreadable registry has no per-target verdicts: "missing" would
        # contradict the "unknown" availability below.
        target_status: dict[str, str] = {}
        if registry is not None:
            target_status = enrich_target_status(vm, registry).target_status or {}
        serving = sum(
            1
            for target in vm.targets
            if running_by_alias.get(target, 0) > 0
            and target_status.get(target) in _SERVING_TARGET_STATUS
        )
        uptime = await self._uptime(start, end, aliases=list(vm.targets))
        points, pct = uptime if uptime is not None else ([], None)
        return VirtualModelStatus(
            name=vm.name,
            description=vm.description,
            targets=list(vm.targets),
            target_status=target_status,
            availability=(
                availability_of(serving, len(vm.targets)) if live_ok else "unknown"
            ),
            uptime_pct=pct,
            history=points,
        )

    async def _instances(
        self,
        live: list[StateSample] | None,
        history: list[tuple[str, str]] | None,
        *,
        managed_aliases: set[str],
        start: datetime,
        end: datetime,
    ) -> list[InstanceStatus]:
        """Instances whose alias no intent manages, live or in the window."""
        alias_of: dict[str, str] = {}
        for alias, instance_id in history or []:
            alias_of[instance_id] = alias
        running: dict[str, bool] = {}
        for sample in live or []:
            alias_of[sample.instance_id] = sample.alias
            running[sample.instance_id] = sample.state == STATE_RUNNING

        result = []
        for instance_id in sorted(alias_of):
            alias = alias_of[instance_id]
            if alias in managed_aliases:
                continue
            uptime = await self._uptime(start, end, instance_id=instance_id)
            points, pct = uptime if uptime is not None else ([], None)
            result.append(
                InstanceStatus(
                    instance_id=instance_id,
                    alias=alias,
                    availability=(
                        ("operational" if running.get(instance_id) else "down")
                        if live is not None
                        else "unknown"
                    ),
                    uptime_pct=pct,
                    history=points,
                )
            )
        return result


def _availability(running: int, total: int, *, known: bool) -> Availability:
    """``unknown`` when the live source is missing, else the live verdict."""
    return availability_of(running, total) if known else "unknown"


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
