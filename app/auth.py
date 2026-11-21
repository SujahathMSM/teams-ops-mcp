from __future__ import annotations

import hashlib
import logging
import secrets
from contextvars import ContextVar, Token
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.db import get_pool

logger = logging.getLogger(__name__)

VALID_ROLES: tuple[str, ...] = ("viewer", "editor", "admin")

# Why throttle: an MCP session sends many requests per minute; writing last_used_at on
# every one would turn each read-only request into a DB write for no extra insight.
LAST_USED_THROTTLE_SECONDS = 60


class PermissionDeniedError(ToolError):
    """Raised when an authenticated user lacks the required role for a tool."""


@dataclass(frozen=True)
class AuthenticatedUser:
    """Represents the currently authenticated user for an HTTP/MCP request."""

    id: int
    username: str
    role: str


_current_user: ContextVar[AuthenticatedUser | None] = ContextVar("current_user", default=None)


def get_current_user() -> AuthenticatedUser | None:
    """Return the authenticated user bound to the current async request context."""
    return _current_user.get()


def set_current_user(user: AuthenticatedUser | None) -> Token[AuthenticatedUser | None]:
    """Bind an authenticated user to the current async request context."""
    return _current_user.set(user)


def reset_current_user(token: Token[AuthenticatedUser | None]) -> None:
    """Reset the current user ContextVar using its token."""
    _current_user.reset(token)


def generate_raw_token() -> str:
    """Generate a cryptographically random raw Bearer token shown once to the user."""
    return f"tomcp_{secrets.token_urlsafe(32)}"


def hash_token(raw_token: str) -> str:
    """Return the 64-character lowercase SHA-256 hex digest of a raw token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def get_user_by_token(raw_token: str) -> AuthenticatedUser | None:
    """Look up an active user in Postgres by the SHA-256 hash of their raw token."""
    cleaned = raw_token.strip()
    if not cleaned:
        return None

    token_hash = hash_token(cleaned)
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, username, role, active
                FROM users
                WHERE token_hash = %(token_hash)s
                LIMIT 1;
                """,
                {"token_hash": token_hash},
            )
            row = await cur.fetchone()

    if row is None or not row["active"]:
        return None

    return AuthenticatedUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
    )


async def touch_last_used(user_id: int) -> None:
    """Record token usage time, at most once per LAST_USED_THROTTLE_SECONDS per user.

    Failures are logged and swallowed: usage tracking must never block authentication
    (e.g. if migration 001 has not been applied to an older database yet).
    """
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                """
                UPDATE users
                SET last_used_at = NOW()
                WHERE id = %(user_id)s
                  AND (
                    last_used_at IS NULL
                    OR last_used_at < NOW() - make_interval(secs => %(throttle)s)
                  );
                """,
                {"user_id": user_id, "throttle": LAST_USED_THROTTLE_SECONDS},
            )
            await conn.commit()
    except Exception:
        logger.warning("Could not update last_used_at for user_id=%s", user_id, exc_info=True)


def require_role(*allowed_roles: str, tool_name: str) -> AuthenticatedUser:
    """Verify the current user has one of the allowed roles, or raise PermissionDeniedError."""
    user = get_current_user()
    if user is None:
        raise PermissionDeniedError(f"Authentication required to call '{tool_name}'.")

    if user.role not in allowed_roles:
        roles_str = " or ".join(f"'{r}'" for r in allowed_roles)
        raise PermissionDeniedError(
            f"Permission denied: tool '{tool_name}' requires role {roles_str} "
            f"(user '{user.username}' has role '{user.role}')."
        )
    return user


def _extract_bearer_token(scope: Scope) -> str | None:
    for header_name, header_value in scope.get("headers", []):
        if header_name.lower() == b"authorization":
            decoded = header_value.decode("latin-1").strip()
            parts = decoded.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
                return token if token else None
            return None
    return None


class BearerAuthMiddleware:
    """Pure ASGI middleware enforcing Bearer token authentication on protected routes.

    Why pure ASGI instead of BaseHTTPMiddleware: BaseHTTPMiddleware buffers response
    bodies and can break streaming transports like MCP Streamable HTTP.
    """

    def __init__(self, app: ASGIApp, public_paths: set[str] | None = None) -> None:
        self.app = app
        self.public_paths = public_paths or {"/health"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        # Allow unauthenticated health checks and CORS OPTIONS preflight requests
        if method == "OPTIONS" or path in self.public_paths:
            await self.app(scope, receive, send)
            return

        raw_token = _extract_bearer_token(scope)
        if not raw_token:
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "detail": "Missing or malformed Authorization: Bearer <token> header.",
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        user = await get_user_by_token(raw_token)
        if user is None:
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "detail": "Invalid or inactive Bearer token.",
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await touch_last_used(user.id)

        ctx_token = set_current_user(user)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_user(ctx_token)
