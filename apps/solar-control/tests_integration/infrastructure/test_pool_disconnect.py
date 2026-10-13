"""Realistic pool-leak reproduction harness for solar-control #99.

Issue #99: prod pods log ``Exception terminating connection`` plus
SAWarning ``garbage collector is trying to clean up non-checked-in
connection``; the pool drains over time. Leading hypothesis: level-
triggered anyio cancel scopes (BaseHTTPMiddleware + StreamingResponse)
deliver a second cancel while SQLAlchemy invalidates a connection, so the
record is never checked in.

Harness: real uvicorn serving (in-process, lifespan off) a minimal FastAPI
app with the REAL ``auth_middleware`` registered via
``app.middleware("http")``, plus /v1/* routes that run slow queries and
streaming responses through a ``_session()``-style helper. The abort is a
REAL client abort: a raw-socket close (``asyncio.open_connection`` +
``writer.close()``) mid-request — exactly what a real ASGI server delivers
when the client drops the TCP connection. NOT ``task.cancel()``.

Ten scenarios plus the idle-in-transaction check. The RED tests reproduce
the leak; the GREEN tests are control scenarios (no leak expected). Do not
fix app code — report the red/green matrix honestly.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import socket
from typing import Any

import pytest
from sqlalchemy import text

from app.auth import auth_middleware
from app.config import settings
from app.database.connection import close_db, get_session_factory, init_db

pytestmark = pytest.mark.infrastructure

# Small pool so the leak reproduces quickly and the pool_timeout window is
# short enough to hit under a blocked checkout.
POOL_SIZE = 2
MAX_OVERFLOW = 0
POOL_TIMEOUT = 2

# The abort lands well inside the server-side window (queries sleep >= 2s).
ABORT_AFTER_S = 2.0
# Generous settle: let the cancel propagate, the session cleanup run (or
# fail), and the GC fire before asserting the pool is clean.
SETTLE_S = 5.0
# Burst test: short settle between iterations keeps the runtime bounded
# (~1.5s per iteration) while giving each abort a window to settle.
BURST_ITERATIONS = 30
BURST_SETTLE_S = 0.5

# Log signatures from issue #99.
_TERMINATE_SIGNATURE = "Exception terminating connection"
_GC_SIGNATURE = "non-checked-in connection"

TEST_KEY = "sk-test-pool-leak-99"
TEST_ENDPOINT = "pool-leak-test-endpoint"


class _LogCapture(logging.Handler):
    """Capture log records so pool-level errors can be asserted."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def matching(self, needle: str) -> list[str]:
        return [m for m in self.messages if needle in m]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_app(include_management: bool = False):
    """Minimal app: real auth middleware + DB-backed /v1 routes.

    With ``include_management=True`` the REAL management router is mounted
    exactly as app/main.py mounts it (``APIRouter(prefix="/api")`` behind
    ``auth_middleware``), so the Webui read path exercises the production
    route + key handling.
    """
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    app = FastAPI()
    app.middleware("http")(auth_middleware)

    if include_management:
        from app.routes.management import router as management_router

        app.include_router(management_router)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/v1/models")
    async def slow_models():
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT pg_sleep(5)"))
        return {"object": "list", "models": [], "data": []}

    @app.get("/v1/stream")
    async def stream():
        async def generator():
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
                for i in range(10):
                    yield f"data: chunk {i}\n\n".encode()
                    await asyncio.sleep(1.0)

        return StreamingResponse(generator(), media_type="text/event-stream")

    return app


async def _start_server(db_url: str, asgi_app=None):
    """Start a real uvicorn server (in-process, lifespan off).

    init_db runs first (production pool settings overridden with the small
    test pool) so the app's own db modules can seed, then uvicorn serves
    the app on a random loopback port.
    """
    import uvicorn

    engine = await init_db(
        db_url,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT,
    )
    await _insert_endpoint_and_key(db_url)
    port = _free_port()
    config = uvicorn.Config(
        app=asgi_app or _make_app(),
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(100):
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.1)
    return server, serve_task, port, engine


