"""§17.5 — Coding-Agent App-Server Client integration tests.

Runs the real ``CopilotSession`` / ``run_agent_session`` against the
``mock_agent.py`` subprocess and verifies session lifecycle, timeouts,
event extraction, approval handling, and tool-call rejection.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from symphony.config import ServiceConfig
from symphony.errors import (
    InvalidWorkspaceCwdError,
    PortExitError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, WorkflowDefinition
from symphony.runner import CopilotSession, run_agent_session

from .conftest import agent_command


def _cfg(tmp_path, agent_cfg: dict | None = None, **overrides) -> ServiceConfig:
    raw: dict = {
        "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
        "copilot": {
            "command": agent_command(agent_cfg or {"turns": 1}),
            "turn_timeout_ms": 10000,
            "read_timeout_ms": 3000,
            "stall_timeout_ms": 60000,
            **overrides,
        },
    }
    return ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))


def _ws(tmp_path, name: str = "ws") -> str:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _issue(id: str = "id1") -> Issue:
    return Issue(id=id, identifier="#1", title="Test issue", state="open")


# ---------------------------------------------------------------------------
# §17.5 — Launch uses workspace cwd with bash -lc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_creates_session_in_workspace(tmp_path):
    """Agent subprocess is started in the workspace directory."""
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    assert session.session.copilot_pid is not None
    await session.stop()


@pytest.mark.asyncio
async def test_invalid_workspace_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(InvalidWorkspaceCwdError):
        s = CopilotSession(cfg, str(tmp_path / "nope"), _issue())
        await s.start()


# ---------------------------------------------------------------------------
# §17.5 — Thread and turn identities extracted → session_started
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_turn_identities(tmp_path):
    ws = _ws(tmp_path)
    events: list[AgentEvent] = []
    cfg = _cfg(tmp_path)
    session = CopilotSession(cfg, ws, _issue(), on_event=events.append)

    await session.start()
    assert session.session.thread_id == "mock-thread-1"
    assert any(e.event == "session_started" for e in events)

    await session.run_turn("prompt")
    assert session.session.session_id == "mock-thread-1-mock-turn-1"
    assert any(e.event == "turn_completed" for e in events)

    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Multi-turn on same thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_turn_reuses_thread(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"turns": 3})
    session = CopilotSession(cfg, ws, _issue())
    await session.start()

    for i in range(1, 4):
        await session.run_turn(f"prompt {i}", turn_number=i)

    assert session.session.turn_count == 3
    assert session.session.thread_id == "mock-thread-1"  # same thread
    assert session.session.turn_id == "mock-turn-3"

    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Read timeout enforced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_timeout_enforced(tmp_path):
    """A slow agent init triggers ResponseTimeoutError."""
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"slow_init_ms": 5000}, read_timeout_ms=500)
    session = CopilotSession(cfg, ws, _issue())
    with pytest.raises(Exception):  # ResponseTimeoutError or PortExitError
        await session.start()
    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Turn timeout enforced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_timeout_enforced(tmp_path):
    """A hanging turn triggers TurnTimeoutError."""
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"behavior": "hang"}, turn_timeout_ms=500)
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    with pytest.raises(TurnTimeoutError):
        await session.run_turn("prompt")
    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Stderr separate from protocol stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stderr_does_not_interfere(tmp_path):
    """Noisy stderr from the agent does not break the protocol."""
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"turns": 1, "stderr_noise": True})
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    await session.run_turn("prompt")
    await session.stop()
    assert session.session.turn_count == 1


# ---------------------------------------------------------------------------
# §17.5 — Approvals auto-approved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_auto_approved(tmp_path):
    """Approval requests are auto-approved without stalling."""
    ws = _ws(tmp_path)
    events: list[AgentEvent] = []
    cfg = _cfg(tmp_path, agent_cfg={"turns": 1, "approval_turn": 0})
    session = CopilotSession(cfg, ws, _issue(), on_event=events.append)
    await session.start()
    await session.run_turn("prompt")
    await session.stop()

    assert any(e.event == "approval_auto_approved" for e in events)
    assert any(e.event == "turn_completed" for e in events)


# ---------------------------------------------------------------------------
# §17.5 — Unsupported tool calls rejected without stalling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_tool_call_rejected(tmp_path):
    """Unsupported tool calls return failure; session continues."""
    ws = _ws(tmp_path)
    events: list[AgentEvent] = []
    cfg = _cfg(tmp_path, agent_cfg={
        "turns": 1, "tool_call_turn": 0, "tool_name": "bad_tool",
    })
    session = CopilotSession(cfg, ws, _issue(), on_event=events.append)
    await session.start()
    await session.run_turn("prompt")
    await session.stop()

    assert any("unsupported" in (e.event or "").lower() or "bad_tool" in (e.message or "")
               for e in events)
    assert session.session.turn_count == 1


# ---------------------------------------------------------------------------
# §17.5 — User input required → failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_input_required_fails(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"behavior": "input_required"})
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    with pytest.raises(TurnInputRequiredError):
        await session.run_turn("prompt")
    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Token usage extracted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_usage_extracted(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={
        "turns": 1,
        "token_usage": {"input": 200, "output": 100, "total": 300},
    })
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    await session.run_turn("prompt")
    await session.stop()

    assert session.session.copilot_input_tokens == 200
    assert session.session.copilot_output_tokens == 100
    assert session.session.copilot_total_tokens == 300


# ---------------------------------------------------------------------------
# §17.5 — Rate-limit telemetry extracted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_telemetry_extracted(tmp_path):
    ws = _ws(tmp_path)
    events: list[AgentEvent] = []
    cfg = _cfg(tmp_path, agent_cfg={"turns": 1, "rate_limit_turn": 0})
    session = CopilotSession(cfg, ws, _issue(), on_event=events.append)
    await session.start()
    await session.run_turn("prompt")
    await session.stop()

    assert any(e.rate_limits is not None for e in events)


# ---------------------------------------------------------------------------
# §17.5 — Turn failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_failure_raises(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"behavior": "fail"})
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    with pytest.raises(TurnFailedError):
        await session.run_turn("prompt")
    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Turn cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_cancelled_raises(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"behavior": "cancel"})
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    with pytest.raises(TurnCancelledError):
        await session.run_turn("prompt")
    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Subprocess exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_exit_raises(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"behavior": "exit"})
    session = CopilotSession(cfg, ws, _issue())
    await session.start()
    with pytest.raises(PortExitError):
        await session.run_turn("prompt")
    await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — run_agent_session stops on non-active state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_session_stops_on_non_active(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"turns": 5})

    async def closed_after_first(issue_id):
        return Issue(id=issue_id, identifier="#1", title="t", state="closed")

    session = await run_agent_session(
        config=cfg, workspace_path=ws, issue=_issue(),
        prompt="go", attempt=None, max_turns=5,
        fetch_issue_state=closed_after_first,
    )
    assert session.turn_count == 1


# ---------------------------------------------------------------------------
# §17.5 — run_agent_session state-refresh failure fails the attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_refresh_failure_propagates(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, agent_cfg={"turns": 5})

    async def fail_refresh(issue_id):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await run_agent_session(
            config=cfg, workspace_path=ws, issue=_issue(),
            prompt="go", attempt=None, max_turns=5,
            fetch_issue_state=fail_refresh,
        )
