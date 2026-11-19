from __future__ import annotations

from typing import Any

import psycopg
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.db import get_pool


class IncidentSearchError(ToolError):
    """Raised when incident search input is invalid or the database is unavailable."""


_SEARCH_SQL = """
WITH q AS (
    SELECT websearch_to_tsquery('english', %(query)s) AS tsq
)
SELECT
    i.id,
    i.incident_key,
    i.title,
    i.service,
    i.severity,
    i.status,
    i.summary,
    i.root_cause,
    i.runbook_steps,
    i.tags,
    i.occurred_at,
    ROUND(
        (
            COALESCE(ts_rank_cd(i.search_vector, q.tsq), 0.0)
            + CASE
                WHEN i.incident_key ILIKE %(like_pattern)s THEN 1.0
                WHEN i.title ILIKE %(like_pattern)s THEN 0.5
                WHEN i.service ILIKE %(like_pattern)s THEN 0.3
                ELSE 0.0
              END
        )::numeric,
        4
    )::float AS relevance_score
FROM incidents i
CROSS JOIN q
WHERE
    (q.tsq <> ''::tsquery AND i.search_vector @@ q.tsq)
    OR i.incident_key ILIKE %(like_pattern)s
    OR i.title ILIKE %(like_pattern)s
    OR i.service ILIKE %(like_pattern)s
    OR i.summary ILIKE %(like_pattern)s
    OR i.root_cause ILIKE %(like_pattern)s
    OR i.runbook_steps ILIKE %(like_pattern)s
ORDER BY relevance_score DESC, i.occurred_at DESC
LIMIT %(limit)s;
"""


async def search_incidents(query: str, limit: int = 5) -> dict[str, Any]:
    """Run full-text search across the incidents table in PostgreSQL."""
    cleaned_query = query.strip()
    if not cleaned_query:
        raise IncidentSearchError("Search query cannot be empty.")

    if limit < 1 or limit > 20:
        raise IncidentSearchError("limit must be between 1 and 20.")

    params = {
        "query": cleaned_query,
        "like_pattern": f"%{cleaned_query}%",
        "limit": limit,
    }

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SEARCH_SQL, params)
                rows = await cur.fetchall()
    except psycopg.Error as exc:
        raise IncidentSearchError(f"Database error while searching incidents: {exc}") from exc

    incidents: list[dict[str, Any]] = []
    for row in rows:
        occurred_at = row["occurred_at"]
        incidents.append(
            {
                "id": row["id"],
                "incident_key": row["incident_key"],
                "title": row["title"],
                "service": row["service"],
                "severity": row["severity"],
                "status": row["status"],
                "summary": row["summary"],
                "root_cause": row["root_cause"],
                "runbook_steps": row["runbook_steps"],
                "tags": list(row["tags"] or []),
                "occurred_at": (
                    occurred_at.isoformat()
                    if hasattr(occurred_at, "isoformat")
                    else str(occurred_at)
                ),
                "relevance_score": row["relevance_score"],
            }
        )

    return {
        "query": cleaned_query,
        "match_count": len(incidents),
        "incidents": incidents,
    }


def register_incident_tools(mcp: MCPServer) -> None:
    """Register incident search tools on the MCPServer instance."""

    @mcp.tool()
    async def incidents_search(query: str, limit: int = 5) -> dict[str, Any]:
        """Full-text search over past engineering incidents and runbooks in Postgres.

        Args:
            query: Keywords, error code, service name, or incident key (e.g. 'redis latency', 
            '502').
            limit: Maximum number of matching incidents to return (1-20, default 5).
        """
        return await search_incidents(query=query, limit=limit)