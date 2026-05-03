"""Tests for domain models."""

from __future__ import annotations

from datetime import datetime, timezone

from symphony.models import BlockerRef, Issue, OrchestratorState, WorkerResult


class TestIssue:
    def test_to_template_dict(self):
        issue = Issue(
            id="abc",
            identifier="#1",
            title="Test",
            description="desc",
            priority=2,
            state="open",
            labels=["bug", "p1"],
            blocked_by=[BlockerRef(id="x", identifier="#2", state="open")],
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        d = issue.to_template_dict()
        assert d["id"] == "abc"
        assert d["identifier"] == "#1"
        assert d["labels"] == ["bug", "p1"]
        assert len(d["blocked_by"]) == 1
        assert d["blocked_by"][0]["identifier"] == "#2"
        assert "2025" in d["created_at"]

    def test_missing_optional_fields(self):
        issue = Issue(id="a", identifier="#1", title="t")
        d = issue.to_template_dict()
        assert d["description"] == ""
        assert d["priority"] is None
        assert d["labels"] == []
        assert d["blocked_by"] == []


class TestOrchestratorState:
    def test_defaults(self):
        state = OrchestratorState()
        assert state.poll_interval_ms == 30000
        assert state.max_concurrent_agents == 10
        assert len(state.running) == 0
        assert len(state.claimed) == 0
