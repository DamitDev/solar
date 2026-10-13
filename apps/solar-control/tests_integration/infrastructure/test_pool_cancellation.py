"""Red-test harness: how the solar-control DB pool leaks on client abort.

Starlette 1.3.1 BaseHTTPMiddleware (starlette/middleware/base.py) wraps
``call_next`` in a task group; a client disconnect triggers
``task_group.cancel_scope.cancel()`` and every subsequent await re-raises
cancellation. SQLAlchemy 2.0.54 ``AsyncSession.__aexit__`` closes sessions
in a shielded task — plain session close is shield-protected; the leak
comes from a cancel landing between checkout and close, or during checkout.

Self-contained: a minimal FastAPI app in-process against an ephemeral
Postgres container (pool_size=2, max_overflow=0, pool_timeout=2), the REAL
``auth_middleware`` from ``app.auth`` registered via
``app.middleware("http")``, plus a route running ``SELECT pg_sleep(5)``
through a ``_session()``-style helper. The abort is a REAL client abort:
the ASGI task is cancelled — exactly the cancellation a real ASGI server
delivers when the client drops the TCP connection mid-request.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.auth import auth_middleware
from app.database.connection import close_db, get_session_factory, init_db

pytestmark = pytest.mark.infrastructure

POOL_SIZE = 2
POOL_TIMEOUT = 2
# The route sleeps 5s server-side; the abort lands well inside the window.
QUERY_S = 5.0
ABORT_AFTER_S = 0.5


def _make_app() -> FastAPI:
    """Minimal app: real auth middleware + one _session()-style route."""
    app = FastAPI()
    app.middleware("http")(auth_middleware)

    @app.get("/health")
    async def slow_query():
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT pg_sleep(5)"))
        return {"ok": True}

    @app.get("/ready")
    async def fast_query():
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {"ok": True}

    return app


async def _drive(app: FastAPI, path: str, abort_after: float | None = None):
    """Drive the ASGI app in-process; return the response status code.

    ``abort_after`` cancels the ASGI task mid-request — the same
    cancellation a real ASGI server delivers when the client drops the
    connection; the cancel is what reaches the handler's DB
    await. A bodyless GET never reads ``receive``, so feeding the
    disconnect message would be a no-op; the task cancel is the real abort.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 54321),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    status_code: int | None = None

    async def send(message):
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]

    task = asyncio.create_task(app(scope, receive, send))
    if abort_after is not None:
        await asyncio.sleep(abort_after)
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if abort_after is None:
            raise
        return None
    except RuntimeError:
        # BaseHTTPMiddleware surfaces a mid-request abort as
        # "No response returned." when the app task dies first.
        if abort_after is None:
            raise
        return None
    return status_code


async def _settle_and_assert_pool_empty(engine, recwarn) -> None:
    """Let the shielded close + pool return finish, then assert no leak."""
    for _ in range(30):
        await asyncio.sleep(0.1)
        if engine.sync_engine.pool.checkedout() == 0:
            break
    gc.collect()
    checked_out = engine.sync_engine.pool.checkedout()
    assert checked_out == 0, (
        f"pool leak: {checked_out} connection(s) still checked out after "
        "client abort settled"
    )
    leaked = [
        str(w.message) for w in recwarn.list if "non-checked-in" in str(w.message)
    ]
    assert not leaked, f"SQLAlchemy warned about the abort: {leaked}"


def _pg_url(postgres_container) -> str:
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    return f"postgresql://test:test@{host}:{port}/test"


async def test_abort_during_query_no_pool_leak(postgres_container, recwarn):
    """Scenario (a): abort while the query is in-flight inside the handler.

    The cancel lands while the session holds a connection mid-``SELECT
    pg_sleep(5)``. After the abort settles, the pool must be fully
    returned and a follow-up request must succeed within pool_timeout.
    """
    url = _pg_url(postgres_container)
    app = _make_app()
    engine = await init_db(
        url, pool_size=POOL_SIZE, max_overflow=0, pool_timeout=POOL_TIMEOUT
    )
    try:
        # Sanity: the route machinery works before the abort.
        status = await _drive(app, "/ready")
        assert status == 200, "sanity: fast query failed before the abort"

        # The abort: cancel mid-query.
        await _drive(app, "/health", abort_after=ABORT_AFTER_S)
        await _settle_and_assert_pool_empty(engine, recwarn)

        # Follow-up: the pool must serve a new request within pool_timeout.
        status = await _drive(app, "/ready")
        assert status == 200, "follow-up request failed after the abort"
    finally:
        await close_db()


async def test_abort_during_checkout_no_pool_leak(postgres_container, recwarn):
    """Scenario (b): pool filled from outside the app, then abort.

    Both pool slots are held by raw ``engine.connect()`` calls, so the
    request's session checkout blocks. The abort lands during that
    checkout. After the abort settles and the artificial fill is
    released, the pool must be fully returned and a follow-up request
    must succeed within pool_timeout.
    """
    url = _pg_url(postgres_container)
    app = _make_app()
    engine = await init_db(
        url, pool_size=POOL_SIZE, max_overflow=0, pool_timeout=POOL_TIMEOUT
    )
    try:
        # Sanity: the route machinery works before the abort.
        status = await _drive(app, "/ready")
        assert status == 200, "sanity: fast query failed before the abort"

        # Fill the pool from outside the app so the request's checkout blocks.
        conns = [await engine.connect() for _ in range(POOL_SIZE)]
        try:
            # The abort: cancel while the checkout is still waiting
            # (the fill holds both slots for far longer than the abort).
            await _drive(app, "/health", abort_after=ABORT_AFTER_S)

            # Release the artificial fill so checkedout measures the
            # aborted request's connection, not the fill.
            for conn in conns:
                await conn.close()
            conns.clear()

            await _settle_and_assert_pool_empty(engine, recwarn)

            status = await _drive(app, "/ready")
            assert status == 200, "follow-up request failed after the abort"
        finally:
            for conn in conns:
                with contextlib.suppress(Exception):
                    await conn.close()
    finally:
        await close_db()