async def _stop_server(server, serve_task) -> None:
    """Stop the uvicorn server and dispose the DB engine (bounded)."""
    server.should_exit = True
    with contextlib.suppress(asyncio.TimeoutError, Exception):
        await asyncio.wait_for(serve_task, timeout=10.0)
    with contextlib.suppress(asyncio.TimeoutError, Exception):
        await asyncio.wait_for(close_db(), timeout=10.0)


async def _raw_request(
    port: int,
    path: str,
    api_key: str | None = None,
    *,
    abort_after: float | None = None,
    read_first: bool = False,
) -> bytes | None:
    """Send an HTTP GET over a raw socket; optionally abort mid-request.

    The abort is a REAL client abort: ``writer.close()`` on the raw socket
    delivers a TCP FIN to the server — exactly what a real ASGI server
    sees when the client drops the connection. NOT ``task.cancel()``.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
    if api_key:
        request += f"X-API-Key: {api_key}\r\n"
    request += "\r\n"
    writer.write(request.encode())
    await writer.drain()
    try:
        if abort_after is not None:
            if read_first:
                # Read the first response bytes (proves the stream started).
                await asyncio.wait_for(reader.read(1024), timeout=10.0)
            await asyncio.sleep(abort_after)
            writer.close()
            await writer.wait_closed()
            return None
        data = await asyncio.wait_for(reader.read(65536), timeout=30.0)
        writer.close()
        await writer.wait_closed()
        return data
    except (ConnectionError, RuntimeError, TimeoutError):
        return None


async def _insert_endpoint_and_key(db_url: str) -> str:
    """Insert a real endpoint + API key using the app's own db modules.

    Idempotent: tests within one session share the migrated solar_gateway
    DB, and the endpoint name is unique.
    """
    from app.database.api_keys import api_key_db
    from app.database.endpoints import endpoint_db

    existing = await endpoint_db.get_all_endpoints()
    for ep in existing:
        if ep.name == TEST_ENDPOINT:
            return ep.id
    ep = await endpoint_db.create_endpoint(TEST_ENDPOINT)
    await api_key_db.create(ep.id, "test-key", key=TEST_KEY)
    return ep.id


async def _lock_table(db_url: str, table: str):
    """Acquire a table lock from a raw connection to block app queries.

    ``LOCK TABLE ... IN EXCLUSIVE MODE`` conflicts with ACCESS SHARE, so
    even a plain SELECT blocks until the lock is released. asyncpg runs
    autocommit by default, so the LOCK needs an explicitly started
    transaction that stays open until rollback.
    """
    import asyncpg

    conn = await asyncpg.connect(db_url)
    txn = conn.transaction()
    await txn.start()
    await conn.execute(f"LOCK TABLE {table} IN EXCLUSIVE MODE")
    return conn, txn


async def _idle_in_transaction_count(db_url: str) -> int:
    """Count 'idle in transaction' sessions from pg_stat_activity.

    A leaked connection that was mid-transaction shows up here: the pool
    lost track of it, the transaction was never committed or rolled back,
    and Postgres sees it as 'idle in transaction'.
    """
    import asyncpg

    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow(
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE state = 'idle in transaction' AND pid != pg_backend_pid()"
        )
        return row["n"]
    finally:
        await conn.close()


async def _settle_and_check(
    engine, log_capture: _LogCapture, db_url: str, context: str
) -> None:
    """Let the abort settle, then assert pool + logs + idle-in-transaction."""
    for _ in range(int(SETTLE_S * 10)):
        await asyncio.sleep(0.1)
        if engine.sync_engine.pool.checkedout() == 0:
            break
    gc.collect()
    await asyncio.sleep(0.5)

    checked_out = engine.sync_engine.pool.checkedout()
    gc_hits = log_capture.matching(_GC_SIGNATURE)
    terminate_hits = log_capture.matching(_TERMINATE_SIGNATURE)
    idle_in_txn = await _idle_in_transaction_count(db_url)

    # The idle-in-transaction check: after the abort settles, no connection
    # may be left idle in transaction.
    assert idle_in_txn == 0, (
        f"[{context}] idle-in-transaction leak: {idle_in_txn} session(s) "
        "left 'idle in transaction' after abort settled"
    )
    assert checked_out == 0, (
        f"[{context}] pool leak: {checked_out} connection(s) still "
        "checked out after abort settled"
    )
    assert not gc_hits, f"[{context}] SQLAlchemy GC warning: {gc_hits}"
    assert not terminate_hits, f"[{context}] terminate warning: {terminate_hits}"


async def _terminate_idle_backends(db_url: str) -> int:
    """Terminate the app's idle backends via a raw asyncpg side connection.

    After a completed request the app's pooled connections sit idle in
    Postgres (``state = 'idle'``). Terminating those backends makes the
    pooled connections stale, so the next checkout's ``pool_pre_ping`` ping
    fails and the pool must reconnect. The test DB is dedicated to the app
    under test, so every other backend besides this side connection is an
    app backend. Same out-of-band pattern as ``_lock_table``.
    """
    import asyncpg

    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            "SELECT pid FROM pg_stat_activity "
            "WHERE state = 'idle' AND pid != pg_backend_pid()"
        )
        for row in rows:
            await conn.execute("SELECT pg_terminate_backend($1)", row["pid"])
        return len(rows)
    finally:
        await conn.close()


def _make_pure_asgi_app():
    """Pure-ASGI control app: no BaseHTTPMiddleware, no streaming.

    The middleware is a plain class with ``async def __call__(self, scope,
    receive, send)`` — no anyio cancel scope wraps the DB call. The route
    resolves the API key inline through the app's own db modules and
    returns a plain JSON response. Same abort-during-query scenario as the
    RED slow-query case, but without the level-triggered machinery.
    """
    from fastapi.responses import JSONResponse

    from app.database.connection import get_session_factory
    from app.database.endpoints import endpoint_db

    async def _send_json(scope, receive, send, payload, status_code=200):  # type: ignore[no-untyped-def]
        response = JSONResponse(payload, status_code=status_code)
        await response(scope, receive, send)

    async def _app(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            return
        headers = dict(scope.get("headers") or [])
        raw_key = headers.get(b"x-api-key")
        api_key = raw_key.decode("latin-1") if raw_key else None
        resolved = await endpoint_db.resolve_by_api_key(api_key) if api_key else None
        if not resolved:
            await _send_json(
                scope, receive, send, {"error": {"message": "unauthorized"}}, 401
            )
            return
        if scope["path"] != "/v1/models":
            await _send_json(scope, receive, send, {"detail": "not found"}, 404)
            return
        # The DB lookup runs inline on the request task with NO cancel
        # scope around it and NO StreamingResponse: the plain-response
        # control for the abort-during-query scenario.
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT pg_sleep(5)"))
        await _send_json(
            scope, receive, send, {"object": "list", "models": [], "data": []}
        )

    return _app


# ── RED scenarios: reproduce the leak ──────────────────────────────


async def test_disconnect_during_auth_db_lookup(db_env, alembic_solar_control, recwarn):
    """Scenario 1 (RED): disconnect while the auth DB lookup is in-flight.

    The api_keys table is locked from outside the app, so the auth
    middleware's ``resolve_by_api_key`` blocks. The raw-socket close lands
    mid-lookup. After the abort settles, the pool must be fully returned
    and no log signatures may fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        lock_conn: Any = None
        lock_txn: Any = None
        try:
            lock_conn, lock_txn = await _lock_table(db_url, "api_keys")
            # The abort: disconnect mid-auth-lookup.
            await _raw_request(
                port, "/v1/models", api_key=TEST_KEY, abort_after=ABORT_AFTER_S
            )
        finally:
            # Release the lock BEFORE the settle check: the lock
            # connection itself sits idle in transaction while it holds
            # the table lock, and it must not count as a leak.
            if lock_txn is not None:
                await lock_txn.rollback()
            if lock_conn is not None:
                await lock_conn.close()
        await _settle_and_check(engine, log_capture, db_url, "auth-lookup")
        await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_disconnect_during_slow_query(db_env, alembic_solar_control, recwarn):
    """Scenario 2 (RED): disconnect mid-query in the handler.

    The route runs ``SELECT pg_sleep(5)`` through a session. The abort
    lands while the query is in-flight inside the handler. After the abort
    settles, the pool must be fully returned and no log signatures may
    fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            # Sanity: the route machinery works before the abort.
            resp = await _raw_request(port, "/health")
            assert (
                resp is not None and b"200" in resp
            ), "sanity: /health failed before the abort"

            # The abort: disconnect mid-query.
            await _raw_request(
                port, "/v1/models", api_key=TEST_KEY, abort_after=ABORT_AFTER_S
            )
            await _settle_and_check(engine, log_capture, db_url, "slow-query")
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_disconnect_during_streaming_response(
    db_env, alembic_solar_control, recwarn
):
    """Scenario 3 (RED): disconnect mid-stream on a StreamingResponse.

    The streaming generator holds a DB session while producing chunks. The
    abort lands mid-stream (after the first chunk, before the second).
    After the abort settles, the pool must be fully returned and no log
    signatures may fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            # The abort: read the first chunk (proves the stream started),
            # then disconnect mid-stream.
            await _raw_request(
                port,
                "/v1/stream",
                api_key=TEST_KEY,
                abort_after=1.0,
                read_first=True,
            )
            await _settle_and_check(engine, log_capture, db_url, "streaming")
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_disconnect_during_blocked_checkout(
    db_env, alembic_solar_control, recwarn
):
    """Scenario 4 (RED): pool filled from outside, disconnect during checkout.

    Both pool slots are held by raw ``engine.connect()`` calls, so the
    request's session checkout blocks. The abort lands during that
    checkout. After the abort settles and the fill is released, the pool
    must be fully returned and no log signatures may fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            # Fill the pool from outside the app so the checkout blocks.
            conns = [await engine.connect() for _ in range(POOL_SIZE)]
            try:
                # The abort: disconnect while the checkout is still waiting.
                await _raw_request(
                    port, "/v1/models", api_key=TEST_KEY, abort_after=ABORT_AFTER_S
                )
                # Release the fill so checkedout measures the aborted
                # request's connection, not the fill.
                for conn in conns:
                    await conn.close()
                conns.clear()
                await _settle_and_check(engine, log_capture, db_url, "checkout")
            finally:
                for conn in conns:
                    with contextlib.suppress(Exception):
                        await conn.close()
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_pre_ping_disconnect(db_env, alembic_solar_control, recwarn):
    """Scenario 4 (Pre-ping path): disconnect while pre-ping fails.

    One normal request populates the pool, then a raw asyncpg side
    connection terminates the app's idle backends, making every pooled
    connection stale. The next /v1/models request's ``pool_pre_ping`` ping
    fails and the pool reconnects — the abort lands inside that window.
    Kept single-test: a NO-abort variant would duplicate the
    after-response control plus the populate step. After the abort
    settles, the pool must be fully returned and no log signatures may
    fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            # Populate the pool: one normal request (pg_sleep(5) runs on
            # the pooled connection, then the connection sits idle).
            resp = await _raw_request(port, "/v1/models", api_key=TEST_KEY)
            assert resp is not None and b"200" in resp, "sanity: /v1/models failed"

            # Make the pooled connections stale: terminate the app's idle
            # backends from outside the app.
            terminated = await _terminate_idle_backends(db_url)
            assert terminated >= 1, "sanity: no idle app backends found to terminate"

            # The abort: disconnect while pre-ping fails and reconnect
            # runs.
            await _raw_request(
                port, "/v1/models", api_key=TEST_KEY, abort_after=ABORT_AFTER_S
            )
            await _settle_and_check(engine, log_capture, db_url, "pre-ping")
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_webui_read_disconnect(db_env, alembic_solar_control, recwarn):
    """Scenario 5 (Webui read path): disconnect mid-query on /api/gateway/*.

    The REAL management router (app/routes/management/gateway.py) is
    mounted exactly as app/main.py mounts it: ``APIRouter(prefix="/api")``
    behind the real ``auth_middleware``, which authorizes /api/* against
    ``settings.management_api_key``. The stats route reads through a
    ``session_factory()`` block; the ``gateway_requests`` table is locked
    from outside the app so the disconnect lands mid-query. After the
    abort settles, the pool must be fully returned and no log signatures
    may fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(
            db_url, asgi_app=_make_app(include_management=True)
        )
        lock_conn: Any = None
        lock_txn: Any = None
        try:
            # Sanity: the management route machinery works before the
            # abort (auth against the management key).
            mgmt_key = settings.management_api_key
            resp = await _raw_request(port, "/api/gateway/stats", api_key=mgmt_key)
            assert (
                resp is not None and b"200" in resp
            ), "sanity: /api/gateway/stats failed before the abort"

            lock_conn, lock_txn = await _lock_table(db_url, "gateway_requests")
            try:
                # The abort: disconnect mid-query (the aggregate read
                # blocks on the table lock).
                await _raw_request(
                    port,
                    "/api/gateway/stats",
                    api_key=mgmt_key,
                    abort_after=ABORT_AFTER_S,
                )
            finally:
                # Release the lock BEFORE the settle check: the lock
                # connection itself sits idle in transaction while it
                # holds the table lock, and it must not count as a leak.
                if lock_txn is not None:
                    await lock_txn.rollback()
                if lock_conn is not None:
                    await lock_conn.close()
            await _settle_and_check(engine, log_capture, db_url, "webui-read")
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


# ── GREEN scenarios: control (no leak expected) ────────────────────


async def test_pure_asgi_control(db_env, alembic_solar_control, recwarn):
    """Scenario 6 (Control): abort-during-query with a PURE ASGI middleware.

    Same abort-during-query shape as the RED slow-query scenario, but the
    middleware is a plain ASGI class (``async def __call__(self, scope,
    receive, send)``) and the response is plain JSON — no
    BaseHTTPMiddleware cancel scope around the DB call, no streaming. The
    DB lookup is the app's own ``endpoint_db.resolve_by_api_key`` +
    ``session_factory()`` pattern. After the abort settles, the pool must
    be fully returned and no log signatures may fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(
            db_url, asgi_app=_make_pure_asgi_app()
        )
        try:
            # Sanity: the pure-ASGI route machinery works before the abort.
            resp = await _raw_request(port, "/v1/models", api_key=TEST_KEY)
            assert (
                resp is not None and b"200" in resp
            ), "sanity: pure-ASGI /v1/models failed"

            # The abort: disconnect mid-query.
            await _raw_request(
                port, "/v1/models", api_key=TEST_KEY, abort_after=ABORT_AFTER_S
            )
            await _settle_and_check(engine, log_capture, db_url, "pure-asgi")
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_streaming_burst(db_env, alembic_solar_control, recwarn):
    """Burst: repeat the streaming abort many times, assert clean at end.

    Loops the scenario-3 streaming abort ``BURST_ITERATIONS`` times in one
    test with a short settle between iterations. Per-iteration log
    signatures are COUNTED, not asserted — the race is the point. At the
    end the pool must be fully returned (checkedout()==0 + no
    idle-in-transaction); the count of iterations that showed signatures
    is reported via the test log.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            # Sanity: the stream machinery works before the burst.
            resp = await _raw_request(
                port,
                "/v1/stream",
                api_key=TEST_KEY,
                abort_after=0.5,
                read_first=True,
            )
            assert resp is None, "sanity: aborted stream returned a response"

            signature_iterations = 0
            for i in range(BURST_ITERATIONS):
                await _raw_request(
                    port,
                    "/v1/stream",
                    api_key=TEST_KEY,
                    abort_after=1.0,
                    read_first=True,
                )
                # Short settle: bounded runtime, enough for the cancel to
                # propagate and the leak (if any) to happen.
                await asyncio.sleep(BURST_SETTLE_S)
                if log_capture.matching(_GC_SIGNATURE) or log_capture.matching(
                    _TERMINATE_SIGNATURE
                ):
                    signature_iterations += 1
                # Reset the capture so the count is per-iteration.
                log_capture.messages.clear()

            checked_out = engine.sync_engine.pool.checkedout()
            gc.collect()
            await asyncio.sleep(BURST_SETTLE_S)
            checked_out_final = engine.sync_engine.pool.checkedout()
            idle_in_txn = await _idle_in_transaction_count(db_url)

            logging.getLogger(__name__).info(
                "burst: %d/%d iterations showed leak signatures; "
                "checked_out=%d (post-gc %d); idle_in_txn=%d",
                signature_iterations,
                BURST_ITERATIONS,
                checked_out,
                checked_out_final,
                idle_in_txn,
            )
            assert checked_out_final == 0, (
                f"[burst] pool leak: {checked_out_final} connection(s) "
                "still checked out after burst settled"
            )
            assert idle_in_txn == 0, (
                f"[burst] idle-in-transaction leak: {idle_in_txn} session(s) "
                "left 'idle in transaction' after burst settled"
            )
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_disconnect_after_response_completes(
    db_env, alembic_solar_control, recwarn
):
    """Scenario 5 (GREEN): disconnect after the response is fully received.

    The fast query completes before the abort. After the disconnect
    settles, the pool must be fully returned and no log signatures may
    fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            # The abort: read the full response (fast query), then
            # disconnect.
            resp = await _raw_request(port, "/v1/models", api_key=TEST_KEY)
            assert resp is not None and b"200" in resp, "sanity: /v1/models failed"
            await _raw_request(port, "/v1/models", api_key=TEST_KEY, abort_after=0.1)
            await _settle_and_check(engine, log_capture, db_url, "after-response")
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_no_disconnect_pool_stays_clean(db_env, alembic_solar_control, recwarn):
    """Scenario 6 (GREEN): no disconnect at all — the control scenario.

    The request completes normally. The pool must be fully returned and no
    log signatures may fire.
    """
    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            resp = await _raw_request(port, "/v1/models", api_key=TEST_KEY)
            assert resp is not None and b"200" in resp, "sanity: /v1/models failed"
            await _settle_and_check(engine, log_capture, db_url, "no-disconnect")
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)


