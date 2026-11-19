from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth import get_current_user
from app.db import get_pool

_SUMMARY_SQL = """
SELECT
    COUNT(*)::int AS total_calls,
    COUNT(*) FILTER (WHERE status = 'ok')::int AS ok_calls,
    COUNT(*) FILTER (WHERE status = 'error')::int AS error_calls,
    COUNT(*) FILTER (WHERE status = 'denied')::int AS denied_count,
    ROUND(COALESCE(AVG(latency_ms), 0)::numeric, 2)::float AS avg_latency_ms,
    COALESCE(SUM(est_tokens), 0)::bigint AS total_est_tokens
FROM tool_calls
WHERE created_at >= NOW() - make_interval(days => %(days)s);
"""

_BY_USER_SQL = """
SELECT
    COALESCE(u.username, 'anonymous') AS username,
    COALESCE(u.role, 'unknown') AS role,
    COUNT(*)::int AS total_calls,
    COUNT(*) FILTER (WHERE tc.status = 'ok')::int AS ok_calls,
    COUNT(*) FILTER (WHERE tc.status = 'error')::int AS error_calls,
    COUNT(*) FILTER (WHERE tc.status = 'denied')::int AS denied_count,
    ROUND(COALESCE(AVG(tc.latency_ms), 0)::numeric, 2)::float AS avg_latency_ms,
    COALESCE(SUM(tc.est_tokens), 0)::bigint AS total_est_tokens
FROM tool_calls tc
LEFT JOIN users u ON u.id = tc.user_id
WHERE tc.created_at >= NOW() - make_interval(days => %(days)s)
GROUP BY u.id, u.username, u.role
ORDER BY total_calls DESC, username ASC;
"""

_BY_TOOL_SQL = """
SELECT
    tc.tool_name,
    COUNT(*)::int AS total_calls,
    COUNT(*) FILTER (WHERE tc.status = 'ok')::int AS ok_calls,
    COUNT(*) FILTER (WHERE tc.status = 'error')::int AS error_calls,
    COUNT(*) FILTER (WHERE tc.status = 'denied')::int AS denied_count,
    ROUND(COALESCE(AVG(tc.latency_ms), 0)::numeric, 2)::float AS avg_latency_ms,
    COALESCE(SUM(tc.est_tokens), 0)::bigint AS total_est_tokens
FROM tool_calls tc
WHERE tc.created_at >= NOW() - make_interval(days => %(days)s)
GROUP BY tc.tool_name
ORDER BY total_calls DESC, tc.tool_name ASC;
"""


async def fetch_usage_stats(days: int = 7) -> dict[str, Any]:
    """Query Postgres for aggregated tool usage metrics over the last N days."""
    pool = await get_pool()
    params = {"days": days}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SUMMARY_SQL, params)
            summary = await cur.fetchone() or {}

            await cur.execute(_BY_USER_SQL, params)
            by_user = list(await cur.fetchall())

            await cur.execute(_BY_TOOL_SQL, params)
            by_tool = list(await cur.fetchall())

    total_calls = int(summary.get("total_calls", 0))
    error_calls = int(summary.get("error_calls", 0))
    error_rate = round(error_calls / total_calls, 4) if total_calls > 0 else 0.0

    return {
        "window_days": days,
        "total_calls": total_calls,
        "ok_calls": int(summary.get("ok_calls", 0)),
        "error_calls": error_calls,
        "denied_count": int(summary.get("denied_count", 0)),
        "error_rate": error_rate,
        "avg_latency_ms": float(summary.get("avg_latency_ms", 0.0)),
        "total_est_tokens": int(summary.get("total_est_tokens", 0)),
        "by_user": by_user,
        "by_tool": by_tool,
    }


async def usage_endpoint(request: Request) -> JSONResponse:
    """HTTP GET /usage handler restricted to admin users."""
    user = get_current_user()
    if user is None:
        return JSONResponse(
            {"error": "unauthorized", "detail": "Authentication required."},
            status_code=401,
        )

    if user.role != "admin":
        return JSONResponse(
            {
                "error": "forbidden",
                "detail": (
                    f"Admin role required to access /usage "
                    f"(user '{user.username}' has role '{user.role}')."
                ),
            },
            status_code=403,
        )

    raw_days = request.query_params.get("days", "7")
    try:
        days = int(raw_days)
    except ValueError:
        return JSONResponse(
            {"error": "bad_request", "detail": "'days' must be an integer between 1 and 365."},
            status_code=400,
        )

    if days < 1 or days > 365:
        return JSONResponse(
            {"error": "bad_request", "detail": "'days' must be an integer between 1 and 365."},
            status_code=400,
        )

    stats = await fetch_usage_stats(days=days)
    return JSONResponse(stats)