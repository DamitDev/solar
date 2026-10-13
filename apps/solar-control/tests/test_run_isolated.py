"""Unit tests for run_isolated error retrieval and close_db drain (#99 follow-ups).

No database needed: the isolated op is a plain coroutine.
"""

import asyncio
import gc
import time

import pytest

from app.database import connection

pytestmark = pytest.mark.anyio


async def test_cancelled_caller_does_not_leak_task_exception():
    recorded = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda l, context: recorded.append(context))

    started = asyncio.Event()

    async def _op():
        started.set()
        await asyncio.sleep(0.2)
        raise RuntimeError("isolated op failure")

    async def _caller():
        await connection.run_isolated(_op())

    caller_task = asyncio.create_task(_caller())
    await started.wait()

    # The op's task is the only entry in _isolated_tasks; grab it before
    # cancelling the caller.
    snapshot = set(connection._isolated_tasks)
    assert len(snapshot) == 1
    caller_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller_task

    # Let the isolated op finish in the background. Drop the local task
    # references first: an unretrieved exception is only reported when the
    # task object is garbage-collected.
    (task,) = snapshot
    await asyncio.wait({task}, timeout=1.0)
    del task, snapshot

    gc.collect()
    assert not recorded  # no "Task exception was never retrieved"
    assert not connection._isolated_tasks


async def test_exception_propagates_to_caller_on_normal_path():
    async def _op():
        await asyncio.sleep(0.05)
        raise RuntimeError("isolated boom")

    with pytest.raises(RuntimeError, match="isolated boom"):
        await connection.run_isolated(_op())
    assert not connection._isolated_tasks


async def test_return_value_passes_through():
    async def _op():
        await asyncio.sleep(0.05)
        return 42

    result = await connection.run_isolated(_op())
    assert result == 42
    assert not connection._isolated_tasks


async def test_close_db_drains_inflight_tasks(monkeypatch):
    started = asyncio.Event()

    async def _op():
        started.set()
        await asyncio.sleep(0.2)

    asyncio.create_task(connection.run_isolated(_op()))
    await started.wait()
    (task,) = connection._isolated_tasks

    # Drain-only path: no engine, so dispose is skipped.
    monkeypatch.setattr(connection, "_engine", None)
    await connection.close_db()

    assert task.done()
    assert not connection._isolated_tasks


async def test_close_db_drain_timeout_returns_promptly(monkeypatch):
    started = asyncio.Event()

    async def _op():
        started.set()
        await asyncio.sleep(0.5)

    asyncio.create_task(connection.run_isolated(_op()))
    await started.wait()
    (task,) = connection._isolated_tasks

    monkeypatch.setattr(connection, "_engine", None)
    start = time.monotonic()
    await connection.close_db(drain_timeout=0.1)
    elapsed = time.monotonic() - start

    # close_db returns promptly; the straggler keeps running uncancelled.
    assert elapsed < 1.0

    # Let the straggler finish so later tests see a clean task set.
    await asyncio.wait({task}, timeout=1.0)
    assert not connection._isolated_tasks
