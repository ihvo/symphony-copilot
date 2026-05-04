"""Tests for tracker client (SPEC §11, §17.3)."""

from __future__ import annotations

import pytest

from symphony.errors import (
    MissingTrackerApiKeyError,
    MissingTrackerRepoError,
)
from symphony.tracker import GitHubTrackerClient, _normalize_issue


class TestNormalizeIssue:
    def test_basic_fields(self):
        node = {
            "id": 123,
            "node_id": "MDU6SXNzdWUx",
            "number": 42,
            "title": "Fix bug",
            "body": "A description",
            "state": "Open",
            "html_url": "https://github.com/o/r/issues/42",
            "labels": [{"name": "Bug"}, {"name": "P1"}],
            "created_at": "2025-01-15T10:00:00Z",
            "updated_at": "2025-01-16T12:00:00Z",
        }
        issue = _normalize_issue(node)
        assert issue.id == "MDU6SXNzdWUx"
        assert issue.identifier == "#42"
        assert issue.title == "Fix bug"
        assert issue.state == "open"
        assert issue.labels == ["bug", "p1"]
        assert issue.created_at is not None
        assert issue.url == "https://github.com/o/r/issues/42"

    def test_labels_normalized_lowercase(self):
        node = {
            "id": 1,
            "number": 1,
            "title": "t",
            "state": "open",
            "labels": [{"name": "BUG"}, {"name": "Feature"}],
        }
        issue = _normalize_issue(node)
        assert issue.labels == ["bug", "feature"]

    def test_priority_from_label(self):
        node = {
            "id": 1,
            "number": 1,
            "title": "t",
            "state": "open",
            "labels": [{"name": "priority/2"}],
        }
        issue = _normalize_issue(node)
        assert issue.priority == 2

    def test_missing_optional_fields(self):
        node = {"id": 1, "number": 1, "title": "t", "state": "open"}
        issue = _normalize_issue(node)
        assert issue.description is None
        assert issue.priority is None
        assert issue.labels == []
        assert issue.blocked_by == []

    def test_non_integer_priority(self):
        node = {"id": 1, "number": 1, "title": "t", "state": "open", "priority": "high"}
        issue = _normalize_issue(node)
        assert issue.priority is None

    def test_empty_labels(self):
        node = {"id": 1, "number": 1, "title": "t", "state": "open", "labels": []}
        issue = _normalize_issue(node)
        assert issue.labels == []

    def test_string_labels(self):
        node = {"id": 1, "number": 1, "title": "t", "state": "open", "labels": ["Bug", "Feature"]}
        issue = _normalize_issue(node)
        assert issue.labels == ["bug", "feature"]


class TestClientInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(MissingTrackerApiKeyError):
            GitHubTrackerClient("https://api.github.com", "", "o/r")

    def test_missing_repo_raises(self):
        with pytest.raises(MissingTrackerRepoError):
            GitHubTrackerClient("https://api.github.com", "tok", "")

    def test_valid_init(self):
        client = GitHubTrackerClient("https://api.github.com", "tok", "o/r")
        assert client._repo == "o/r"

    @pytest.mark.asyncio
    async def test_empty_states_fetch_returns_empty(self):
        """fetch_issues_by_states([]) should return empty without API call."""
        client = GitHubTrackerClient("https://api.github.com", "tok", "o/r")
        result = await client.fetch_issues_by_states([])
        assert result == []


class TestUpdateConfig:
    def test_updates_fields(self):
        client = GitHubTrackerClient("https://api.github.com", "tok", "o/r")
        client.update_config(
            "https://custom.api.com",
            "new_tok",
            "new/repo",
            ["open", "in progress"],
            ["done"],
        )
        assert client._endpoint == "https://custom.api.com"
        assert client._repo == "new/repo"
        assert client._active_states == ["open", "in progress"]
        assert client._terminal_states == ["done"]
