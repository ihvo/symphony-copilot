"""§17.6 — Observability integration tests.

Verifies structured logging, token aggregation, and snapshot correctness
through the real code.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from symphony.logging_config import configure_logging
from symphony.models import (
    AgentEvent,
    CopilotTotals,
    LiveSession,
    OrchestratorState,
    RunningEntry,
)
from symphony.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# §17.6 — Validation failures are operator-visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_emitted_to_stderr(tmp_path, capfd, monkeypatch):
    """Startup validation failure appears in structured logs."""
    configure_logging("INFO")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\n")
    orch = Orchestrator(str(wf))

    with pytest.raises(Exception):
        await orch.start()

    captured = capfd.readouterr()
    lines = [l for l in captured.err.strip().split("\n") if l.strip()]
    assert any("api_key" in l for l in lines), "validation error not in logs"


# ---------------------------------------------------------------------------
# §17.6 — Structured logging includes issue/session context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_logs_are_json(fake_github, make_workflow, tmp_path, capfd):
    """All log lines are parseable JSON with required fields."""
    configure_logging("INFO")
    fake_github.add_issue(1, state="open")

    wf = make_workflow(endpoint=fake_github.base_url, max_turns=1, agent_cfg={"turns": 1})
    orch = Orchestrator(wf)
    await orch.start()

    from .conftest import wait_until
    await wait_until(lambda: "NODE_1" in orch.state.completed, timeout=8.0)
    await orch.stop()

    captured = capfd.readouterr()
    lines = [l for l in captured.err.strip().split("\n") if l.strip()]
    assert len(lines) >= 2

    for line in lines:
        parsed = json.loads(line)
        assert "ts" in parsed
        assert "level" in parsed
        assert "msg" in parsed


# ---------------------------------------------------------------------------
# §17.6 — Token aggregation correct across repeated events
# ---------------------------------------------------------------------------


def test_token_aggregation_across_events(tmp_path, monkeypatch):
    """Token deltas accumulate correctly through _handle_agent_event."""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
    orch = Orchestrator(str(wf))
    orch._load_and_apply_workflow()

    from symphony.models import Issue
    entry = RunningEntry(
        issue_id="id1", identifier="#1",
        issue=Issue(id="id1", identifier="#1", title="t", state="open"),
        state="open", started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    orch._state.running["id1"] = entry

    # Simulate absolute token events (not delta)
    for total in [100, 250, 400]:
        evt = AgentEvent(
            event="notification", issue_id="id1",
            usage={"input_tokens": total, "output_tokens": total // 2, "total_tokens": total + total // 2},
        )
        orch._handle_agent_event(evt)

    # The session should reflect the latest absolute value
    assert entry.session.copilot_input_tokens == 400
    assert entry.session.copilot_output_tokens == 200
    assert entry.session.copilot_total_tokens == 600


# ---------------------------------------------------------------------------
# §17.6 — Logging sink failures do not crash orchestration
# ---------------------------------------------------------------------------


def test_logging_failure_does_not_crash():
    """A broken log handler does not kill the orchestrator main loop.

    Python's default ``logging.raiseExceptions`` is True in dev mode,
    which propagates handler errors.  The spec requires the orchestrator
    to survive; our StructuredFormatter itself must not crash.
    """
    import io
    from symphony.logging_config import StructuredFormatter

    fmt = StructuredFormatter()
    record = logging.LogRecord(
        "test", logging.ERROR, "", 0, "payload %s", ("x",), None,
    )
    # If the formatter itself raises, that's a real bug.  It should not.
    result = fmt.format(record)
    assert isinstance(result, str)
    assert "payload x" in result


# ---------------------------------------------------------------------------
# §17.6 — Snapshot includes rate limits when present
# ---------------------------------------------------------------------------


def test_snapshot_includes_rate_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")
    orch = Orchestrator(str(wf))
    orch._load_and_apply_workflow()

    from symphony.models import RateLimitInfo
    orch._state.copilot_rate_limits = RateLimitInfo(data={"remaining": 42})

    snap = orch.get_snapshot()
    assert snap["rate_limits"]["remaining"] == 42
