from __future__ import annotations

from httpx import AsyncClient

from app.db import get_pool
from tests.conftest import TestUser


async def test_usage_endpoint_requires_admin_role(
    asgi_client: AsyncClient,
    viewer_user: TestUser,
    editor_user: TestUser,
) -> None:
    resp_unauth = await asgi_client.get("/usage")
    assert resp_unauth.status_code == 401

    resp_viewer = await asgi_client.get(
        "/usage",
        headers={"Authorization": f"Bearer {viewer_user.raw_token}"},
    )
    assert resp_viewer.status_code == 403

    resp_editor = await asgi_client.get(
        "/usage",
        headers={"Authorization": f"Bearer {editor_user.raw_token}"},
    )
    assert resp_editor.status_code == 403


async def test_usage_endpoint_validates_days_query_param(
    asgi_client: AsyncClient,
    admin_user: TestUser,
) -> None:
    headers = {"Authorization": f"Bearer {admin_user.raw_token}"}

    resp_non_int = await asgi_client.get("/usage?days=abc", headers=headers)
    assert resp_non_int.status_code == 400

    resp_zero = await asgi_client.get("/usage?days=0", headers=headers)
    assert resp_zero.status_code == 400


async def test_usage_endpoint_aggregates_window_accurately(
    asgi_client: AsyncClient,
    viewer_user: TestUser,
    editor_user: TestUser,
    admin_user: TestUser,
) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tool_calls
                    (user_id, tool_name, arguments, status, latency_ms, response_chars,
                     est_tokens, created_at)
                VALUES
                    (%(viewer_id)s, 'incidents_search', '{}'::jsonb, 'ok', 100, 400, 100, NOW()),
                    (%(viewer_id)s, 'create_issue', '{}'::jsonb, 'denied', 20, 0, 0, NOW()),
                    (%(editor_id)s, 'github_open_prs', '{}'::jsonb, 'error', 240, 0, 0, NOW()),
                    (%(editor_id)s, 'create_issue', '{}'::jsonb, 'ok', 400, 800, 200, NOW()),
                    (%(viewer_id)s, 'ping', '{}'::jsonb, 'ok', 10, 40, 10,
                     NOW() - INTERVAL '15 days');
                """,
                {"viewer_id": viewer_user.id, "editor_id": editor_user.id},
            )
        await conn.commit()

    headers = {"Authorization": f"Bearer {admin_user.raw_token}"}

    # 7-day window should exclude the 15-day-old call
    resp_7 = await asgi_client.get("/usage?days=7", headers=headers)
    assert resp_7.status_code == 200
    data_7 = resp_7.json()

    assert data_7["window_days"] == 7
    assert data_7["total_calls"] == 4
    assert data_7["ok_calls"] == 2
    assert data_7["error_calls"] == 1
    assert data_7["denied_count"] == 1
    assert data_7["error_rate"] == 0.25
    assert data_7["avg_latency_ms"] == 190.0
    assert data_7["total_est_tokens"] == 300
    assert len(data_7["by_user"]) == 2
    assert len(data_7["by_tool"]) == 3

    # 30-day window should include all 5 calls
    resp_30 = await asgi_client.get("/usage?days=30", headers=headers)
    assert resp_30.status_code == 200
    data_30 = resp_30.json()
    assert data_30["total_calls"] == 5
    assert data_30["total_est_tokens"] == 310
