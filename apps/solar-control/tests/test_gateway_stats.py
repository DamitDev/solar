"""Tests for the gateway stats aggregation (read_stats).

read_stats computes everything in SQL; these unit tests stand in for the
database with fake sessions so the derived cache-aware fields are pinned
without needing a live Postgres.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.database.logs import GatewayLogger

START = datetime(2026, 8, 17, tzinfo=timezone.utc)
END = datetime(2026, 8, 18, tzinfo=timezone.utc)


class _Result:
    def __init__(self, value):
        self._value = value

    def one(self):
        return self._value

    def all(self):
        return self._value

    def scalar(self):
        return self._value


class _FakeSession:
    """Canned results per execute() call, popped in order."""

    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt):
        return _Result(self._results.pop(0))


def _patch_session(results):
    # get_session_factory() returns an async_sessionmaker, which is called to
    # produce the session -- mirror that with a factory of a factory.
    return patch(
        "app.database.logs.get_session_factory", lambda: lambda: _FakeSession(results)
    )


def _agg_row(**over):
    base = {
        "completed": 0,
        "missed": 0,
        "error": 0,
        "token_in_total": 0,
        "token_out_total": 0,
        "token_cached_total": 0,
        "token_in_measured_total": 0,
        "p_count": 0,
        "c_count": 0,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _model_row(model, completed=1, token_in=0, token_cached=0, token_out=0):
    return SimpleNamespace(
        model_key=model,
        completed=completed,
        token_in=token_in,
        token_cached=token_cached,
        token_out=token_out,
        avg_duration_s=1.0,
    )


def _host_row(host_id, completed=1, token_in=0, token_cached=0, token_out=0):
    return SimpleNamespace(
        host_id=host_id,
        host_name=f"Host {host_id}",
        completed=completed,
        token_in=token_in,
        token_cached=token_cached,
        token_out=token_out,
        avg_duration_s=1.0,
    )


@pytest.mark.anyio
async def test_cache_hit_rate_counts_only_cache_aware_rows():
    """A NULL-cached_tokens request still adds its prompt tokens to the input
    total but not to the measured denominator, so the rate is 300/500, not
    300/700."""
    logger = GatewayLogger()
    results = [
        _agg_row(
            completed=2,
            missed=0,
            error=1,  # the failed request contributes nothing
            token_in_total=700,
            token_out_total=300,
            token_cached_total=300,
            token_in_measured_total=500,
            p_count=2,
            c_count=2,
        ),
        [
            _model_row("m1", token_in=500, token_cached=300, token_out=200),
            _model_row("m2", token_in=200, token_cached=0, token_out=100),
        ],
        [_host_row("h1", token_in=700, token_cached=300, token_out=300)],
        0,  # rerouted requests
    ]

    with _patch_session(results):
        stats = await logger.read_stats(START, END)

    assert stats["token_in_total"] == 700
    assert stats["token_cached_total"] == 300
    assert stats["token_uncached_total"] == 400
    assert stats["cache_hit_rate"] == pytest.approx(0.6)
    assert {m["model"]: m["token_cached"] for m in stats["models"]} == {
        "m1": 300,
        "m2": 0,
    }
    assert stats["hosts"][0]["token_cached"] == 300


@pytest.mark.anyio
async def test_cache_hit_rate_is_zero_when_nothing_was_measured():
    """A fleet with no cache-aware rows reports cached 0 -- never a division
    error, never a phantom rate."""
    logger = GatewayLogger()
    results = [
        _agg_row(
            completed=1,
            missed=0,
            error=0,
            token_in_total=100,
            token_out_total=50,
            token_cached_total=0,
            token_in_measured_total=0,
            p_count=1,
            c_count=1,
        ),
        [_model_row("hf-model", token_in=100, token_cached=0, token_out=50)],
        [_host_row("h1", token_in=100, token_cached=0, token_out=50)],
        0,
    ]

    with _patch_session(results):
        stats = await logger.read_stats(START, END)

    assert stats["token_cached_total"] == 0
    assert stats["token_uncached_total"] == 100
    assert stats["cache_hit_rate"] == 0


@pytest.mark.anyio
async def test_everything_cached_reports_a_full_rate():
    logger = GatewayLogger()
    results = [
        _agg_row(
            completed=2,
            token_in_total=1000,
            token_out_total=100,
            token_cached_total=1000,
            token_in_measured_total=1000,
            p_count=2,
            c_count=2,
        ),
        [
            _model_row("m1", completed=1, token_in=400, token_cached=400, token_out=50),
            _model_row("m2", completed=1, token_in=600, token_cached=600, token_out=50),
        ],
        [_host_row("h1", completed=2, token_in=1000, token_cached=1000, token_out=100)],
        1,
    ]

    with _patch_session(results):
        stats = await logger.read_stats(START, END)

    assert stats["cache_hit_rate"] == pytest.approx(1.0)
    assert stats["rerouted_requests"] == 1
