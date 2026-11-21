from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.auth import (
    AuthenticatedUser,
    PermissionDeniedError,
    generate_raw_token,
    get_user_by_token,
    hash_token,
    require_role,
    reset_current_user,
    set_current_user,
)
from app.db import get_pool
from tests.conftest import TestUser


async def _last_used_at(user_id: int):  # noqa: ANN202 - test helper
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT last_used_at FROM users WHERE id = %s;", (user_id,))
            row = await cur.fetchone()
    assert row is not None
    return row["last_used_at"]


def test_token_hashing_is_deterministic_and_not_plaintext() -> None:
    raw = generate_raw_token()
    assert raw.startswith("tomcp_")

    digest_1 = hash_token(raw)
    digest_2 = hash_token(raw)
    assert digest_1 == digest_2
    assert len(digest_1) == 64
    assert digest_1 != raw


async def test_health_endpoint_is_public(asgi_client: AsyncClient) -> None:
    resp = await asgi_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_mcp_rejects_missing_or_invalid_bearer_token(asgi_client: AsyncClient) -> None:
    resp_missing = await asgi_client.post("/mcp")
    assert resp_missing.status_code == 401
    assert resp_missing.json()["error"] == "unauthorized"

    resp_bad_scheme = await asgi_client.post(
        "/mcp",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp_bad_scheme.status_code == 401

    resp_invalid = await asgi_client.post(
        "/mcp",
        headers={"Authorization": "Bearer tomcp_does_not_exist"},
    )
    assert resp_invalid.status_code == 401
    assert resp_invalid.json()["error"] == "unauthorized"


async def test_inactive_user_token_is_rejected(
    asgi_client: AsyncClient,
    create_test_user: Callable[[str, str, bool], Awaitable[TestUser]],
) -> None:
    disabled = await create_test_user("disabled_user", "editor", False)
    assert await get_user_by_token(disabled.raw_token) is None

    resp = await asgi_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {disabled.raw_token}"},
    )
    assert resp.status_code == 401


async def test_active_user_lookup_succeeds(viewer_user: TestUser) -> None:
    found = await get_user_by_token(viewer_user.raw_token)
    assert found is not None
    assert found.id == viewer_user.id
    assert found.username == "alice"
    assert found.role == "viewer"


def test_require_role_enforces_viewer_vs_editor_vs_admin() -> None:
    # Unauthenticated context
    with pytest.raises(PermissionDeniedError, match="Authentication required"):
        require_role("editor", "admin", tool_name="create_issue")

    # Viewer denied on editor-only tool
    viewer_ctx = set_current_user(AuthenticatedUser(id=1, username="alice", role="viewer"))
    try:
        with pytest.raises(PermissionDeniedError, match="requires role 'editor' or 'admin'"):
            require_role("editor", "admin", tool_name="create_issue")
    finally:
        reset_current_user(viewer_ctx)

    # Editor allowed
    editor_ctx = set_current_user(AuthenticatedUser(id=2, username="bob", role="editor"))
    try:
        user = require_role("editor", "admin", tool_name="create_issue")
        assert user.username == "bob"
    finally:
        reset_current_user(editor_ctx)


async def test_authenticated_request_records_last_used_at_with_throttle(
    asgi_client: AsyncClient,
    viewer_user: TestUser,
) -> None:
    assert await _last_used_at(viewer_user.id) is None

    # /usage goes through the same Bearer middleware as /mcp (a viewer gets 403 after
    # authenticating), without needing the MCP session manager's lifespan in tests.
    headers = {"Authorization": f"Bearer {viewer_user.raw_token}"}
    assert (await asgi_client.get("/usage", headers=headers)).status_code == 403
    first_seen = await _last_used_at(viewer_user.id)
    assert first_seen is not None

    # A second request inside the throttle window must not rewrite the timestamp.
    await asgi_client.get("/usage", headers=headers)
    assert await _last_used_at(viewer_user.id) == first_seen


async def test_rejected_token_does_not_touch_last_used_at(
    asgi_client: AsyncClient,
    viewer_user: TestUser,
) -> None:
    resp = await asgi_client.get("/usage", headers={"Authorization": "Bearer tomcp_wrong"})
    assert resp.status_code == 401
    assert await _last_used_at(viewer_user.id) is None
