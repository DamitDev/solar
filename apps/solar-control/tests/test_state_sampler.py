"""Tests for the instance-state sampler."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models.gateway import RegistryEntry
from app.services.state_sampler import StateSampler


def _entry(host_id: str, instance_id: str, alias: str) -> RegistryEntry:
    return RegistryEntry(
        host_id=host_id,
        instance_id=instance_id,
        url=f"http://{host_id}",
        api_key="k",
        model_alias=alias,
    )


def _snapshot(age_s: float) -> dict:
    at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return {"at": at.isoformat(), "resources": {}}


class _Harness:
    """Patches the sampler's registry, snapshot and history dependencies."""

    def __init__(self, registry, snapshots):
        self.registry = registry
        self.snapshots = snapshots
        self.inserted: list = []

    async def _insert(self, samples):
        self.inserted.extend(samples)
        return len(samples)

    async def _snapshot(self, host_id):
        value = self.snapshots.get(host_id)
        if isinstance(value, Exception):
            raise value
        return value

    def __enter__(self):
        self._patches = [
            patch(
                "app.services.state_sampler.registry_store.get_registry",
                AsyncMock(return_value=self.registry),
            ),
            patch(
                "app.services.state_sampler.host_store.get_host_resource_snapshot",
                self._snapshot,
            ),
            patch(
                "app.services.state_sampler.state_history_db.insert_batch",
                self._insert,
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


@pytest.mark.asyncio
async def test_one_row_per_instance_per_tick():
    registry = {
        "core": [_entry("h1", "i1", "core"), _entry("h2", "i2", "core")],
        "wind": [_entry("h1", "i3", "wind")],
    }
    snapshots = {"h1": _snapshot(1), "h2": _snapshot(1)}
    with _Harness(registry, snapshots) as h:
        written = await StateSampler().sample_once()

    assert written == 3
    assert {(s.alias, s.instance_id, s.host_id) for s in h.inserted} == {
        ("core", "i1", "h1"),
        ("core", "i2", "h2"),
        ("wind", "i3", "h1"),
    }
    # One tick shares a single timestamp across all rows.
    assert len({s.sampled_at for s in h.inserted}) == 1
    assert h.inserted[0].sampled_at.tzinfo is not None


@pytest.mark.asyncio
async def test_fresh_snapshot_is_running():
    with _Harness({"core": [_entry("h1", "i1", "core")]}, {"h1": _snapshot(1)}) as h:
        await StateSampler().sample_once()
    assert [s.state for s in h.inserted] == ["running"]


@pytest.mark.asyncio
async def test_stale_snapshot_is_offline():
    stale = _snapshot(settings.host_snapshot_max_age_s + 10)
    with _Harness({"core": [_entry("h1", "i1", "core")]}, {"h1": stale}) as h:
        await StateSampler().sample_once()
    assert [s.state for s in h.inserted] == ["offline"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [None, {}, {"at": "not-a-date"}, {"at": 5}, "garbage", RuntimeError("redis down")],
)
async def test_missing_or_unusable_snapshot_is_offline(snapshot):
    with _Harness({"core": [_entry("h1", "i1", "core")]}, {"h1": snapshot}) as h:
        written = await StateSampler().sample_once()
    assert written == 1
    assert [s.state for s in h.inserted] == ["offline"]


@pytest.mark.asyncio
async def test_empty_registry_writes_nothing():
    with _Harness({}, {}) as h:
        written = await StateSampler().sample_once()
    assert written == 0
    assert h.inserted == []


@pytest.mark.asyncio
async def test_snapshot_read_once_per_host():
    registry = {"core": [_entry("h1", "i1", "core"), _entry("h1", "i2", "core")]}
    reads: list[str] = []
    fresh = _snapshot(1)

    async def _counting_read(host_id):
        reads.append(host_id)
        return fresh

    with (
        _Harness(registry, {}) as h,
        patch(
            "app.services.state_sampler.host_store.get_host_resource_snapshot",
            _counting_read,
        ),
    ):
        await StateSampler().sample_once()
    assert reads == ["h1"]
    assert [s.state for s in h.inserted] == ["running", "running"]
    assert len(h.inserted) == 2


@pytest.mark.asyncio
async def test_tick_skipped_when_another_replica_holds_the_lock():
    redis = AsyncMock()
    redis.set.return_value = None  # NX lost
    sampler = StateSampler()
    with (
        patch("app.services.state_sampler.redis_client", return_value=redis),
        patch.object(sampler, "sample_once", AsyncMock(return_value=1)) as sample,
    ):
        assert await sampler._tick() == 0
    sample.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_samples_when_lock_acquired():
    redis = AsyncMock()
    redis.set.return_value = True
    sampler = StateSampler()
    with (
        patch("app.services.state_sampler.redis_client", return_value=redis),
        patch.object(sampler, "sample_once", AsyncMock(return_value=4)),
    ):
        assert await sampler._tick() == 4
    assert redis.set.await_args.kwargs["nx"] is True


@pytest.mark.asyncio
async def test_loop_survives_failing_tick_and_stops_cleanly():
    sampler = StateSampler()
    calls = 0
    two_calls = asyncio.Event()

    async def _tick():
        nonlocal calls
        calls += 1
        if calls >= 2:
            two_calls.set()
        raise RuntimeError("boom")

    with (
        patch.object(sampler, "_tick", _tick),
        patch.object(settings, "state_sample_interval_s", 0.01),
    ):
        await sampler.start()
        await asyncio.wait_for(two_calls.wait(), timeout=2)
        await sampler.stop()

    assert calls >= 2
    assert sampler._task is None
    assert not sampler._running
