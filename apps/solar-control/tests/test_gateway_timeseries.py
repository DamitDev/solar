"""Tests for the bucketed gateway time series (GET /api/gateway/timeseries)."""

from datetime import datetime, timedelta, timezone
from itertools import pairwise
from unittest.mock import AsyncMock, patch

import pytest

from app.database.logs import (
    TIMESERIES_BUCKETS,
    TIMESERIES_MAX_POINTS,
    GatewayLogger,
    fill_buckets,
    resolve_bucket,
)
from app.routes.management.gateway import MAX_TIMESERIES_SERIES, get_timeseries

START = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def span(**kwargs) -> tuple[datetime, datetime]:
    return START, START + timedelta(**kwargs)


class TestResolveBucket:
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"hours": 1}, "1m"),
            ({"hours": 12}, "5m"),
            ({"days": 1}, "15m"),
            ({"days": 7}, "1h"),
            ({"days": 30}, "6h"),
            ({"days": 365}, "7d"),
        ],
    )
    def test_auto_picks_a_readable_resolution(self, kwargs, expected):
        start, end = span(**kwargs)
        assert resolve_bucket(start, end, "auto") == expected

    @pytest.mark.parametrize("bucket", sorted(TIMESERIES_BUCKETS))
    def test_auto_never_exceeds_the_point_budget(self, bucket):
        start, end = span(days=400)
        chosen = resolve_bucket(start, end, "auto")
        assert (end - start).total_seconds() / TIMESERIES_BUCKETS[chosen] <= (
            TIMESERIES_MAX_POINTS
        )

    def test_explicit_bucket_wins_over_auto_selection(self):
        start, end = span(days=365)
        assert resolve_bucket(start, end, "1h") == "1h"

    def test_unknown_bucket_falls_back_to_auto(self):
        start, end = span(hours=1)
        assert resolve_bucket(start, end, "3s") == "1m"

    def test_inverted_range_does_not_crash(self):
        assert resolve_bucket(START, START - timedelta(days=1), "auto") == "1m"


class TestFillBuckets:
    def test_gaps_become_zeros_rather_than_missing_samples(self):
        start, end = span(minutes=5)
        aggregates = {
            int(start.timestamp()): {
                "success": 3,
                "error": 1,
                "missed": 0,
                "token_in": 100,
                "token_cached": 40,
                "token_out": 50,
                "avg_duration_s": 1.5,
            }
        }

        points = fill_buckets(start, end, 60, aggregates)

        assert len(points) == 6
        assert points[0]["success"] == 3
        assert points[0]["avg_duration_s"] == 1.5
        assert [p["success"] for p in points[1:]] == [0, 0, 0, 0, 0]
        # No traffic means no latency to report -- 0.0 would read as "instant".
        assert all(p["avg_duration_s"] is None for p in points[1:])
        # The cached split rides the same zero-fill as every other bucket field.
        assert points[0]["token_cached"] == 40
        assert [p["token_cached"] for p in points[1:]] == [0, 0, 0, 0, 0]

    def test_series_is_contiguous_and_bucket_aligned(self):
        start = datetime(2026, 8, 13, 12, 0, 37, tzinfo=timezone.utc)
        points = fill_buckets(start, start + timedelta(minutes=3), 60, {})

        stamps = [datetime.fromisoformat(p["ts"]) for p in points]
        assert stamps[0] == datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        assert all((b - a).total_seconds() == 60 for a, b in pairwise(stamps))

    def test_empty_range_yields_a_single_bucket(self):
        assert len(fill_buckets(START, START, 60, {})) == 1


class Row:
    """Stand-in for a SQLAlchemy result row from the timeseries query."""

    def __init__(
        self,
        ts,
        *,
        success=0,
        error=0,
        missed=0,
        tin=0,
        tout=0,
        dur=0.0,
        key=None,
        tc=0,
    ):
        self.ts = ts
        self.success = success
        self.error = error
        self.missed = missed
        self.token_in = tin
        self.token_out = tout
        self.token_cached = tc
        self.duration_s = dur
        self.group_key = key


