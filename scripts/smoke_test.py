"""End-to-end smoke test for a running Team Ops MCP server.

Talks to the real HTTP stack (Bearer auth -> MCP Streamable HTTP -> tools -> Postgres audit
-> /usage) exactly like an AI client would, using raw JSON-RPC so it does not depend on any
particular MCP client library.

Usage:
    python scripts/smoke_test.py --viewer-token tomcp_... --admin-token tomcp_...
    # or set SMOKE_VIEWER_TOKEN / SMOKE_ADMIN_TOKEN, and optionally SMOKE_BASE_URL

Exits 0 when every check passes, 1 on the first failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"
EXPECTED_TOOLS = {
    "ping",
    "github_open_prs",
    "github_issue_summary",
    "create_issue",
    "incidents_search",
}


class SmokeTestFailure(Exception):
    """Raised when a smoke-test check fails."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestFailure(message)
    print(f"  PASS  {message}")


def parse_jsonrpc_response(response: httpx.Response) -> dict[str, Any]:
    """Return the JSON-RPC message from a plain JSON or text/event-stream response."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    return json.loads(payload)
        raise SmokeTestFailure("SSE response contained no data event")
    return response.json()


class McpSession:
    """Minimal MCP Streamable HTTP client: initialize, then JSON-RPC requests."""

    def __init__(self, client: httpx.Client, base_url: str, token: str) -> None:
        self.client = client
        self.url = f"{base_url.rstrip('/')}/mcp"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._next_id = 1

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        return self.client.post(self.url, headers=self.headers, json=body)

    def initialize(self) -> dict[str, Any]:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._take_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "team-ops-smoke-test", "version": "1.0.0"},
                },
            }
        )
        if response.status_code != 200:
            raise SmokeTestFailure(f"initialize returned HTTP {response.status_code}")
        message = parse_jsonrpc_response(response)
        result = message.get("result") or {}

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
        self.headers["MCP-Protocol-Version"] = result.get("protocolVersion", PROTOCOL_VERSION)

        notified = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if notified.status_code not in (200, 202):
            raise SmokeTestFailure(f"initialized notification got HTTP {notified.status_code}")
        return result

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._take_id(), "method": method}
        if params is not None:
            body["params"] = params
        response = self._post(body)
        if response.status_code != 200:
            raise SmokeTestFailure(f"{method} returned HTTP {response.status_code}")
        message = parse_jsonrpc_response(response)
        if "error" in message:
            raise SmokeTestFailure(f"{method} JSON-RPC error: {message['error']}")
        return message["result"]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def _take_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current


def tool_text(result: dict[str, Any]) -> str:
    """Concatenate the text content blocks of a tools/call result."""
    return " ".join(
        block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
    )


def run(base_url: str, viewer_token: str, admin_token: str) -> None:
    with httpx.Client(timeout=30.0) as client:
        print("1. Public + auth boundary")
        health = client.get(f"{base_url}/health")
        check(health.status_code == 200, "GET /health is public and returns 200")
        check(health.json().get("status") == "ok", "/health reports status=ok")

        unauth = client.post(f"{base_url}/mcp", json={})
        check(unauth.status_code == 401, "POST /mcp without a token returns 401")

        bad = client.post(
            f"{base_url}/mcp", json={}, headers={"Authorization": "Bearer tomcp_invalid"}
        )
        check(bad.status_code == 401, "POST /mcp with an invalid token returns 401")

        print("2. MCP handshake as viewer")
        session = McpSession(client, base_url, viewer_token)
        init = session.initialize()
        check("serverInfo" in init, f"initialize succeeded (server={init['serverInfo']['name']})")

        tools = {tool["name"] for tool in session.request("tools/list")["tools"]}
        check(EXPECTED_TOOLS <= tools, f"tools/list exposes all 5 tools: {sorted(tools)}")

        print("3. Tool calls")
        ping = session.call_tool("ping", {"message": "smoke"})
        check(not ping.get("isError"), "ping returns successfully")

        search = session.call_tool("incidents_search", {"query": "postgres deadlock"})
        check(not search.get("isError"), "incidents_search returns successfully")
        check("INC-1008" in tool_text(search), "incidents_search finds INC-1008 (Postgres FTS)")

        denied = session.call_tool(
            "create_issue", {"repo": "octocat/Hello-World", "title": "smoke test"}
        )
        check(bool(denied.get("isError")), "create_issue is rejected for a viewer")
        check(
            "Permission denied" in tool_text(denied),
            "rejection carries the RBAC 'Permission denied' message",
        )

        print("4. Usage analytics")
        viewer_usage = client.get(
            f"{base_url}/usage?days=1", headers={"Authorization": f"Bearer {viewer_token}"}
        )
        check(viewer_usage.status_code == 403, "/usage is forbidden for a viewer")

        admin_usage = client.get(
            f"{base_url}/usage?days=1", headers={"Authorization": f"Bearer {admin_token}"}
        )
        check(admin_usage.status_code == 200, "/usage returns 200 for an admin")
        stats = admin_usage.json()
        by_tool = {row["tool_name"]: row for row in stats["by_tool"]}
        check(by_tool.get("incidents_search", {}).get("ok_calls", 0) >= 1, "audit: search ok")
        check(by_tool.get("create_issue", {}).get("denied_count", 0) >= 1, "audit: denial")


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end smoke test for Team Ops MCP")
    parser.add_argument(
        "--base-url", default=os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--viewer-token", default=os.environ.get("SMOKE_VIEWER_TOKEN", ""))
    parser.add_argument("--admin-token", default=os.environ.get("SMOKE_ADMIN_TOKEN", ""))
    args = parser.parse_args()

    if not args.viewer_token or not args.admin_token:
        parser.error("viewer and admin tokens are required (flags or SMOKE_* env vars)")

    try:
        run(args.base_url.rstrip("/"), args.viewer_token, args.admin_token)
    except SmokeTestFailure as exc:
        print(f"  FAIL  {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"  FAIL  could not reach server: {exc}", file=sys.stderr)
        return 1

    print("\nAll smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
