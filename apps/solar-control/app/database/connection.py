"""SQLAlchemy async engine and session factory for PostgreSQL."""

import asyncio
import functools
import logging

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Strong references to in-flight isolated DB tasks: without this set the
# event loop may garbage-collect a live task mid-await while its caller is
# being cancelled. The done callback removes each task once the op finished.
_isolated_tasks: set[asyncio.Task] = set()


def _task_done(task: asyncio.Task) -> None:
    _isolated_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("Isolated DB operation failed: %r", exc)


async def run_isolated(coro):
    """Await *coro* inside its own asyncio task, through a shield.

    A client disconnect (anyio cancel scope) cancels the caller while a DB
    operation is mid-flight; a cancel landing during checkout/invalidate/
    terminate can strand the connection record until GC (#99). The task is
    kept referenced until done so it is not garbage-collected mid-await.
    After the outer await raises CancelledError, the task is NOT awaited
    again (anyio re-cancels at every await): it continues uncancelled in
    the background and the CancelledError propagates to the caller.
    Exceptions from the op propagate through the shield on the normal
    (non-cancelled) path.
    """
    task = asyncio.create_task(coro)
    _isolated_tasks.add(task)
    task.add_done_callback(_task_done)
    return await asyncio.shield(task)


def isolated_db_op(fn):
    """Decorator form of ``run_isolated`` for async DB operation methods.

    Runs the decorated operation in its own asyncio task and awaits it with
    ``asyncio.shield``, so a client disconnect stops the caller waiting but
    the query, any invalidate/terminate and the pool check-in finish in an
    uncancelled task (#99).
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await run_isolated(fn(*args, **kwargs))

    return wrapper


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized. Call init_db() first.")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


async def init_db(
    database_url: str,
    *,
    pool_size: int = 20,
    max_overflow: int = 10,
    pool_recycle: int = 300,
    pool_timeout: int = 30,
) -> AsyncEngine:
    global _engine, _session_factory

    sa_url = database_url
    if sa_url.startswith("postgresql://"):
        sa_url = sa_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    _engine = create_async_engine(
        sa_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
        pool_timeout=pool_timeout,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def close_db(drain_timeout: float = 5.0) -> None:
    global _engine, _session_factory
    if _isolated_tasks:
        await asyncio.wait(set(_isolated_tasks), timeout=drain_timeout)
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
