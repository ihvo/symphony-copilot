"""§17.5 — Coding-Agent App-Server Client integration tests.

Tests the ``CopilotAgentSession`` / ``run_agent_session`` by mocking the
Copilot SDK's ``CopilotClient`` at the boundary.  Verifies session lifecycle,
timeout handling, event extraction, and multi-turn behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from symphony.config import ServiceConfig
from symphony.errors import (
    InvalidWorkspaceCwdError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, WorkflowDefinition
from symphony.runner import CopilotAgentSession as CopilotSession
from symphony.runner import run_agent_session


def _cfg(tmp_path, **overrides) -> ServiceConfig:
    raw: dict = {
        "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
        "copilot": {
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


def _mock_sdk_session(idle_type: str = "session.idle"):
    """Create a mock SDK CopilotSession."""
    ms = AsyncMock()
    ms.session_id = "mock-thread-1"
    ms.destroy = AsyncMock()
    idle = MagicMock()
    idle.type = MagicMock()
    idle.type.value = idle_type
    idle.data = None
    ms.send_and_wait = AsyncMock(return_value=idle)
    return ms


def _mock_client(mock_session=None):
    mc = AsyncMock()
    mc.start = AsyncMock()
    mc.stop = AsyncMock()
    mc.create_session = AsyncMock(return_value=mock_session or _mock_sdk_session())
    return mc


# ---------------------------------------------------------------------------
# §17.5 — Launch in workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launch_creates_session_in_workspace(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    mc = _mock_client()
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue())
        await session.start()
        assert session.session.copilot_pid is None  # SDK manages PID
        assert session.session.thread_id == "mock-thread-1"
        await session.stop()


@pytest.mark.asyncio
async def test_invalid_workspace_raises(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(InvalidWorkspaceCwdError):
        s = CopilotSession(cfg, str(tmp_path / "nope"), _issue())
        await s.start()


# ---------------------------------------------------------------------------
# §17.5 — Thread and turn identities → session_started
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_turn_identities(tmp_path):
    ws = _ws(tmp_path)
    events: list[AgentEvent] = []
    cfg = _cfg(tmp_path)
    mc = _mock_client()
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue(), on_event=events.append)
        await session.start()
        assert session.session.thread_id == "mock-thread-1"
        assert any(e.event == "session_started" for e in events)

        await session.run_turn("prompt")
        assert session.session.session_id == "mock-thread-1-turn-1"
        assert any(e.event == "turn_completed" for e in events)
        await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Multi-turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_turn_reuses_thread(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    ms = _mock_sdk_session()
    mc = _mock_client(ms)
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue())
        await session.start()
        for i in range(1, 4):
            await session.run_turn(f"prompt {i}", turn_number=i)
        assert session.session.turn_count == 3
        assert session.session.thread_id == "mock-thread-1"
        await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Turn timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_timeout_enforced(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path, turn_timeout_ms=100)
    ms = _mock_sdk_session()
    ms.send_and_wait = AsyncMock(side_effect=TimeoutError())
    mc = _mock_client(ms)
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue())
        await session.start()
        with pytest.raises(TurnTimeoutError):
            await session.run_turn("prompt")
        await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Turn failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_failure_raises(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    ms = _mock_sdk_session()
    ms.send_and_wait = AsyncMock(side_effect=Exception("model refused"))
    mc = _mock_client(ms)
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue())
        await session.start()
        with pytest.raises(TurnFailedError):
            await session.run_turn("prompt")
        await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Session error event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_error_raises(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    err_evt = MagicMock()
    err_evt.type = MagicMock()
    err_evt.type.value = "session.error"
    err_evt.data = MagicMock()
    err_evt.data.message = "critical failure"
    ms = _mock_sdk_session()
    ms.send_and_wait = AsyncMock(return_value=err_evt)
    mc = _mock_client(ms)
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue())
        await session.start()
        with pytest.raises(TurnFailedError, match="critical failure"):
            await session.run_turn("prompt")
        await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — User input required → failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_input_required_fails(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    ms = _mock_sdk_session()
    ms.send_and_wait = AsyncMock(side_effect=Exception("user input required"))
    mc = _mock_client(ms)
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue())
        await session.start()
        with pytest.raises(TurnInputRequiredError):
            await session.run_turn("prompt")
        await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — Cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_cancelled_raises(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    ms = _mock_sdk_session()
    ms.send_and_wait = AsyncMock(side_effect=Exception("turn was cancelled"))
    mc = _mock_client(ms)
    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = CopilotSession(cfg, ws, _issue())
        await session.start()
        with pytest.raises(TurnCancelledError):
            await session.run_turn("prompt")
        await session.stop()


# ---------------------------------------------------------------------------
# §17.5 — run_agent_session stops on non-active state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_session_stops_on_non_active(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    mc = _mock_client()

    async def closed_after_first(issue_id):
        return Issue(id=issue_id, identifier="#1", title="t", state="closed")

    with patch("symphony.runner.CopilotClient", return_value=mc):
        session = await run_agent_session(
            config=cfg,
            workspace_path=ws,
            issue=_issue(),
            prompt="go",
            attempt=None,
            max_turns=5,
            fetch_issue_state=closed_after_first,
        )
    assert session.turn_count == 1


# ---------------------------------------------------------------------------
# §17.5 — State refresh failure propagates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_refresh_failure_propagates(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    mc = _mock_client()

    async def fail_refresh(issue_id):
        raise RuntimeError("boom")

    with patch("symphony.runner.CopilotClient", return_value=mc):
        with pytest.raises(RuntimeError, match="boom"):
            await run_agent_session(
                config=cfg,
                workspace_path=ws,
                issue=_issue(),
                prompt="go",
                attempt=None,
                max_turns=5,
                fetch_issue_state=fail_refresh,
            )


# ---------------------------------------------------------------------------
# §17.5 — Max turns respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_turns_respected(tmp_path):
    ws = _ws(tmp_path)
    cfg = _cfg(tmp_path)
    mc = _mock_client()

    with patch("symphony.runner.CopilotClient", return_value=mc):
        result = await run_agent_session(
            config=cfg,
            workspace_path=ws,
            issue=_issue(),
            prompt="go",
            attempt=None,
            max_turns=3,
        )
    assert result.turn_count == 3
