"""Periodic instance-state sampler.

Writes one history row per registered instance every ``state_sample_interval_s``
so uptime can be computed from samples rather than request traffic: a
requestless outage still leaves "offline" rows, or no rows at all, instead of
looking like silence.

State is derived from the WS-first host resource snapshot (``host_health``
push, stored by control with its own receive time): an instance whose host has
a fresh snapshot is ``running``, anything else is ``offline``.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.database.state_history import StateSample, state_history_db
from app.redis_state import host_store, registry_store
from app.redis_state.connection import redis_client
from app.redis_state.freshness import entry_age_s

logger = logging.getLogger(__name__)

STATE_RUNNING = "running"
STATE_OFFLINE = "offline"

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
        """Write one row per registered instance. Returns rows written."""
        registry = await registry_store.get_registry()
        if not registry:
            return 0

        sampled_at = datetime.now(timezone.utc)
        host_fresh: dict[str, bool] = {}
        samples: list[StateSample] = []
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
        return await state_history_db.insert_batch(samples)

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
