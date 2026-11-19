from __future__ import annotations

import httpx
import pytest
import respx

from app.config import settings
from app.tools.github import (
    GitHubAPIError,
    create_github_issue,
    fetch_issue_summary,
    fetch_open_prs,
)


@respx.mock
async def test_fetch_open_prs_parses_prs_and_reviewers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_token", "fake_gh_token")

    respx.get("https://api.github.com/repos/octocat/demo/pulls").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 42,
                    "title": "Add circuit breaker to GitHub client",
                    "user": {"login": "alice"},
                    "created_at": "2026-11-10T00:00:00Z",
                    "requested_reviewers": [{"login": "bob"}],
                    "requested_teams": [{"slug": "platform"}],
                    "html_url": "https://github.com/octocat/demo/pull/42",
                }
            ],
        )
    )

    result = await fetch_open_prs("octocat/demo")
    assert result["repo"] == "octocat/demo"
    assert result["open_pr_count"] == 1
    pr = result["prs"][0]
    assert pr["number"] == 42
    assert pr["author"] == "alice"
    assert pr["age_days"] >= 0
    assert pr["requested_reviewers"] == ["bob", "team:platform"]


async def test_fetch_open_prs_validates_repo_format() -> None:
    with pytest.raises(GitHubAPIError, match="Expected 'owner/name' format"):
        await fetch_open_prs("not-a-valid-repo")


@respx.mock
async def test_fetch_open_prs_handles_404_and_403() -> None:
    respx.get("https://api.github.com/repos/missing/repo/pulls").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(GitHubAPIError, match="was not found \\(404\\)"):
        await fetch_open_prs("missing/repo")

    respx.get("https://api.github.com/repos/limited/repo/pulls").mock(
        return_value=httpx.Response(403, json={"message": "API rate limit exceeded"})
    )
    with pytest.raises(GitHubAPIError, match="rate-limited \\(403\\)"):
        await fetch_open_prs("limited/repo")


@respx.mock
async def test_fetch_issue_summary_excludes_prs_and_groups_labels() -> None:
    respx.get("https://api.github.com/repos/octocat/demo/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "state": "open",
                    "labels": [{"name": "bug"}, {"name": "high-priority"}],
                },
                {
                    "number": 2,
                    "state": "closed",
                    "labels": [],
                },
                {
                    "number": 3,
                    "state": "open",
                    "labels": [{"name": "bug"}],
                    "pull_request": {"url": "https://api.github.com/repos/octocat/demo/pulls/3"},
                },
            ],
        )
    )

    summary = await fetch_issue_summary("octocat/demo")
    assert summary["total_issues"] == 2
    assert summary["by_state"] == {"open": 1, "closed": 1}
    assert summary["by_label"]["bug"] == {"open": 1, "closed": 0, "total": 1}
    assert summary["by_label"]["unlabeled"] == {"open": 0, "closed": 1, "total": 1}
    assert summary["truncated"] is False


@respx.mock
async def test_fetch_issue_summary_sets_truncated_after_max_pages() -> None:
    full_page = [{"number": i, "state": "open", "labels": []} for i in range(100)]
    route = respx.get("https://api.github.com/repos/octocat/huge/issues").mock(
        return_value=httpx.Response(200, json=full_page)
    )

    summary = await fetch_issue_summary("octocat/huge", label="bug")
    assert route.call_count == 3
    assert summary["total_issues"] == 300
    assert summary["filter_label"] == "bug"
    assert summary["truncated"] is True


@respx.mock
async def test_create_github_issue_posts_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "github_token", "fake_gh_token")

    route = respx.post("https://api.github.com/repos/octocat/demo/issues").mock(
        return_value=httpx.Response(
            201,
            json={
                "number": 99,
                "title": "Fix Redis TTL",
                "state": "open",
                "user": {"login": "bob"},
                "html_url": "https://github.com/octocat/demo/issues/99",
            },
        )
    )

    created = await create_github_issue(
        repo="octocat/demo",
        title="Fix Redis TTL",
        body="Add expiry on session_audit keys.",
    )
    assert route.called
    assert created["number"] == 99
    assert created["url"] == "https://github.com/octocat/demo/issues/99"


async def test_create_github_issue_requires_title_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_token", "")
    with pytest.raises(GitHubAPIError, match="GITHUB_TOKEN must be configured"):
        await create_github_issue("octocat/demo", "Valid title")

    with pytest.raises(GitHubAPIError, match="title cannot be empty"):
        await create_github_issue("octocat/demo", "   ")