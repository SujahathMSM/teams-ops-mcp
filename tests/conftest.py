from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import sql

from app.auth import generate_raw_token, hash_token
from app.config import settings
from app.db import close_pool, configure_windows_event_loop, get_pool
from app.server import create_app

# Must execute before pytest-asyncio creates the session event loop on Windows.
configure_windows_event_loop()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class TestUser:
    """Test user record paired with its plaintext Bearer token."""

    id: int
    username: str
    role: str
    raw_token: str


def _derive_test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL", "").strip()
    if explicit:
        return explicit

    parsed = urlparse(settings.database_url)
    base_db = parsed.path.lstrip("/") or "teamops"
    test_db = base_db if base_db.endswith("_test") else f"{base_db}_test"
    return urlunparse(parsed._replace(path=f"/{test_db}"))


def _bootstrap_test_database(test_db_url: str) -> None:
    """Create the *_test database if missing and apply schema.sql + seed.sql."""
    parsed = urlparse(test_db_url)
    target_db = parsed.path.lstrip("/")
    admin_url = urlunparse(parsed._replace(path="/postgres"))

    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s;",
                (target_db,),
            ).fetchone()
            if row is None:
                conn.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db))
                )
    except psycopg.OperationalError:
        # Fallback if 'postgres' database is inaccessible; try base settings.database_url
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s;",
                (target_db,),
            ).fetchone()
            if row is None:
                conn.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db))
                )

    schema_sql = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
    seed_sql = (PROJECT_ROOT / "sql" / "seed.sql").read_text(encoding="utf-8")

    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute(schema_sql)
        conn.execute(seed_sql)


@pytest.fixture(scope="session")
async def setup_test_db() -> AsyncIterator[None]:
    """Point settings to the test DB, apply schema/seed once per session, and close pool."""
    test_db_url = _derive_test_database_url()
    _bootstrap_test_database(test_db_url)

    original_url = settings.database_url
    await close_pool()
    settings.database_url = test_db_url

    yield

    await close_pool()
    settings.database_url = original_url


@pytest.fixture
async def clean_db(setup_test_db: None) -> AsyncIterator[None]:
    """Truncate users and tool_calls before each DB test while preserving seeded incidents."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE TABLE tool_calls, users RESTART IDENTITY CASCADE;")
        await conn.commit()
    yield


@pytest.fixture
def create_test_user(
    clean_db: None,
) -> Callable[[str, str, bool], Awaitable[TestUser]]:
    """Factory fixture that inserts a user with a hashed token into the test database."""

    async def _create(
        username: str,
        role: str = "viewer",
        active: bool = True,
    ) -> TestUser:
        raw_token = generate_raw_token()
        token_hash = hash_token(raw_token)
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO users (username, role, token_hash, active)
                    VALUES (%(username)s, %(role)s, %(token_hash)s, %(active)s)
                    RETURNING id;
                    """,
                    {
                        "username": username,
                        "role": role,
                        "token_hash": token_hash,
                        "active": active,
                    },
                )
                row = await cur.fetchone()
            await conn.commit()
        assert row is not None
        return TestUser(
            id=row["id"],
            username=username,
            role=role,
            raw_token=raw_token,
        )

    return _create


@pytest.fixture
async def viewer_user(
    create_test_user: Callable[[str, str, bool], Awaitable[TestUser]],
) -> TestUser:
    """Pre-created viewer user."""
    return await create_test_user("alice", "viewer", True)


@pytest.fixture
async def editor_user(
    create_test_user: Callable[[str, str, bool], Awaitable[TestUser]],
) -> TestUser:
    """Pre-created editor user."""
    return await create_test_user("bob", "editor", True)


@pytest.fixture
async def admin_user(
    create_test_user: Callable[[str, str, bool], Awaitable[TestUser]],
) -> TestUser:
    """Pre-created admin user."""
    return await create_test_user("admin", "admin", True)


@pytest.fixture
async def asgi_client(clean_db: None) -> AsyncIterator[AsyncClient]:
    """Async HTTP client wired directly to the ASGI app (no live TCP server needed)."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        yield client