class TestShapeTimeseries:
    shape = staticmethod(GatewayLogger._shape_timeseries)

    def test_groups_are_summed_into_the_combined_series(self):
        rows = [
            Row(START, success=2, tin=10, tout=5, dur=4.0, key="ep-a"),
            Row(START, success=3, error=1, tin=20, tout=7, dur=6.0, key="ep-b"),
        ]

        points, series = self.shape(rows, START, START, 60, grouped=True)

        assert points[0]["success"] == 5
        assert points[0]["error"] == 1
        assert points[0]["token_in"] == 30
        assert {s["key"] for s in series} == {"ep-a", "ep-b"}

    def test_token_cached_appears_in_points_and_grouped_series(self):
        # The tc=0 row stands in for a NULL-cached_tokens (non-cache-aware)
        # request: it must not break the sum, it simply contributes nothing.
        rows = [
            Row(START, success=3, tin=300, tc=200, tout=50, dur=6.0, key="ep-a"),
            Row(START, success=1, tin=100, tc=0, tout=10, dur=0.5, key="ep-b"),
        ]

        points, series = self.shape(rows, START, START, 60, grouped=True)

        assert points[0]["token_cached"] == 200
        assert points[0]["token_in"] == 400
        assert {s["key"]: s["points"][0]["token_cached"] for s in series} == {
            "ep-a": 200,
            "ep-b": 0,
        }

    def test_combined_latency_is_weighted_by_volume(self):
        # A mean of per-group means would over-weight the quiet group.
        rows = [
            Row(START, success=1, dur=10.0, key="quiet"),
            Row(START, success=9, dur=9.0, key="busy"),
        ]

        points, _ = self.shape(rows, START, START, 60, grouped=True)

        assert points[0]["avg_duration_s"] == pytest.approx(1.9)

    def test_series_are_ordered_by_volume(self):
        rows = [
            Row(START, success=1, key="quiet"),
            Row(START, success=50, key="busy"),
            Row(START, success=10, key="middling"),
        ]

        _, series = self.shape(rows, START, START, 60, grouped=True)

        assert [s["key"] for s in series] == ["busy", "middling", "quiet"]
        assert [s["total"] for s in series] == [50, 10, 1]

    def test_every_group_shares_the_same_bucket_grid(self):
        # Stacking only lines up if each series spans the whole range.
        rows = [
            Row(START, success=1, key="ep-a"),
            Row(START + timedelta(minutes=3), success=1, key="ep-b"),
        ]

        points, series = self.shape(
            rows, START, START + timedelta(minutes=3), 60, grouped=True
        )

        assert all(len(s["points"]) == len(points) for s in series)
        assert all(
            [p["ts"] for p in s["points"]] == [p["ts"] for p in points] for s in series
        )

    def test_ungrouped_query_returns_no_series(self):
        points, series = self.shape(
            [Row(START, success=1)], START, START, 60, grouped=False
        )

        assert points[0]["success"] == 1
        assert series == []

    def test_missing_group_key_is_labelled_rather_than_dropped(self):
        _, series = self.shape(
            [Row(START, success=1, key=None)], START, START, 60, grouped=True
        )

        assert [s["key"] for s in series] == ["unknown"]


class TestTimeseriesRoute:
    @pytest.mark.asyncio
    async def test_forwards_filters_and_echoes_the_resolved_bucket(self):
        read = AsyncMock(return_value=("15m", [{"ts": START.isoformat()}], []))

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            result = await get_timeseries(
                from_ts=START.isoformat(),
                to_ts=(START + timedelta(days=1)).isoformat(),
                bucket="auto",
                group_by="endpoint",
                request_type="chat",
                model="qwen3.5:4b",
                host_id="host-1",
                endpoint_id="ep-1",
            )

        assert result["bucket"] == "15m"
        assert result["group_by"] == "endpoint"
        assert result["points"] == [{"ts": START.isoformat()}]

        kwargs = read.await_args.kwargs
        assert kwargs["bucket"] == "auto"
        assert kwargs["group_by"] == "endpoint"
        assert kwargs["request_type"] == "chat"
        assert kwargs["model"] == "qwen3.5:4b"
        assert kwargs["host_id"] == "host-1"
        assert kwargs["endpoint_id"] == "ep-1"

    @pytest.mark.asyncio
    async def test_request_type_all_is_treated_as_no_filter(self):
        read = AsyncMock(return_value=("1h", [], []))

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            await get_timeseries(request_type="all")

        assert read.await_args.kwargs["request_type"] is None

    @pytest.mark.asyncio
    async def test_defaults_to_the_last_day(self):
        read = AsyncMock(return_value=("15m", [], []))

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            result = await get_timeseries()

        start = datetime.fromisoformat(result["from"])
        end = datetime.fromisoformat(result["to"])
        assert (
            timedelta(hours=23, minutes=59) < end - start < timedelta(days=1, hours=1)
        )

    @pytest.mark.asyncio
    async def test_long_tail_of_series_is_capped_and_flagged(self):
        many = [{"key": f"ep-{i}", "total": i, "points": []} for i in range(20)]
        read = AsyncMock(return_value=("1h", [], many))

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            result = await get_timeseries(group_by="endpoint")

        assert len(result["series"]) == MAX_TIMESERIES_SERIES
        assert result["series_truncated"] is True

    @pytest.mark.asyncio
    async def test_short_series_list_is_not_flagged_as_truncated(self):
        read = AsyncMock(
            return_value=("1h", [], [{"key": "ep-1", "total": 1, "points": []}])
        )

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            result = await get_timeseries(group_by="endpoint")

        assert result["series_truncated"] is False
