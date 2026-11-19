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
from tests.conftest import TestUser


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