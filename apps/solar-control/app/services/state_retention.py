"""Daily retention job for the instance-state history table.

Deletes samples older than ``state_history_retention_days`` so the table that
backs the status page stays bounded while the last five years are kept.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.database.state_history import state_history_db
from app.redis_state.connection import redis_client

logger = logging.getLogger(__name__)

_LOCK_KEY = "solar:state_retention:lock"
# Fraction of the interval the run lock is held. Below 1 so a replica can take
# the next run despite scheduling jitter.
_LOCK_INTERVAL_FRACTION = 0.9


class StateHistoryRetention:
    """Background loop that prunes expired state-history rows."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background cleanup loop."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "State history retention started (keep=%dd, interval=%.0fs)",
            settings.state_history_retention_days,
            settings.state_history_cleanup_interval_s,
        )

    async def stop(self) -> None:
        """Stop the background cleanup loop."""
        self._running = False
        self._stop_event.set()  # unblock any sleep
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("State history retention stopped")

    # ── Main loop ──────────────────────────────────────────────

    async def _loop(self) -> None:
        """Main loop: clean up → sleep → repeat."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("State history cleanup failed")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=settings.state_history_cleanup_interval_s,
                )
            except asyncio.TimeoutError:
                pass  # interval elapsed — normal

    async def _tick(self) -> int:
        """Clean up unless another replica already did this interval."""
        lock_ms = max(
            1,
            int(
                settings.state_history_cleanup_interval_s
                * 1000
                * _LOCK_INTERVAL_FRACTION
            ),
        )
        acquired = await redis_client().set(_LOCK_KEY, "1", nx=True, px=lock_ms)
        if not acquired:
            return 0
        return await self.cleanup_once()

    async def cleanup_once(self, now: datetime | None = None) -> int:
        """Delete rows beyond the retention window. Returns rows deleted."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=settings.state_history_retention_days)
        deleted = await state_history_db.delete_older_than(cutoff)
        if deleted:
            logger.info(
                "State history cleanup removed %d rows older than %s",
                deleted,
                cutoff.isoformat(),
            )
        return deleted


state_history_retention = StateHistoryRetention()
