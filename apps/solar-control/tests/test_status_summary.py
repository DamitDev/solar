"""Tests for the status summary's uptime math and availability rules."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.dialects import postgresql

from app.database.state_history import StateHistoryDB
from app.models.status_message import StatusMessageResponse
from app.services.status_summary import (
    WINDOW_DAYS,
    availability_of,
    uptime_series,
    visible_messages,
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
    # Today's bar still renders, but the in-progress day is not in the
    # percentage: 89 complete days, 88 up plus the outage day at 25%.
    assert points[-1].uptime == 1.0
    assert pct == pytest.approx((88 + 0.25) / 89 * 100, abs=0.001)


def test_missing_days_are_zero_filled_and_count_as_downtime():
    buckets = {_epoch(0): (100, 100), _epoch(3): (100, 100)}
    points, pct = uptime_series(window_start(NOW), NOW, buckets)
    # Days -2 and -1 have no samples: zeros, not interpolated neighbours.
    assert [p.uptime for p in points[-4:]] == [1.0, 0.0, 0.0, 1.0]
    assert [p.samples for p in points[-4:]] == [100, 0, 0, 100]
    # Today is in progress; of the complete days, one is up and two are gaps.
    assert pct == pytest.approx(100 / 3, abs=0.001)


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
    buckets = {_epoch(1): (10, 0), _epoch(0): (10, 0)}
    points, pct = uptime_series(window_start(NOW), NOW, buckets)
    assert points[-1].uptime == 0.0 and points[-1].samples == 10
    assert pct == 0.0


def test_in_progress_day_is_not_counted_in_the_percentage():
    """A few ticks since midnight must not outvote the complete days."""
    buckets = {_epoch(d): (100, 100) for d in range(1, WINDOW_DAYS)}
    buckets[_epoch(0)] = (3, 0)  # three down ticks so far today
    points, pct = uptime_series(window_start(NOW), NOW, buckets)
    # The bar still renders today's partial ratio...
    assert points[-1].uptime == 0.0 and points[-1].samples == 3
    # ...but the percentage is the 89 complete days, all up.
    assert pct == 100.0


def test_only_in_progress_day_means_unknown_percentage():
    points, pct = uptime_series(window_start(NOW), NOW, {_epoch(0): (10, 10)})
    assert points[-1].uptime == 1.0 and points[-1].samples == 10
    assert pct is None


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
async def test_availability_buckets_collapses_per_instance_tick_and_returns_sparse_map():
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
    # Rows collapse per instance per tick (bool_or over sampled_at AND
    # instance_id) before bucketing, never joining gateway request logs: one of
    # two replicas down must stay a partial day instead of a full one.
    assert "bool_or" in sql
    assert (
        "GROUP BY instance_state_history.sampled_at, "
        "instance_state_history.instance_id" in sql
    )
    assert "alias IN ('core', 'wind')" in sql
    assert "gateway_requests" not in sql


HOUR = timedelta(hours=1)
ALL_TYPES = ["incident", "maintenance", "planned maintenance", "resolution"]


def _msg(type_: str, start: datetime, end: datetime | None) -> StatusMessageResponse:
    return StatusMessageResponse(
        id="00000000-0000-0000-0000-000000000001",
        body="body",
        type=type_,
        start_time=start,
        end_time=end,
    )


@pytest.mark.parametrize("type_", ALL_TYPES)
def test_message_visible_inside_window(type_):
    msg = _msg(type_, NOW - HOUR, NOW + HOUR)
    assert visible_messages([msg], NOW) == [msg]


@pytest.mark.parametrize("type_", ALL_TYPES)
def test_message_start_boundary_is_inclusive(type_):
    msg = _msg(type_, NOW, NOW + HOUR)
    assert visible_messages([msg], NOW) == [msg]


@pytest.mark.parametrize("type_", ALL_TYPES)
def test_message_end_boundary_is_exclusive(type_):
    msg = _msg(type_, NOW - HOUR, NOW)
    assert visible_messages([msg], NOW) == []


@pytest.mark.parametrize("type_", ALL_TYPES)
def test_message_past_end_is_filtered(type_):
    msg = _msg(type_, NOW - 2 * HOUR, NOW - HOUR)
    assert visible_messages([msg], NOW) == []


@pytest.mark.parametrize("type_", ALL_TYPES)
def test_open_ended_message_stays_visible(type_):
    msg = _msg(type_, NOW - HOUR, None)
    assert visible_messages([msg], NOW) == [msg]


@pytest.mark.parametrize("type_", ALL_TYPES)
def test_future_start_is_visible_only_for_planned_maintenance(type_):
    msg = _msg(type_, NOW + HOUR, NOW + 2 * HOUR)
    assert visible_messages([msg], NOW) == (
        [msg] if type_ == "planned maintenance" else []
    )


def test_open_ended_future_planned_maintenance_is_visible():
    msg = _msg("planned maintenance", NOW + HOUR, None)
    assert visible_messages([msg], NOW) == [msg]


def test_future_other_type_appears_exactly_at_start():
    msg = _msg("incident", NOW + HOUR, None)
    assert visible_messages([msg], NOW + HOUR - timedelta(seconds=1)) == []
    assert visible_messages([msg], NOW + HOUR) == [msg]


def test_visible_messages_keep_input_order():
    first = _msg("incident", NOW - 2 * HOUR, None)
    second = _msg("planned maintenance", NOW + HOUR, None)
    assert visible_messages([first, second], NOW) == [first, second]
