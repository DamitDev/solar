"""Tests for the status summary's uptime math and availability rules."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.dialects import postgresql

from app.database.state_history import StateHistoryDB
from app.services.status_summary import (
    WINDOW_DAYS,
    availability_of,
    uptime_series,
    window_start,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
DAY = 86400


def _epoch(days_ago: int) -> int:
    today = int(NOW.timestamp()) // DAY * DAY
    return today - days_ago * DAY


def test_window_spans_exactly_90_daily_buckets():
    start = window_start(NOW)
    assert start == datetime(2026, 7, 24, tzinfo=timezone.utc)
    points, _ = uptime_series(start, NOW, {})
    assert len(points) == WINDOW_DAYS


def test_gap_free_full_uptime():
    buckets = {_epoch(d): (100, 100) for d in range(WINDOW_DAYS)}
    points, pct = uptime_series(window_start(NOW), NOW, buckets)
    assert all(p.uptime == 1.0 and p.samples == 100 for p in points)
    assert pct == 100.0


def test_partial_day_outage_lowers_that_day_only():
    buckets = {_epoch(d): (100, 100) for d in range(WINDOW_DAYS)}
    buckets[_epoch(3)] = (100, 25)
    points, pct = uptime_series(window_start(NOW), NOW, buckets)
    assert points[-4].uptime == 0.25
    assert pct == pytest.approx((89 + 0.25) / 90 * 100, abs=0.001)


def test_missing_days_are_zero_filled_and_count_as_downtime():
    buckets = {_epoch(0): (100, 100), _epoch(3): (100, 100)}
    points, pct = uptime_series(window_start(NOW), NOW, buckets)
    # Days -2 and -1 have no samples: zeros, not interpolated neighbours.
    assert [p.uptime for p in points[-4:]] == [1.0, 0.0, 0.0, 1.0]
    assert [p.samples for p in points[-4:]] == [100, 0, 0, 100]
    assert pct == 50.0


def test_days_before_first_sample_are_not_counted():
    buckets = {_epoch(1): (100, 100), _epoch(0): (100, 100)}
    points, pct = uptime_series(window_start(NOW), NOW, buckets)
    assert len(points) == WINDOW_DAYS
    assert points[0].uptime == 0.0
    assert pct == 100.0


def test_no_samples_means_unknown_uptime():
    points, pct = uptime_series(window_start(NOW), NOW, {})
    assert pct is None
    assert all(p.uptime == 0.0 and p.samples == 0 for p in points)


def test_all_down_samples_is_zero_uptime():
    points, pct = uptime_series(window_start(NOW), NOW, {_epoch(0): (10, 0)})
    assert points[-1].uptime == 0.0 and points[-1].samples == 10
    assert pct == 0.0


@pytest.mark.parametrize(
    ("running", "total", "expected"),
    [
        (2, 2, "operational"),
        (1, 2, "degraded"),
        (0, 2, "down"),
        (0, 0, "down"),
    ],
)
def test_availability_of(running, total, expected):
    assert availability_of(running, total) == expected


class _FakeSession:
    def __init__(self, rows):
        self.statements: list = []
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.statements.append(stmt)
        return iter(self._rows)


@pytest.mark.asyncio
async def test_availability_buckets_collapses_ticks_and_returns_sparse_map():
    session = _FakeSession([SimpleNamespace(bucket=float(_epoch(0)), total=10, up=7)])
    with patch(
        "app.database.state_history.get_session_factory",
        lambda: lambda: session,
    ):
        out = await StateHistoryDB().availability_buckets(
            NOW - timedelta(days=90), NOW, DAY, aliases=["core", "wind"]
        )

    assert out == {_epoch(0): (10, 7)}
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    # Rows collapse per tick (bool_or) before bucketing, never joining
    # gateway request logs.
    assert "bool_or" in sql
    assert "GROUP BY instance_state_history.sampled_at" in sql
    assert "alias IN ('core', 'wind')" in sql
    assert "gateway_requests" not in sql
