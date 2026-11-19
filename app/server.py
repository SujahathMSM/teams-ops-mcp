from __future__ import annotations

import logging
from datetime import UTC, datetime

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.auth import BearerAuthMiddleware
from app.config import settings
from app.db import configure_windows_event_loop
from app.logging_db import audited_tool
from app.tools.github import register_github_tools
from app.tools.incidents import register_incident_tools
from app.usage import usage_endpoint

configure_windows_event_loop()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Why MCPServer: In official Python MCP SDK v2.x, FastMCP was renamed to MCPServer.
mcp = MCPServer(settings.app_name)


@mcp.tool()
@audited_tool
async def ping(message: str = "pong") -> str:
    """Health-check tool that echoes back a message with the server UTC timestamp."""
    now = datetime.now(UTC).isoformat()
    return f"{settings.app_name}: {message} (utc={now})"


register_github_tools(mcp)
register_incident_tools(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Unauthenticated liveness endpoint for local checks, Docker, and CI."""
    return JSONResponse(
        {
            "status": "ok",
            "service": settings.app_name,
            "env": settings.app_env,
        }
    )


@mcp.custom_route("/usage", methods=["GET"])
async def usage(request: Request) -> JSONResponse:
    """Admin-only analytics endpoint summarizing tool usage over ?days=N."""
    return await usage_endpoint(request)


def create_app() -> ASGIApp:
    """Create the ASGI app serving MCP Streamable HTTP at /mcp wrapped with Bearer auth."""
    mcp_starlette_app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        )
    )
    return BearerAuthMiddleware(mcp_starlette_app, public_paths={"/health"})


app = create_app()

if __name__ == "__main__":
    logger.info("Starting %s on http://%s:%s/mcp", settings.app_name, settings.host, settings.port)
    uvicorn.run(
        "app.server:app",
        host=settings.host,
        port=settings.port,
        reload=(settings.app_env == "development"),
    )
