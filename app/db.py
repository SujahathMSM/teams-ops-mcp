from __future__ import annotations

import asyncio
import sys
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings


def configure_windows_event_loop() -> None:
    """Configure SelectorEventLoop on Windows (required by psycopg3 async)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            import uvicorn.loops.asyncio as uvicorn_asyncio

            uvicorn_asyncio.asyncio_loop_factory = (
                lambda use_subprocess=False: asyncio.SelectorEventLoop
            )
        except ImportError:
            pass


configure_windows_event_loop()

_pool: AsyncConnectionPool[Any] | None = None


async def get_pool() -> AsyncConnectionPool[Any]:
    """Return the shared async Postgres connection pool, opening it lazily if needed."""
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"row_factory": dict_row},
        )
        await _pool.open()
    return _pool


async def close_pool() -> None:
    """Close the shared connection pool if open."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None