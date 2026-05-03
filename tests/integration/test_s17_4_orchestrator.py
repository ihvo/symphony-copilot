"""§17.4 — Orchestrator Dispatch, Reconciliation, and Retry integration tests.

These wire the real orchestrator against the ``FakeGitHub`` HTTP server
and the ``mock_agent.py`` subprocess to exercise full dispatch cycles,
reconciliation, stall detection, retry backoff, and concurrency limits.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import pytest

from symphony.models import Issue, RunningEntry, LiveSession
from symphony.orchestrator import Orchestrator

from .conftest import FakeGitHub, agent_command, wait_until


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# §17.4 — Full poll → dispatch → agent → completion cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_dispatch_cycle(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """Orchestrator polls fake GitHub, dispatches, agent succeeds,
    continuation retry is scheduled."""
    fake_github.add_issue(1, state="open")

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
        agent_cfg={"turns": 1},
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        # Give the tick time to fire (scheduled at 0 ms) and the worker to finish
        ok = await wait_until(
            lambda: "NODE_1" in orch.state.completed,
            timeout=8.0,
        )
        assert ok, "worker did not complete in time"

        # Continuation retry should be scheduled
        ok = await wait_until(
            lambda: "NODE_1" in orch.state.retry_attempts,
            timeout=3.0,
        )
        assert ok, "continuation retry not scheduled"
        assert orch.state.retry_attempts["NODE_1"].attempt == 1
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Dispatch respects priority sort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_priority_sort(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """Higher-priority issues (lower number) are dispatched first."""
    fake_github.add_issue(1, state="open", labels=["priority/3"], created_at="2025-01-01T00:00:00Z")
    fake_github.add_issue(2, state="open", labels=["priority/1"], created_at="2025-01-02T00:00:00Z")
    fake_github.add_issue(3, state="open", labels=["priority/2"], created_at="2025-01-01T00:00:00Z")

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_concurrent=1,
        max_turns=1,
        agent_cfg={"turns": 1},
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        # Wait for first dispatch
        ok = await wait_until(lambda: len(orch.state.running) > 0, timeout=5.0)
        assert ok
        # The first dispatched issue should be #2 (priority/1 = lowest number)
        running = list(orch.state.running.values())
        assert running[0].identifier == "#2"
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Reconciliation: terminal state → stop + workspace cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_terminal_cleans_workspace(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """When a running issue becomes terminal, the worker is stopped and
    workspace is cleaned."""
    fake_github.add_issue(10, state="open")
    mock_agent_runner["hang_for"].add("NODE_10")  # keep worker alive for reconciliation

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        # Wait for dispatch
        ok = await wait_until(lambda: "NODE_10" in orch.state.running, timeout=5.0)
        assert ok

        # Transition issue to closed in the fake
        fake_github.set_state(10, "closed")

        # Trigger reconciliation tick
        await orch._reconcile()

        assert "NODE_10" not in orch.state.running

        # Workspace should be cleaned
        ws_path = os.path.join(orch.config.workspace_root, "_10")
        # Give a moment for async cleanup
        await asyncio.sleep(0.2)
        assert not os.path.exists(ws_path)
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Reconciliation: non-active → stop without cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_non_active_keeps_workspace(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """Non-active, non-terminal state stops the worker but keeps the workspace."""
    fake_github.add_issue(11, state="open")
    mock_agent_runner["hang_for"].add("NODE_11")

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        ok = await wait_until(lambda: "NODE_11" in orch.state.running, timeout=5.0)
        assert ok

        # Transition to a state that's neither active nor terminal
        fake_github.set_state(11, "in review")
        await orch._reconcile()

        assert "NODE_11" not in orch.state.running

        # Workspace preserved
        ws_path = os.path.join(orch.config.workspace_root, "_11")
        assert os.path.isdir(ws_path)
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Stall detection kills and retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stall_detection_kills_and_retries(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """A stalled worker is killed and a retry is scheduled."""
    fake_github.add_issue(20, state="open")
    mock_agent_runner["hang_for"].add("NODE_20")

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
        copilot_overrides={"stall_timeout_ms": 500},
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        ok = await wait_until(lambda: "NODE_20" in orch.state.running, timeout=5.0)
        assert ok

        # Wait for stall to be detected on next reconciliation
        await asyncio.sleep(0.8)
        await orch._reconcile()

        assert "NODE_20" not in orch.state.running
        assert "NODE_20" in orch.state.retry_attempts
        assert orch.state.retry_attempts["NODE_20"].error == "session stalled"
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Abnormal worker exit → exponential backoff retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abnormal_exit_exponential_backoff(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """Agent failure schedules a retry with attempt > 0."""
    fake_github.add_issue(30, state="open")
    mock_agent_runner["fail_for"].add("NODE_30")

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        # Wait for the failed worker to produce a retry
        ok = await wait_until(
            lambda: "NODE_30" in orch.state.retry_attempts,
            timeout=8.0,
        )
        assert ok, "retry not scheduled after failure"
        assert orch.state.retry_attempts["NODE_30"].attempt >= 1
        assert orch.state.retry_attempts["NODE_30"].error is not None
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Per-state concurrency limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_state_concurrency_limits(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """Per-state limit of 1 blocks second issue in the same state."""
    fake_github.add_issue(40, state="open")
    fake_github.add_issue(41, state="open")
    mock_agent_runner["hang_for"].update({"NODE_40", "NODE_41"})  # keep alive

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_concurrent=5,
        max_turns=1,
        agent_cfg={"turns": 1, "slow_turn_ms": 3000},
        extra_yaml="agent:\n  max_concurrent_agents_by_state:\n    open: 1\n",
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        await asyncio.sleep(1.5)
        # Only 1 open issue should be running
        assert len(orch.state.running) == 1
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Startup terminal workspace cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_terminal_workspace_cleanup(fake_github, make_workflow, tmp_path):
    """At startup, workspaces for terminal issues are removed."""
    fake_github.add_issue(50, state="closed")

    wf = make_workflow(endpoint=fake_github.base_url)
    # Pre-create a workspace directory for the terminal issue
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()
    stale_ws = ws_root / "_50"
    stale_ws.mkdir()
    (stale_ws / "leftover.txt").write_text("old data")

    orch = Orchestrator(wf)
    await orch.start()

    try:
        # Give cleanup a moment
        await asyncio.sleep(0.5)
        assert not stale_ws.exists(), "terminal workspace was not cleaned up"
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.4 — Snapshot API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_reflects_running_and_retry(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """Snapshot shows running sessions and retry queue."""
    fake_github.add_issue(60, state="open")
    mock_agent_runner["hang_for"].add("NODE_60")

    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
    )
    orch = Orchestrator(wf)
    await orch.start()

    try:
        ok = await wait_until(lambda: len(orch.state.running) > 0, timeout=5.0)
        assert ok

        snap = orch.get_snapshot()
        assert snap["counts"]["running"] >= 1
        assert "copilot_totals" in snap
        assert isinstance(snap["running"], list)
        assert snap["running"][0]["issue_identifier"] == "#60"
    finally:
        await orch.stop()