async def test_metrics_counters_clean_after_abort(db_env, alembic_solar_control):
    """Task-4 observability: install_pool_metrics sees a clean abort.

    Runs the scenario-2 abort-during-slow-query shape with
    ``install_pool_metrics`` wired onto the engine (as lifespan now does).
    With the #99 fix in place, no production signatures fire even under
    abort, so the terminate and GC-warning counters must be ZERO after the
    abort settles, while the checkout/checkin counters show real traffic.
    """
    import gc

    from app.database.pool_metrics import reset_counters, snapshot_counters

    db_url = db_env["control_db"]
    log_capture = _LogCapture()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    pool_logger.addHandler(log_capture)
    try:
        reset_counters()
        server, serve_task, port, engine = await _start_server(db_url)
        try:
            # Wire metrics exactly as lifespan does (post-init_db).
            from app.database.pool_metrics import install_pool_metrics

            install_pool_metrics(engine)

            # Sanity: the route machinery works before the abort.
            resp = await _raw_request(port, "/health")
            assert (
                resp is not None and b"200" in resp
            ), "sanity: /health failed before the abort"

            # The abort: disconnect mid-query.
            await _raw_request(
                port, "/v1/models", api_key=TEST_KEY, abort_after=ABORT_AFTER_S
            )

            # Same settle loop as _settle_and_check, then assert the
            # metrics counters instead of the log signatures.
            for _ in range(int(SETTLE_S * 10)):
                await asyncio.sleep(0.1)
                if engine.sync_engine.pool.checkedout() == 0:
                    break
            gc.collect()
            await asyncio.sleep(0.5)

            counters = snapshot_counters()
            logging.getLogger(__name__).info("metrics after abort: %s", counters)
            # The fix means no production signatures fire even under abort:
            # the leak-signature counters must be ZERO.
            assert (
                counters["terminate"] == 0
            ), f"[metrics] terminate counter nonzero: {counters['terminate']}"
            assert counters["gc_non_checked_in"] == 0, (
                f"[metrics] GC-warning counter nonzero: "
                f"{counters['gc_non_checked_in']}"
            )
            # Real traffic must have been counted: at least the /health +
            # /v1/models checkouts, and the aborted request's connection
            # must have been checked back in (checkedin >= checkout count).
            assert (
                counters["checkout"] >= 2
            ), f"[metrics] checkout counter too low: {counters['checkout']}"
            assert (
                counters["checkin"] >= 2
            ), f"[metrics] checkin counter too low: {counters['checkin']}"
        finally:
            await _stop_server(server, serve_task)
    finally:
        pool_logger.removeHandler(log_capture)
