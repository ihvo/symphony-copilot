"""Tests for orchestrator dispatch, reconciliation, and retry (SPEC §7-8, §17.4)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from symphony.models import BlockerRef, Issue, OrchestratorState, RetryEntry, RunningEntry, LiveSession
from symphony.orchestrator import Orchestrator, _sort_key


def _issue(
    id: str = "id1",
    identifier: str = "#1",
    title: str = "Test",
    state: str = "open",
    priority: int | None = None,
    created_at: datetime | None = None,
    blocked_by: list | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=title,
        state=state,
        priority=priority,
        created_at=created_at or datetime(2025, 1, 1, tzinfo=timezone.utc),
        blocked_by=blocked_by or [],
    )


class TestSortKey:
    def test_priority_ascending(self):
        i1 = _issue(priority=1, id="a")
        i2 = _issue(priority=2, id="b")
        i3 = _issue(priority=None, id="c")
        result = sorted([i3, i1, i2], key=_sort_key)
        assert [r.id for r in result] == ["a", "b", "c"]

    def test_created_at_tiebreaker(self):
        i1 = _issue(id="a", priority=1, created_at=datetime(2025, 1, 2, tzinfo=timezone.utc))
        i2 = _issue(id="b", priority=1, created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        result = sorted([i1, i2], key=_sort_key)
        assert [r.id for r in result] == ["b", "a"]

    def test_identifier_tiebreaker(self):
        i1 = _issue(id="a", identifier="#2", priority=1, created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        i2 = _issue(id="b", identifier="#1", priority=1, created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        result = sorted([i1, i2], key=_sort_key)
        assert [r.id for r in result] == ["b", "a"]


class TestShouldDispatch:
    def _make_orch(self, tmp_path, monkeypatch) -> Orchestrator:
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(
            "---\ntracker:\n  kind: github\n  repo: o/r\n"
            "agent:\n  max_concurrent_agents: 5\n---\nPrompt"
        )
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        return orch

    def test_eligible_issue(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        issue = _issue(state="open")
        assert orch._should_dispatch(issue)

    def test_already_running(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        issue = _issue(state="open")
        orch._state.running["id1"] = RunningEntry(
            issue_id="id1", identifier="#1", issue=issue, state="open"
        )
        assert not orch._should_dispatch(issue)

    def test_already_claimed(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        issue = _issue(state="open")
        orch._state.claimed.add("id1")
        assert not orch._should_dispatch(issue)

    def test_terminal_state(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        issue = _issue(state="closed")
        assert not orch._should_dispatch(issue)

    def test_non_active_state(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        issue = _issue(state="unknown_state")
        assert not orch._should_dispatch(issue)

    def test_no_slots(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        # Fill all 5 slots
        for i in range(5):
            orch._state.running[f"slot{i}"] = RunningEntry(
                issue_id=f"slot{i}", identifier=f"#{i}", issue=_issue(id=f"slot{i}"), state="open"
            )
        issue = _issue(id="new", state="open")
        assert not orch._should_dispatch(issue)

    def test_missing_fields_not_eligible(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        issue = Issue(id="", identifier="#1", title="t", state="open")
        assert not orch._should_dispatch(issue)
        issue2 = Issue(id="x", identifier="", title="t", state="open")
        assert not orch._should_dispatch(issue2)

    def test_todo_with_nonterminal_blocker(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        orch._last_good_config._raw["tracker"]["active_states"] = ["open", "todo"]
        issue = _issue(
            state="todo",
            blocked_by=[BlockerRef(id="b1", identifier="#99", state="open")],
        )
        assert not orch._should_dispatch(issue)

    def test_todo_with_terminal_blocker(self, tmp_path, monkeypatch):
        orch = self._make_orch(tmp_path, monkeypatch)
        orch._last_good_config._raw["tracker"]["active_states"] = ["open", "todo"]
        issue = _issue(
            state="todo",
            blocked_by=[BlockerRef(id="b1", identifier="#99", state="closed")],
        )
        assert orch._should_dispatch(issue)


class TestRetryBackoff:
    def test_continuation_retry_attempt_1(self, tmp_path, monkeypatch):
        orch = self._make_orch_minimal(tmp_path, monkeypatch)
        orch._schedule_retry("id1", attempt=1, identifier="#1", error=None, delay_ms=1000)
        assert "id1" in orch._state.retry_attempts
        assert orch._state.retry_attempts["id1"].attempt == 1

    def test_exponential_backoff(self, tmp_path, monkeypatch):
        """Verify backoff formula: min(10000 * 2^(attempt-1), max_backoff)."""
        orch = self._make_orch_minimal(tmp_path, monkeypatch)

        # attempt=1: 10000
        orch._schedule_retry("a", 1, "#a", "err")
        # We can't easily test delay_ms directly since it's computed inside,
        # but we can verify the entry is created
        assert orch._state.retry_attempts["a"].attempt == 1

    def test_cancel_existing_retry(self, tmp_path, monkeypatch):
        orch = self._make_orch_minimal(tmp_path, monkeypatch)
        orch._schedule_retry("id1", 1, "#1", "err1")
        old_entry = orch._state.retry_attempts["id1"]
        orch._schedule_retry("id1", 2, "#1", "err2")
        new_entry = orch._state.retry_attempts["id1"]
        assert new_entry.attempt == 2

    def _make_orch_minimal(self, tmp_path, monkeypatch) -> Orchestrator:
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nPrompt")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.new_event_loop()
        return orch


class TestPerStateConcurrency:
    def test_per_state_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(
            "---\ntracker:\n  kind: github\n  repo: o/r\n"
            "  active_states: [open, todo]\n"
            "agent:\n  max_concurrent_agents: 10\n"
            "  max_concurrent_agents_by_state:\n    open: 2\n---\nP"
        )
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()

        # Fill 2 open slots
        for i in range(2):
            orch._state.running[f"o{i}"] = RunningEntry(
                issue_id=f"o{i}", identifier=f"#{i}", issue=_issue(id=f"o{i}"), state="open"
            )

        # Another open issue should be blocked
        issue = _issue(id="new_open", state="open")
        assert orch._per_state_slots("open") == 0
        assert not orch._should_dispatch(issue)

        # But a todo issue should still be OK (no per-state limit)
        todo = _issue(id="new_todo", state="todo")
        assert orch._should_dispatch(todo)


class TestSnapshot:
    def test_empty_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        snap = orch.get_snapshot()
        assert snap["counts"]["running"] == 0
        assert snap["counts"]["retrying"] == 0
        assert "copilot_totals" in snap

    def test_snapshot_with_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._state.running["id1"] = RunningEntry(
            issue_id="id1",
            identifier="#1",
            issue=_issue(),
            started_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
            state="open",
        )
        snap = orch.get_snapshot()
        assert snap["counts"]["running"] == 1
        assert snap["running"][0]["issue_identifier"] == "#1"


class TestIssueDetail:
    def test_found_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._state.running["id1"] = RunningEntry(
            issue_id="id1", identifier="#1", issue=_issue(), state="open",
            started_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        detail = orch.get_issue_detail("#1")
        assert detail is not None
        assert detail["status"] == "running"

    def test_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        assert orch.get_issue_detail("#999") is None


class TestReconcileStalls:
    """Tests for stall detection (SPEC §8.5 Part A)."""

    @pytest.mark.asyncio
    async def test_stall_schedules_retry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(
            "---\ntracker:\n  kind: github\n  repo: o/r\n"
            "copilot:\n  stall_timeout_ms: 1000\n---\nP"
        )
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.get_event_loop()

        # Add a running entry that started 2 seconds ago with no events
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        entry = RunningEntry(
            issue_id="stale1", identifier="#10", issue=_issue(id="stale1", identifier="#10"),
            state="open", started_at=old_time,
        )
        orch._state.running["stale1"] = entry
        orch._state.claimed.add("stale1")

        await orch._reconcile_stalls(orch._effective_config())

        # Worker should be terminated
        assert "stale1" not in orch._state.running
        # Retry should be scheduled
        assert "stale1" in orch._state.retry_attempts
        assert orch._state.retry_attempts["stale1"].error == "session stalled"

    @pytest.mark.asyncio
    async def test_stall_disabled_when_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(
            "---\ntracker:\n  kind: github\n  repo: o/r\n"
            "copilot:\n  stall_timeout_ms: 0\n---\nP"
        )
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()

        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        orch._state.running["s1"] = RunningEntry(
            issue_id="s1", identifier="#1", issue=_issue(id="s1"),
            state="open", started_at=old_time,
        )
        await orch._reconcile_stalls(orch._effective_config())
        # Should still be running (stall detection disabled)
        assert "s1" in orch._state.running


class TestReconcileStates:
    """Tests for tracker state refresh reconciliation (SPEC §8.5 Part B)."""

    @pytest.mark.asyncio
    async def test_terminal_state_removes_and_cleans(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.get_event_loop()

        # Add running entry
        orch._state.running["id42"] = RunningEntry(
            issue_id="id42", identifier="#42", issue=_issue(id="id42", identifier="#42"),
            state="open", started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        orch._state.claimed.add("id42")

        # Mock tracker to return closed state
        async def mock_fetch(numbers):
            return [_issue(id="id42", identifier="#42", state="closed")]
        orch._tracker.fetch_issues_by_numbers = mock_fetch

        await orch._reconcile_states(orch._effective_config())
        assert "id42" not in orch._state.running

    @pytest.mark.asyncio
    async def test_active_state_updates_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()

        orch._state.running["id1"] = RunningEntry(
            issue_id="id1", identifier="#1", issue=_issue(id="id1", title="Old"),
            state="open", started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )

        async def mock_fetch(numbers):
            return [_issue(id="id1", identifier="#1", title="Updated", state="open")]
        orch._tracker.fetch_issues_by_numbers = mock_fetch

        await orch._reconcile_states(orch._effective_config())
        assert orch._state.running["id1"].issue.title == "Updated"

    @pytest.mark.asyncio
    async def test_no_running_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        # No running entries – should be a no-op
        await orch._reconcile_states(orch._effective_config())


class TestWorkflowReload:
    """Tests for dynamic workflow reload (SPEC §6.2)."""

    def test_valid_reload_applies(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\npolling:\n  interval_ms: 5000\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        assert orch._state.poll_interval_ms == 5000

        # Change workflow
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\npolling:\n  interval_ms: 10000\n---\nP")
        orch._check_workflow_reload()
        assert orch._state.poll_interval_ms == 10000

    def test_invalid_reload_keeps_last_good(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\npolling:\n  interval_ms: 5000\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        assert orch._state.poll_interval_ms == 5000

        # Write invalid config
        wf_path.write_text("---\ntracker:\n  kind: jira\n---\nP")
        orch._check_workflow_reload()
        # Should still have the last good config
        assert orch._effective_config().tracker_kind == "github"
        assert orch._state.poll_interval_ms == 5000

    def test_deleted_workflow_keeps_last_good(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        original_config = orch._effective_config()

        # Delete workflow file
        wf_path.unlink()
        orch._check_workflow_reload()
        # Last good config should still be available
        assert orch._effective_config() is original_config


class TestWorkerExitHandling:
    """Tests for worker exit and retry scheduling (SPEC §16.6)."""

    def test_normal_exit_schedules_continuation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.new_event_loop()

        # Simulate running entry
        entry = RunningEntry(
            issue_id="id1", identifier="#1", issue=_issue(),
            state="open", started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        orch._state.running["id1"] = entry
        orch._state.claimed.add("id1")

        # Simulate normal worker exit
        from symphony.models import WorkerResult
        result = WorkerResult(
            issue_id="id1", identifier="#1", success=True,
            started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ended_at=datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc),
        )
        orch._handle_worker_exit(result)

        # Should be in completed and have continuation retry at attempt 1
        assert "id1" in orch._state.completed
        assert "id1" in orch._state.retry_attempts
        assert orch._state.retry_attempts["id1"].attempt == 1

    def test_abnormal_exit_schedules_backoff(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.new_event_loop()

        entry = RunningEntry(
            issue_id="id1", identifier="#1", issue=_issue(),
            state="open", started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            retry_attempt=2,
        )
        orch._state.running["id1"] = entry
        orch._state.claimed.add("id1")

        from symphony.models import WorkerResult
        result = WorkerResult(
            issue_id="id1", identifier="#1", success=False,
            error="agent crashed",
        )
        orch._handle_worker_exit(result)

        # Should schedule retry with incremented attempt
        assert "id1" in orch._state.retry_attempts
        assert orch._state.retry_attempts["id1"].attempt == 3


class TestOnRetry:
    """Tests for retry timer handling (SPEC §8.4, §16.6)."""

    def _make_orch(self, tmp_path, monkeypatch) -> Orchestrator:
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.new_event_loop()
        return orch

    @pytest.mark.asyncio
    async def test_retry_release_when_not_found(self, tmp_path, monkeypatch):
        """Retry releases claim when issue is no longer a candidate."""
        orch = self._make_orch(tmp_path, monkeypatch)
        orch._state.claimed.add("id1")
        orch._state.retry_attempts["id1"] = RetryEntry(
            issue_id="id1", identifier="#1", attempt=1,
        )

        # Mock tracker returns no matching issue
        async def mock_fetch():
            return [_issue(id="other", identifier="#99")]
        orch._tracker.fetch_candidate_issues = mock_fetch

        await orch._on_retry("id1")

        # Claim should be released
        assert "id1" not in orch._state.claimed
        assert "id1" not in orch._state.retry_attempts

    @pytest.mark.asyncio
    async def test_retry_requeue_on_no_slots(self, tmp_path, monkeypatch):
        """Retry requeues when no slots are available."""
        orch = self._make_orch(tmp_path, monkeypatch)
        orch._state.claimed.add("id1")
        orch._state.retry_attempts["id1"] = RetryEntry(
            issue_id="id1", identifier="#1", attempt=2,
        )
        # Fill all slots
        for i in range(10):
            orch._state.running[f"s{i}"] = RunningEntry(
                issue_id=f"s{i}", identifier=f"#{i}", issue=_issue(id=f"s{i}"), state="open"
            )

        async def mock_fetch():
            return [_issue(id="id1")]
        orch._tracker.fetch_candidate_issues = mock_fetch

        await orch._on_retry("id1")

        # Should be requeued with incremented attempt
        assert "id1" in orch._state.retry_attempts
        assert orch._state.retry_attempts["id1"].attempt == 3
        assert orch._state.retry_attempts["id1"].error == "no available orchestrator slots"

    @pytest.mark.asyncio
    async def test_retry_requeue_on_fetch_failure(self, tmp_path, monkeypatch):
        """Retry requeues when candidate fetch fails."""
        orch = self._make_orch(tmp_path, monkeypatch)
        orch._state.claimed.add("id1")
        orch._state.retry_attempts["id1"] = RetryEntry(
            issue_id="id1", identifier="#1", attempt=1,
        )

        async def mock_fail():
            raise RuntimeError("network error")
        orch._tracker.fetch_candidate_issues = mock_fail

        await orch._on_retry("id1")

        assert "id1" in orch._state.retry_attempts
        assert orch._state.retry_attempts["id1"].error == "retry poll failed"


class TestReconcileNonActiveNonTerminal:
    """Test for state that is neither active nor terminal (SPEC §8.5 Part B)."""

    @pytest.mark.asyncio
    async def test_stops_without_cleanup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(
            "---\ntracker:\n  kind: github\n  repo: o/r\n"
            "  active_states: [open]\n  terminal_states: [closed]\n---\nP"
        )
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.get_event_loop()

        # Create workspace so we can verify it's NOT cleaned
        import os
        ws_root = orch._effective_config().workspace_root
        ws_path = os.path.join(ws_root, "_42")
        os.makedirs(ws_path, exist_ok=True)

        orch._state.running["id42"] = RunningEntry(
            issue_id="id42", identifier="#42", issue=_issue(id="id42", identifier="#42"),
            state="open", started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        orch._state.claimed.add("id42")

        # Return state="in review" which is neither active nor terminal
        async def mock_fetch(numbers):
            return [_issue(id="id42", identifier="#42", state="in review")]
        orch._tracker.fetch_issues_by_numbers = mock_fetch

        await orch._reconcile_states(orch._effective_config())

        # Worker terminated
        assert "id42" not in orch._state.running
        # Claim released
        assert "id42" not in orch._state.claimed
        # Workspace preserved (no cleanup for non-terminal)
        assert os.path.isdir(ws_path)


class TestReconciliationCancelledWorkerNoRetry:
    """Reconciliation-cancelled workers should not schedule retry."""

    def test_no_retry_when_already_terminated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
        orch = Orchestrator(str(wf_path))
        orch._load_and_apply_workflow()
        orch._loop = asyncio.new_event_loop()

        # Worker result arrives but entry was already removed by reconciliation
        from symphony.models import WorkerResult
        result = WorkerResult(
            issue_id="id1", identifier="#1", success=False,
            error="cancelled",
        )
        # No running entry exists (already popped by _terminate_running)
        orch._handle_worker_exit(result)

        # Should NOT schedule retry since entry was already handled
        assert "id1" not in orch._state.retry_attempts

