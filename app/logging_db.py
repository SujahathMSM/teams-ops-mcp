from __future__ import annotations

import functools
import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from psycopg.types.json import Jsonb

from app.auth import PermissionDeniedError, get_current_user
from app.db import get_pool

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
)

# Redact raw tokens if a user or LLM accidentally embeds one inside a normal text argument
_SECRET_VALUE_PATTERN = re.compile(
    r"(tomcp_[A-Za-z0-9_-]+|ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|Bearer\s+[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_secrets(value: Any) -> Any:
    """Recursively redact sensitive keys and token patterns from tool arguments."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if _is_sensitive_key(key_str):
                redacted[key_str] = "[REDACTED]"
            else:
                redacted[key_str] = redact_secrets(v)
        return redacted

    if isinstance(value, list | tuple):
        return [redact_secrets(item) for item in value]

    if isinstance(value, str):
        return _SECRET_VALUE_PATTERN.sub("[REDACTED]", value)

    return value


def _measure_response(result: Any) -> tuple[int, int]:
    """Return (response_chars, est_tokens) where est_tokens = response_chars // 4."""
    if result is None:
        return 0, 0
    if isinstance(result, str):
        chars = len(result)
    else:
        try:
            chars = len(json.dumps(result, default=str))
        except TypeError:
            chars = len(str(result))
    return chars, chars // 4


async def log_tool_call(
    *,
    user_id: int | None,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    latency_ms: int,
    response_chars: int = 0,
    est_tokens: int = 0,
    error_message: str | None = None,
) -> None:
    """Insert an audit row into tool_calls with secrets redacted from arguments."""
    safe_args = redact_secrets(arguments)
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tool_calls (
                    user_id,
                    tool_name,
                    arguments,
                    status,
                    latency_ms,
                    response_chars,
                    est_tokens,
                    error_message
                )
                VALUES (
                    %(user_id)s,
                    %(tool_name)s,
                    %(arguments)s,
                    %(status)s,
                    %(latency_ms)s,
                    %(response_chars)s,
                    %(est_tokens)s,
                    %(error_message)s
                );
                """,
                {
                    "user_id": user_id,
                    "tool_name": tool_name,
                    "arguments": Jsonb(safe_args),
                    "status": status,
                    "latency_ms": max(0, latency_ms),
                    "response_chars": max(0, response_chars),
                    "est_tokens": max(0, est_tokens),
                    "error_message": error_message,
                },
            )
        await conn.commit()


def audited_tool(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Decorator that records every tool execution (ok, error, denied) in tool_calls."""
    sig = inspect.signature(fn)
    tool_name = fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = sig.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            call_args = dict(bound.arguments)
        except TypeError:
            call_args = dict(kwargs)

        user = get_current_user()
        user_id = user.id if user is not None else None

        start = time.perf_counter()
        status = "ok"
        response_chars = 0
        est_tokens = 0
        error_message: str | None = None

        try:
            result = await fn(*args, **kwargs)
            response_chars, est_tokens = _measure_response(result)
            return result
        except PermissionDeniedError as exc:
            status = "denied"
            error_message = str(exc)
            raise
        except Exception as exc:
            status = "error"
            error_message = str(exc)
            raise
        finally:
            latency_ms = int((time.perf_counter() - start) * 1000)
            try:
                await log_tool_call(
                    user_id=user_id,
                    tool_name=tool_name,
                    arguments=call_args,
                    status=status,
                    latency_ms=latency_ms,
                    response_chars=response_chars,
                    est_tokens=est_tokens,
                    error_message=error_message,
                )
            except Exception:
                logger.exception("Failed to write audit log for tool '%s'", tool_name)

    return wrapper