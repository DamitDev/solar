"""FastAPI dependency that provides a per-request :class:`~sqlalchemy.ext.asyncio.AsyncSession`.

Transaction policy
------------------
* The dependency commits automatically when the request handler returns without
  raising an exception.
* If any exception propagates out of the handler the session is rolled back
  before the error is re-raised.
* The session is always closed in the ``finally`` block so no connection is
  leaked regardless of the outcome.

Usage::

    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.database import get_db_session

    @router.post("/items")
    async def create_item(session: AsyncSession = Depends(get_db_session)):
        ...

If a handler needs finer-grained control (e.g. a savepoint mid-request) it can
call ``await session.begin_nested()`` without affecting the outer commit/rollback
boundary owned by this dependency.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a SQLAlchemy :class:`AsyncSession` scoped to a single request."""
    factory = get_session_factory()
    session: AsyncSession = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
