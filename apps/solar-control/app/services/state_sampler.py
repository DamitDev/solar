"""Periodic instance-state sampler.

Writes one history row per known instance every ``state_sample_interval_s``
so uptime can be computed from samples rather than request traffic: a
requestless outage still leaves "offline" rows, or no rows at all, instead of
looking like silence.

State is derived from the WS-first host resource snapshot (``host_health``
push, stored by control with its own receive time): a registered instance whose
host has a fresh snapshot is ``running``, anything else is ``offline``. The
registry only lists instances a host reports as ``running``, so an instance that
stops or crashes de-registers there; the host instance cache keeps the last full
list the host pushed and supplies those ``offline`` rows.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.database.state_history import (
    STATE_OFFLINE,
    STATE_RUNNING,
    StateSample,
    state_history_db,
)
from app.models.gateway import RegistryEntry
from app.redis_state import host_store, registry_store
from app.redis_state.connection import redis_client
from app.redis_state.freshness import entry_age_s

logger = logging.getLogger(__name__)

_LOCK_KEY = "solar:state_sampler:lock"
# Fraction of the interval the tick lock is held. Below 1 so the replica that
# won the previous tick can take the next one despite scheduling jitter.
_LOCK_INTERVAL_FRACTION = 0.9


class StateSampler:
    """Background loop that records the state of every registered instance."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background sampling loop."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "State sampler started (interval=%.1fs)", settings.state_sample_interval_s
        )

    async def stop(self) -> None:
        """Stop the background sampling loop."""
        self._running = False
        self._stop_event.set()  # unblock any sleep
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("State sampler stopped")

    # ── Main loop ──────────────────────────────────────────────

    async def _loop(self) -> None:
        """Main loop: sample → sleep → repeat."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("State sampling pass failed")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=settings.state_sample_interval_s,
                )
            except asyncio.TimeoutError:
                pass  # interval elapsed — normal

    async def _tick(self) -> int:
        """Sample once unless another replica already did this interval."""
        lock_ms = max(
            1,
            int(settings.state_sample_interval_s * 1000 * _LOCK_INTERVAL_FRACTION),
        )
        acquired = await redis_client().set(_LOCK_KEY, "1", nx=True, px=lock_ms)
        if not acquired:
            return 0
        return await self.sample_once()

    async def sample_once(self) -> int:
        """Write one row per known instance. Returns rows written."""
        samples = await self.observe(datetime.now(timezone.utc))
        return await state_history_db.insert_batch(samples)

    async def observe(
        self,
        sampled_at: datetime,
        registry: dict[str, list[RegistryEntry]] | None = None,
    ) -> list[StateSample]:
        """Current state of every known instance, stamped *sampled_at*.

        A caller that already holds the registry passes it to avoid a second
        read.

        Shared by the sampling tick and the live half of the status summary,
        so "currently running" and "recorded as running" cannot disagree.

        The registry lists only instances a host reports as ``running``: a stop
        or crash on a healthy host de-registers and would otherwise leave no
        row at all. Instances the registry does not list are read from the host
        instance cache and recorded as ``offline``, so the outage is downtime
        while it lasts.
        """
        if registry is None:
            registry = await registry_store.get_registry()
        host_fresh: dict[str, bool] = {}
        samples: list[StateSample] = []
        sampled: set[tuple[str, str]] = set()
        for alias, entries in registry.items():
            for entry in entries:
                if entry.host_id not in host_fresh:
                    host_fresh[entry.host_id] = await self._is_host_snapshot_fresh(
                        entry.host_id
                    )
                samples.append(
                    StateSample(
                        alias=alias,
                        instance_id=entry.instance_id,
                        host_id=entry.host_id,
                        state=(
                            STATE_RUNNING
                            if host_fresh[entry.host_id]
                            else STATE_OFFLINE
                        ),
                        sampled_at=sampled_at,
                    )
                )
                sampled.add((entry.host_id, entry.instance_id))
        samples.extend(await self._not_running_samples(sampled_at, registry, sampled))
        return samples

    async def _not_running_samples(
        self,
        sampled_at: datetime,
        registry: dict[str, list[RegistryEntry]],
        sampled: set[tuple[str, str]],
    ) -> list[StateSample]:
        """``offline`` rows for cached instances the registry does not list.

        The registry carries only instances a host reports as ``running``, so a
        stopped or crashed instance is absent from it while the host instance
        cache still holds the last full list the host pushed. One cache read per
        known host per tick; instances already sampled from the registry are
        skipped so a tick never writes two rows for one instance.
        """
        known_hosts = {
            entry.host_id for entries in registry.values() for entry in entries
        }
        known_hosts.update(await self._instance_cache_host_ids())
        samples: list[StateSample] = []
        for host_id in sorted(known_hosts):
            for instance in await self._cached_instances(host_id):
                instance_id = instance.get("id")
                if not instance_id or (host_id, instance_id) in sampled:
                    continue
                alias = instance.get("alias")
                if not alias:
                    # Without an alias the row cannot be attributed to a
                    # component: a literal "unknown" would become a phantom one.
                    logger.debug(
                        "Skipping cached instance %s on host %s without an alias",
                        instance_id,
                        host_id,
                    )
                    continue
                if instance.get("status") == "running":
                    # The registry may simply not have refreshed yet.
                    continue
                samples.append(
                    StateSample(
                        alias=alias,
                        instance_id=instance_id,
                        host_id=host_id,
                        state=STATE_OFFLINE,
                        sampled_at=sampled_at,
                    )
                )
        return samples

    @staticmethod
    async def _instance_cache_host_ids() -> set[str]:
        """Hosts with a cached instance list; empty when unreadable."""
        try:
            return set(await host_store.get_instance_cache_host_ids())
        except Exception:
            logger.warning("Failed to list hosts with cached instances", exc_info=True)
            return set()

    @staticmethod
    async def _cached_instances(host_id: str) -> list[dict]:
        """Cached instance list for *host_id*; empty when unreadable."""
        try:
            instances = await host_store.get_host_instances(host_id)
        except Exception:
            logger.warning(
                "Failed to read cached instances for host %s", host_id, exc_info=True
            )
            return []
        return instances if isinstance(instances, list) else []

    @staticmethod
    async def _is_host_snapshot_fresh(host_id: str) -> bool:
        """Whether *host_id* pushed a resource snapshot recently enough.

        A missing, malformed or unreadable snapshot counts as not fresh; it
        must never crash the loop.
        """
        try:
            entry = await host_store.get_host_resource_snapshot(host_id)
        except Exception:
            logger.warning(
                "Failed to read snapshot for host %s", host_id, exc_info=True
            )
            return False
        if not isinstance(entry, dict):
            return False
        age = entry_age_s(entry.get("at"))
        return age is not None and age <= settings.host_snapshot_max_age_s


state_sampler = StateSampler()
