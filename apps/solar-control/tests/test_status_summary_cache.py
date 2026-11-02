"""Tests for the full status summary response cache."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.status_summary import StatusSummaryService


class _Payload:
    """Stand-in for StatusSummary: build() re-stamps generated_at."""

    def __init__(self, value: str = "payload") -> None:
        self.value = value
        self.generated_at = "build-time"

    def model_copy(self, update: dict | None = None) -> "_Payload":
        copied = _Payload(self.value)
        for key, value in (update or {}).items():
            setattr(copied, key, value)
        return copied


@pytest.mark.anyio
async def test_build_reuses_the_response_within_ttl():
    svc = StatusSummaryService()
    build = AsyncMock(return_value=_Payload())
    with patch.object(svc, "_build", build):
        first = await svc.build()
        second = await svc.build()
        assert build.await_count == 1
    assert first.value == second.value == "payload"
    assert first.generated_at != "build-time"


@pytest.mark.anyio
async def test_build_rebuilds_after_the_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.services.status_summary.time.monotonic", lambda: clock[0])
    svc = StatusSummaryService()
    build = AsyncMock(return_value=_Payload())
    with patch.object(svc, "_build", build):
        await svc.build()
        clock[0] += 11
        await svc.build()
    assert build.await_count == 2


@pytest.mark.anyio
async def test_build_is_single_flight():
    svc = StatusSummaryService()
    gate = asyncio.Event()
    calls = 0

    async def slow() -> _Payload:
        nonlocal calls
        calls += 1
        await gate.wait()
        return _Payload()

    with patch.object(svc, "_build", side_effect=slow):
        tasks = [asyncio.create_task(svc.build()) for _ in range(5)]
        await asyncio.sleep(0.01)
        gate.set()
        results = await asyncio.gather(*tasks)

    assert calls == 1
    assert all(result.value == "payload" for result in results)


@pytest.mark.anyio
async def test_clear_cache_forces_a_rebuild():
    svc = StatusSummaryService()
    build = AsyncMock(return_value=_Payload())
    with patch.object(svc, "_build", build):
        await svc.build()
        svc.clear_cache()
        await svc.build()
    assert build.await_count == 2


@pytest.mark.anyio
async def test_a_build_that_raced_an_invalidation_is_not_stored():
    svc = StatusSummaryService()
    gate = asyncio.Event()
    first = True

    async def slow() -> _Payload:
        nonlocal first
        if first:
            first = False
            await gate.wait()
        return _Payload()

    with patch.object(svc, "_build", side_effect=slow):
        stale = asyncio.create_task(svc.build())
        await asyncio.sleep(0.01)
        svc.clear_cache()
        gate.set()
        await stale
    assert svc._response is None


@pytest.mark.anyio
async def test_cached_reuses_within_its_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.services.status_summary.time.monotonic", lambda: clock[0])
    svc = StatusSummaryService()
    loader = AsyncMock(return_value="value")
    assert await svc._cached("key", loader) == "value"
    clock[0] += 30
    assert await svc._cached("key", loader) == "value"
    assert loader.await_count == 1


@pytest.mark.anyio
async def test_cached_rebuilds_after_its_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.services.status_summary.time.monotonic", lambda: clock[0])
    svc = StatusSummaryService()
    loader = AsyncMock(return_value="value")
    assert await svc._cached("key", loader) == "value"
    clock[0] += 61
    assert await svc._cached("key", loader) == "value"
    assert loader.await_count == 2


@pytest.mark.anyio
async def test_cached_keys_own_their_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.services.status_summary.time.monotonic", lambda: clock[0])
    svc = StatusSummaryService()
    first = AsyncMock(return_value="a")
    second = AsyncMock(return_value="b")
    await svc._cached("a", first)
    clock[0] += 30
    await svc._cached("b", second)
    clock[0] += 31
    await svc._cached("a", first)
    assert first.await_count == 2
    assert second.await_count == 1
