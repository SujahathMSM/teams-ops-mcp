from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.config import settings

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubAPIError(ToolError):
    """Raised when the GitHub REST API returns an expected error or invalid input is given."""


def _validate_repo(repo: str) -> str:
    cleaned = repo.strip()
    if not _REPO_PATTERN.match(cleaned):
        raise GitHubAPIError(
            f"Invalid repo '{repo}'. Expected 'owner/name' format (e.g. 'octocat/Hello-World')."
        )
    return cleaned


def _build_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{settings.app_name}/0.1.0",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _raise_for_github_status(response: httpx.Response, repo: str) -> None:
    if response.is_success:
        return

    try:
        detail = response.json().get("message", response.text)
    except ValueError:
        detail = response.text

    if response.status_code == 401:
        raise GitHubAPIError("GitHub authentication failed (401). Check GITHUB_TOKEN in .env.")
    if response.status_code == 403:
        raise GitHubAPIError(
            f"GitHub access forbidden or rate-limited (403) for '{repo}': {detail}"
        )
    if response.status_code == 404:
        raise GitHubAPIError(f"GitHub repository '{repo}' was not found (404).")
    raise GitHubAPIError(f"GitHub API error ({response.status_code}) for '{repo}': {detail}")


async def fetch_open_prs(repo: str) -> dict[str, Any]:
    """Fetch open pull requests for a GitHub repository (owner/name)."""
    repo_name = _validate_repo(repo)
    url = f"{settings.github_api_base_url.rstrip('/')}/repos/{repo_name}/pulls"

    async with httpx.AsyncClient(timeout=settings.github_timeout_seconds) as client:
        try:
            response = await client.get(
                url,
                headers=_build_headers(),
                params={
                    "state": "open",
                    "per_page": 50,
                    "sort": "updated",
                    "direction": "desc",
                },
            )
        except httpx.RequestError as exc:
            raise GitHubAPIError(f"Network error contacting GitHub API: {exc}") from exc

    _raise_for_github_status(response, repo_name)
    raw_prs = response.json()

    now = datetime.now(UTC)
    prs: list[dict[str, Any]] = []
    for pr in raw_prs:
        created_at = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
        age_days = round((now - created_at).total_seconds() / 86400, 1)

        reviewers = [r["login"] for r in pr.get("requested_reviewers", []) if "login" in r]
        team_reviewers = [
            f"team:{t['slug']}" for t in pr.get("requested_teams", []) if "slug" in t
        ]

        prs.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "author": (pr.get("user") or {}).get("login") or "unknown",
                "age_days": age_days,
                "requested_reviewers": reviewers + team_reviewers,
                "url": pr["html_url"],
            }
        )

    return {
        "repo": repo_name,
        "open_pr_count": len(prs),
        "prs": prs,
    }


async def fetch_issue_summary(repo: str, label: str | None = None) -> dict[str, Any]:
    """Summarize issues grouped by state and label (excluding PRs)."""
    repo_name = _validate_repo(repo)
    url = f"{settings.github_api_base_url.rstrip('/')}/repos/{repo_name}/issues"
    cleaned_label = label.strip() if label and label.strip() else None

    # Why cap at 3 pages (300 items): keeps latency and rate-limit usage bounded for an AI tool
    # while handling small-to-medium repos cleanly.
    max_pages = 3
    per_page = 100
    all_items: list[dict[str, Any]] = []
    truncated = False

    async with httpx.AsyncClient(timeout=settings.github_timeout_seconds) as client:
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {
                "state": "all",
                "per_page": per_page,
                "page": page,
            }
            if cleaned_label:
                params["labels"] = cleaned_label

            try:
                response = await client.get(url, headers=_build_headers(), params=params)
            except httpx.RequestError as exc:
                raise GitHubAPIError(f"Network error contacting GitHub API: {exc}") from exc

            _raise_for_github_status(response, repo_name)
            batch = response.json()
            all_items.extend(batch)

            if len(batch) < per_page:
                break
            if page == max_pages:
                truncated = True

    # Why filter out "pull_request": GitHub's /issues endpoint returns both issues and PRs.
    issues = [item for item in all_items if "pull_request" not in item]

    by_state = {"open": 0, "closed": 0}
    by_label: dict[str, dict[str, int]] = {}

    for issue in issues:
        state = issue.get("state", "open")
        if state not in by_state:
            by_state[state] = 0
        by_state[state] += 1

        labels = [lbl["name"] for lbl in issue.get("labels", []) if "name" in lbl]
        if not labels:
            labels = ["unlabeled"]

        for lbl_name in labels:
            if lbl_name not in by_label:
                by_label[lbl_name] = {"open": 0, "closed": 0, "total": 0}
            if state in ("open", "closed"):
                by_label[lbl_name][state] += 1
            by_label[lbl_name]["total"] += 1

    sorted_by_label = dict(
        sorted(by_label.items(), key=lambda kv: kv[1]["total"], reverse=True)
    )

    return {
        "repo": repo_name,
        "filter_label": cleaned_label,
        "total_issues": len(issues),
        "by_state": by_state,
        "by_label": sorted_by_label,
        "truncated": truncated,
    }


def register_github_tools(mcp: MCPServer) -> None:
    """Register GitHub tools on the MCPServer instance."""

    @mcp.tool()
    async def github_open_prs(repo: str) -> dict[str, Any]:
        """List open pull requests for a GitHub repository ('owner/name').

        Returns title, author, age in days, and requested reviewers for each open PR.
        """
        return await fetch_open_prs(repo)

    @mcp.tool()
    async def github_issue_summary(repo: str, label: str | None = None) -> dict[str, Any]:
        """Count repository issues grouped by state (open/closed) and label.

        Args:
            repo: Repository in 'owner/name' format.
            label: Optional GitHub label name to filter by.
        """
        return await fetch_issue_summary(repo, label=label)