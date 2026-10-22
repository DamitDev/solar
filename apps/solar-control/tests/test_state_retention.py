"""Tests for the state-history retention job."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.database.state_history import StateHistoryDB
from app.services.state_retention import StateHistoryRetention

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class _FakeHistory:
    """In-memory stand-in applying ``sampled_at < cutoff`` like the real delete."""

    def __init__(self, sampled_at: list[datetime]):
        self.rows = list(sampled_at)
        self.cutoffs: list[datetime] = []

    async def delete_older_than(self, cutoff: datetime) -> int:
        self.cutoffs.append(cutoff)
        kept = [t for t in self.rows if t >= cutoff]
        deleted = len(self.rows) - len(kept)
        self.rows = kept
        return deleted


@pytest.mark.asyncio
async def test_retention_boundary_keeps_inside_and_deletes_beyond():
    window = timedelta(days=settings.state_history_retention_days)
    boundary = NOW - window
    fake = _FakeHistory(
        [
            boundary - timedelta(seconds=1),  # just beyond the window
            boundary - timedelta(days=30),  # well beyond
            boundary,  # exactly on the edge
            boundary + timedelta(seconds=1),  # just inside
            NOW,
        ]
    )
    with patch("app.services.state_retention.state_history_db", fake):
        deleted = await StateHistoryRetention().cleanup_once(NOW)

    assert deleted == 2
    assert fake.cutoffs == [boundary]
    assert fake.rows == [boundary, boundary + timedelta(seconds=1), NOW]


def test_default_retention_covers_five_years():
    assert settings.state_history_retention_days >= 5 * 365
    assert settings.state_history_cleanup_interval_s == 86400.0


@pytest.mark.asyncio
async def test_retention_window_is_configurable():
    fake = _FakeHistory([NOW - timedelta(days=10), NOW - timedelta(days=2)])
    with (
        patch("app.services.state_retention.state_history_db", fake),
        patch.object(settings, "state_history_retention_days", 5),
    ):
        deleted = await StateHistoryRetention().cleanup_once(NOW)

    assert deleted == 1
    assert fake.rows == [NOW - timedelta(days=2)]


@pytest.mark.asyncio
async def test_delete_older_than_builds_strict_cutoff_delete():
    statements: list = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            statements.append(stmt)
            return SimpleNamespace(rowcount=7)

        async def commit(self):
            pass

    with patch(
        "app.database.state_history.get_session_factory",
        lambda: lambda: _Session(),
    ):
        deleted = await StateHistoryDB().delete_older_than(NOW)

    assert deleted == 7
    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert sql.startswith("DELETE FROM instance_state_history")
    assert "sampled_at <" in sql and "sampled_at <=" not in sql


@pytest.mark.asyncio
async def test_tick_skipped_when_another_replica_holds_the_lock():
    redis = AsyncMock()
    redis.set.return_value = None  # NX lost
    job = StateHistoryRetention()
    with (
        patch("app.services.state_retention.redis_client", return_value=redis),
        patch.object(job, "cleanup_once", AsyncMock(return_value=1)) as cleanup,
    ):
        assert await job._tick() == 0
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_cleans_up_when_lock_acquired():
    redis = AsyncMock()
    redis.set.return_value = True
    job = StateHistoryRetention()
    with (
        patch("app.services.state_retention.redis_client", return_value=redis),
        patch.object(job, "cleanup_once", AsyncMock(return_value=4)),
    ):
        assert await job._tick() == 4
    assert redis.set.await_args.kwargs["nx"] is True


@pytest.mark.asyncio
async def test_loop_survives_failing_tick_and_stops_cleanly():
    job = StateHistoryRetention()
    calls = 0
    two_calls = asyncio.Event()

    async def _tick():
        nonlocal calls
        calls += 1
        if calls >= 2:
            two_calls.set()
        raise RuntimeError("boom")

    with (
        patch.object(job, "_tick", _tick),
        patch.object(settings, "state_history_cleanup_interval_s", 0.01),
    ):
        await job.start()
        await asyncio.wait_for(two_calls.wait(), timeout=2)
        await job.stop()

    assert calls >= 2
    assert job._task is None
    assert not job._running
