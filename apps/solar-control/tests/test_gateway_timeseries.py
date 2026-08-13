"""Tests for the bucketed gateway time series (GET /api/gateway/timeseries)."""

from datetime import datetime, timedelta, timezone
from itertools import pairwise
from unittest.mock import AsyncMock, patch

import pytest

from app.database.logs import (
    TIMESERIES_BUCKETS,
    TIMESERIES_MAX_POINTS,
    fill_buckets,
    resolve_bucket,
)
from app.routes.management.gateway import get_timeseries

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

    def test_series_is_contiguous_and_bucket_aligned(self):
        start = datetime(2026, 8, 13, 12, 0, 37, tzinfo=timezone.utc)
        points = fill_buckets(start, start + timedelta(minutes=3), 60, {})

        stamps = [datetime.fromisoformat(p["ts"]) for p in points]
        assert stamps[0] == datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        assert all((b - a).total_seconds() == 60 for a, b in pairwise(stamps))

    def test_empty_range_yields_a_single_bucket(self):
        assert len(fill_buckets(START, START, 60, {})) == 1


class TestTimeseriesRoute:
    @pytest.mark.asyncio
    async def test_forwards_filters_and_echoes_the_resolved_bucket(self):
        read = AsyncMock(return_value=("15m", [{"ts": START.isoformat()}]))

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            result = await get_timeseries(
                from_ts=START.isoformat(),
                to_ts=(START + timedelta(days=1)).isoformat(),
                bucket="auto",
                request_type="chat",
                model="qwen3.5:4b",
                host_id="host-1",
                endpoint_id="ep-1",
            )

        assert result["bucket"] == "15m"
        assert result["points"] == [{"ts": START.isoformat()}]

        kwargs = read.await_args.kwargs
        assert kwargs["bucket"] == "auto"
        assert kwargs["request_type"] == "chat"
        assert kwargs["model"] == "qwen3.5:4b"
        assert kwargs["host_id"] == "host-1"
        assert kwargs["endpoint_id"] == "ep-1"

    @pytest.mark.asyncio
    async def test_request_type_all_is_treated_as_no_filter(self):
        read = AsyncMock(return_value=("1h", []))

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            await get_timeseries(request_type="all")

        assert read.await_args.kwargs["request_type"] is None

    @pytest.mark.asyncio
    async def test_defaults_to_the_last_day(self):
        read = AsyncMock(return_value=("15m", []))

        with patch("app.routes.management.gateway.gateway_logger") as logger:
            logger.read_timeseries = read
            result = await get_timeseries()

        start = datetime.fromisoformat(result["from"])
        end = datetime.fromisoformat(result["to"])
        assert (
            timedelta(hours=23, minutes=59) < end - start < timedelta(days=1, hours=1)
        )
