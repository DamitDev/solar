"""SQLAlchemy 2.x async engine and session factory for Data Repository.

The module owns one async engine (postgresql+asyncpg://) and exposes a
session factory for FastAPI Depends() providers.
"""

from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the SQLAlchemy async session factory.

    Use this in FastAPI ``Depends()`` providers to obtain per-request
    :class:`~sqlalchemy.ext.asyncio.AsyncSession` instances.
    """
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized. Call init_db() first.")
    return _session_factory


async def init_db(database_url: str, *, min_size: int = 2, max_size: int = 10) -> None:
    """Create the SQLAlchemy async engine and session factory.

    Parameters
    ----------
    database_url:
        Sync PostgreSQL URL (``postgresql://...``).  The function derives the
        async-driver URL (``postgresql+asyncpg://...``) internally.
    min_size:
        Minimum connections in the pool.
    max_size:
        Maximum connections in the pool.
    """
    global _engine, _session_factory

    async_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    _engine = create_async_engine(
        async_url,
        pool_size=min_size,
        max_overflow=max(0, max_size - min_size),
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def close_db() -> None:
    """Dispose the SQLAlchemy engine and clear the session factory."""
    global _engine, _session_factory

    if _engine:
        await _engine.dispose()
        _engine = None

    _session_factory = None
