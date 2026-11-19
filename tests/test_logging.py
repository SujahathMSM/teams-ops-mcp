from __future__ import annotations

import pytest

from app.auth import (
    AuthenticatedUser,
    require_role,
    reset_current_user,
    set_current_user,
)
from app.db import get_pool
from app.logging_db import audited_tool, redact_secrets
from tests.conftest import TestUser


def test_redact_secrets_scrubs_sensitive_keys_and_token_patterns() -> None:
    payload = {
        "repo": "octocat/demo",
        "api_key": "should_hide",
        "nested": {
            "user_password": "secret_value",
            "notes": [
                "keep this normal note",
                "leaked tomcp_abc123SecretValue here",
                "Authorization: Bearer xyz.123.token",
            ],
        },
    }

    cleaned = redact_secrets(payload)
    assert cleaned["repo"] == "octocat/demo"
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["user_password"] == "[REDACTED]"
    assert cleaned["nested"]["notes"][0] == "keep this normal note"
    assert "tomcp_abc123SecretValue" not in cleaned["nested"]["notes"][1]
    assert "[REDACTED]" in cleaned["nested"]["notes"][1]
    assert "xyz.123.token" not in cleaned["nested"]["notes"][2]


async def test_audited_tool_logs_ok_denied_and_error_rows(viewer_user: TestUser) -> None:
    @audited_tool
    async def sample_ok(message: str, token: str = "secret") -> dict[str, str]:
        return {"echo": message}

    @audited_tool
    async def sample_write(repo: str) -> str:
        require_role("editor", "admin", tool_name="sample_write")
        return f"wrote {repo}"

    @audited_tool
    async def sample_boom(query: str) -> str:
        raise RuntimeError("boom!")

    ctx = set_current_user(
        AuthenticatedUser(
            id=viewer_user.id,
            username=viewer_user.username,
            role=viewer_user.role,
        )
    )
    try:
        res = await sample_ok(message="hello tomcp_inlineSecret99", token="my_secret_arg")
        assert res == {"echo": "hello tomcp_inlineSecret99"}

        with pytest.raises(Exception, match="Permission denied"):
            await sample_write(repo="octocat/demo")

        with pytest.raises(RuntimeError, match="boom!"):
            await sample_boom(query="crash")
    finally:
        reset_current_user(ctx)

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user_id, tool_name, arguments, status, response_chars, est_tokens,
                       error_message
                FROM tool_calls
                ORDER BY id ASC;
                """
            )
            rows = list(await cur.fetchall())

    assert len(rows) == 3

    # 1. ok row with secret redaction
    assert rows[0]["user_id"] == viewer_user.id
    assert rows[0]["tool_name"] == "sample_ok"
    assert rows[0]["status"] == "ok"
    assert rows[0]["arguments"]["token"] == "[REDACTED]"
    assert "tomcp_inlineSecret99" not in rows[0]["arguments"]["message"]
    assert rows[0]["response_chars"] > 0
    assert rows[0]["est_tokens"] == rows[0]["response_chars"] // 4

    # 2. denied row
    assert rows[1]["tool_name"] == "sample_write"
    assert rows[1]["status"] == "denied"
    assert "Permission denied" in rows[1]["error_message"]

    # 3. error row
    assert rows[2]["tool_name"] == "sample_boom"
    assert rows[2]["status"] == "error"
    assert rows[2]["error_message"] == "boom!"