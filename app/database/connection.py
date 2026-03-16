"""Shared asyncpg connection pool for PostgreSQL."""

from typing import Optional

import asyncpg

_pool: Optional[asyncpg.Pool] = None


def db_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool


async def init_db(
    database_url: str, *, min_size: int = 2, max_size: int = 10
) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        database_url, min_size=min_size, max_size=max_size
    )
    return _pool


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
