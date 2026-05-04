"""§17.3 — Issue Tracker Client integration tests.

Wires the real ``GitHubTrackerClient`` against the ``FakeGitHub`` HTTP
server and verifies candidate fetch, pagination, state refresh, label
normalization, and error mapping.
"""

from __future__ import annotations

import pytest

from symphony.errors import (
    GitHubApiRequestError,
    GitHubApiStatusError,
    GitHubUnknownPayloadError,
)
from symphony.tracker import GitHubTrackerClient

# ---------------------------------------------------------------------------
# §17.3 — Candidate issue fetch uses active states and repository
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_fetch_uses_active_states_and_repo(fake_github):
    """Tracker queries the correct repo and state from config."""
    fake_github.add_issue(1, state="open")
    fake_github.add_issue(2, state="closed")

    client = GitHubTrackerClient(
        fake_github.base_url,
        "tok",
        "test/repo",
        active_states=["open"],
        terminal_states=["closed"],
    )
    try:
        issues = await client.fetch_candidate_issues()
    finally:
        await client.close()

    assert len(issues) == 1
    assert issues[0].identifier == "#1"

    # Verify the URL included repo and state
    assert any(
        "/repos/test/repo/issues" in path and params.get("state") == "open"
        for _, path, params in fake_github.request_log
    )


@pytest.mark.asyncio
async def test_github_query_uses_repo_filter(fake_github):
    """URL path includes the configured repository."""
    fake_github.add_issue(10, state="open")

    client = GitHubTrackerClient(
        fake_github.base_url,
        "tok",
        "myorg/myrepo",
    )
    try:
        await client.fetch_candidate_issues()
    finally:
        await client.close()

    assert any("/repos/myorg/myrepo/issues" in path for _, path, _ in fake_github.request_log)


# ---------------------------------------------------------------------------
# §17.3 — Pagination preserves order across multiple pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_preserves_order(fake_github):
    """Client fetches all pages and maintains insertion order."""
    # The fake server paginates via per_page/page.  We add >50 issues so
    # two pages are needed (default per_page=50).
    for i in range(1, 55):
        fake_github.add_issue(i, state="open", created_at=f"2025-01-{i:02d}T00:00:00Z")

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        issues = await client.fetch_candidate_issues()
    finally:
        await client.close()

    assert len(issues) == 54
    numbers = [int(i.identifier.lstrip("#")) for i in issues]
    assert numbers == list(range(1, 55)), "pagination must preserve order"

    # Verify both pages were requested
    pages_requested = [
        int(params.get("page", "1"))
        for _, _, params in fake_github.request_log
        if "page" in params
    ]
    assert 1 in pages_requested
    assert 2 in pages_requested


# ---------------------------------------------------------------------------
# §17.3 — PRs are filtered out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_requests_filtered_out(fake_github):
    """GitHub returns PRs in the issues endpoint; they must be skipped."""
    fake_github.add_issue(1, state="open")
    fake_github.add_issue(2, state="open", is_pr=True)
    fake_github.add_issue(3, state="open")

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        issues = await client.fetch_candidate_issues()
    finally:
        await client.close()

    identifiers = {i.identifier for i in issues}
    assert identifiers == {"#1", "#3"}


# ---------------------------------------------------------------------------
# §17.3 — Labels normalized to lowercase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_labels_normalized_lowercase(fake_github):
    fake_github.add_issue(1, state="open", labels=["Bug", "PRIORITY/1", "Feature"])

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        issues = await client.fetch_candidate_issues()
    finally:
        await client.close()

    assert issues[0].labels == ["bug", "priority/1", "feature"]


# ---------------------------------------------------------------------------
# §17.3 — Issue state refresh by number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_state_refresh_by_number(fake_github):
    """fetch_issues_by_numbers returns normalized issues for given numbers."""
    fake_github.add_issue(42, state="open", title="Hello")
    fake_github.add_issue(43, state="closed", title="World")

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        issues = await client.fetch_issues_by_numbers([42, 43])
    finally:
        await client.close()

    by_ident = {i.identifier: i for i in issues}
    assert by_ident["#42"].state == "open"
    assert by_ident["#43"].state == "closed"


@pytest.mark.asyncio
async def test_fetch_issue_states_by_ids_delegates_to_numbers(fake_github):
    """fetch_issue_states_by_ids with id_to_number mapping works."""
    fake_github.add_issue(7, state="open")

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        issues = await client.fetch_issue_states_by_ids(
            ["NODE_7"],
            id_to_number={"NODE_7": 7},
        )
    finally:
        await client.close()

    assert len(issues) == 1
    assert issues[0].identifier == "#7"


# ---------------------------------------------------------------------------
# §17.3 — Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_non_200_raises_status_error(fake_github):
    """Non-200 HTTP → GitHubApiStatusError."""
    fake_github.inject_error("list", 403, '{"message":"Forbidden"}')

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        with pytest.raises(GitHubApiStatusError) as exc_info:
            await client.fetch_candidate_issues()
        assert exc_info.value.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_error_malformed_json(fake_github):
    """Non-JSON response body → GitHubUnknownPayloadError."""
    fake_github.inject_error("list", 200, "this is not json")

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        with pytest.raises(GitHubUnknownPayloadError):
            await client.fetch_candidate_issues()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_error_connection_refused():
    """Connection refused → GitHubApiRequestError."""
    client = GitHubTrackerClient(
        "http://127.0.0.1:1",
        "tok",
        "test/repo",  # nothing on port 1
    )
    try:
        with pytest.raises(GitHubApiRequestError):
            await client.fetch_candidate_issues()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_error_single_issue_404(fake_github):
    """Fetching a nonexistent issue number → GitHubApiStatusError(404)."""
    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        with pytest.raises(GitHubApiStatusError) as exc_info:
            await client.fetch_issue_by_number(999)
        assert exc_info.value.status == 404
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# §17.3 — Terminal-state fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_issues_by_states(fake_github):
    """fetch_issues_by_states returns issues in the requested state."""
    fake_github.add_issue(1, state="open")
    fake_github.add_issue(2, state="closed")
    fake_github.add_issue(3, state="closed")

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        closed = await client.fetch_issues_by_states(["closed"])
    finally:
        await client.close()

    assert len(closed) == 2
    assert all(i.state == "closed" for i in closed)


@pytest.mark.asyncio
async def test_fetch_issues_by_states_empty_list(fake_github):
    """Empty state list returns [] without making an API call."""
    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        result = await client.fetch_issues_by_states([])
    finally:
        await client.close()

    assert result == []
    assert len(fake_github.request_log) == 0


# ---------------------------------------------------------------------------
# §17.3 — Dynamic config update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_config_changes_repo(fake_github):
    """update_config changes the repository used in subsequent calls."""
    fake_github.add_issue(1, state="open")

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        client.update_config(
            fake_github.base_url,
            "tok",
            "other/repo",
            active_states=["open"],
            terminal_states=["closed"],
        )
        await client.fetch_candidate_issues()
    finally:
        await client.close()

    assert any("/repos/other/repo/issues" in path for _, path, _ in fake_github.request_log)
