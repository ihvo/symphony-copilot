"""Tests for runner module (SPEC §10, §17.5).

Since the runner now uses the ``github-copilot-sdk`` Python package,
these tests mock the SDK's ``CopilotClient`` and ``CopilotSession``
at the boundary rather than shell scripts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from symphony.config import ServiceConfig
from symphony.errors import (
    InvalidWorkspaceCwdError,
    TurnFailedError,
)
from symphony.models import AgentEvent, Issue, WorkflowDefinition
from symphony.runner import CopilotAgentSession as CopilotSession
from symphony.runner import run_agent_session


def _cfg(tmp_path) -> ServiceConfig:
    raw = {
        "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
        "copilot": {
            "turn_timeout_ms": 5000,
            "read_timeout_ms": 2000,
            "stall_timeout_ms": 60000,
        },
    }
    return ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))


def _ws(tmp_path, name: str = "ws") -> str:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _issue() -> Issue:
    return Issue(id="id1", identifier="#1", title="Test issue", state="open")


def _mock_sdk_session(idle_event_type: str = "session.idle"):
    """Create a mock SDK CopilotSession."""
    mock_session = AsyncMock()
    mock_session.session_id = "mock-session-1"
    mock_session.destroy = AsyncMock()

    # send_and_wait returns a SessionEvent-like object
    idle_event = MagicMock()
    idle_event.type = MagicMock()
    idle_event.type.value = idle_event_type
    idle_event.data = None
    mock_session.send_and_wait = AsyncMock(return_value=idle_event)

    return mock_session


def _mock_sdk_client(mock_session=None):
    """Create a mock CopilotClient."""
    mock_client = AsyncMock()
    mock_client.start = AsyncMock()
    mock_client.stop = AsyncMock()
    mock_client.create_session = AsyncMock(return_value=mock_session or _mock_sdk_session())
    return mock_client


class TestCopilotAgentSession:
    @pytest.mark.asyncio
    async def test_invalid_workspace_raises(self, tmp_path):
        cfg = _cfg(tmp_path)
        session = CopilotSession(cfg, str(tmp_path / "nonexistent"), _issue())
        with pytest.raises(InvalidWorkspaceCwdError):
            await session.start()

    @pytest.mark.asyncio
    async def test_start_creates_session(self, tmp_path):
        ws = _ws(tmp_path)
        cfg = _cfg(tmp_path)
        events: list[AgentEvent] = []

        mock_session = _mock_sdk_session()
        mock_client = _mock_sdk_client(mock_session)

        with patch("symphony.runner.CopilotClient", return_value=mock_client):
            session = CopilotSession(cfg, ws, _issue(), on_event=events.append)
            await session.start()

            assert session.session.thread_id == "mock-session-1"
            assert any(e.event == "session_started" for e in events)

            await session.stop()

    @pytest.mark.asyncio
    async def test_run_turn_increments_count(self, tmp_path):
        ws = _ws(tmp_path)
        cfg = _cfg(tmp_path)
        mock_session = _mock_sdk_session()
        mock_client = _mock_sdk_client(mock_session)

        with patch("symphony.runner.CopilotClient", return_value=mock_client):
            session = CopilotSession(cfg, ws, _issue())
            await session.start()

            await session.run_turn("prompt 1", turn_number=1)
            assert session.session.turn_count == 1

            await session.run_turn("prompt 2", turn_number=2)
            assert session.session.turn_count == 2

            await session.stop()

    @pytest.mark.asyncio
    async def test_turn_failure_raises(self, tmp_path):
        ws = _ws(tmp_path)
        cfg = _cfg(tmp_path)

        mock_session = _mock_sdk_session()
        mock_session.send_and_wait = AsyncMock(side_effect=Exception("model refused"))
        mock_client = _mock_sdk_client(mock_session)

        with patch("symphony.runner.CopilotClient", return_value=mock_client):
            session = CopilotSession(cfg, ws, _issue())
            await session.start()
            with pytest.raises(TurnFailedError):
                await session.run_turn("prompt")
            await session.stop()

    @pytest.mark.asyncio
    async def test_session_error_event_raises(self, tmp_path):
        ws = _ws(tmp_path)
        cfg = _cfg(tmp_path)

        error_event = MagicMock()
        error_event.type = MagicMock()
        error_event.type.value = "session.error"
        error_event.data = MagicMock()
        error_event.data.message = "fatal error"

        mock_session = _mock_sdk_session()
        mock_session.send_and_wait = AsyncMock(return_value=error_event)
        mock_client = _mock_sdk_client(mock_session)

        with patch("symphony.runner.CopilotClient", return_value=mock_client):
            session = CopilotSession(cfg, ws, _issue())
            await session.start()
            with pytest.raises(TurnFailedError, match="fatal error"):
                await session.run_turn("prompt")
            await session.stop()


class TestRunAgentSession:
    @pytest.mark.asyncio
    async def test_stops_on_non_active_state(self, tmp_path):
        ws = _ws(tmp_path)
        cfg = _cfg(tmp_path)

        mock_session = _mock_sdk_session()
        mock_client = _mock_sdk_client(mock_session)

        async def closed_refresh(issue_id):
            return Issue(id=issue_id, identifier="#1", title="t", state="closed")

        with patch("symphony.runner.CopilotClient", return_value=mock_client):
            result = await run_agent_session(
                config=cfg,
                workspace_path=ws,
                issue=_issue(),
                prompt="go",
                attempt=None,
                max_turns=5,
                fetch_issue_state=closed_refresh,
            )
        assert result.turn_count == 1

    @pytest.mark.asyncio
    async def test_state_refresh_error_fails_attempt(self, tmp_path):
        ws = _ws(tmp_path)
        cfg = _cfg(tmp_path)

        mock_session = _mock_sdk_session()
        mock_client = _mock_sdk_client(mock_session)

        async def fail_refresh(issue_id):
            raise RuntimeError("network error")

        with patch("symphony.runner.CopilotClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="network error"):
                await run_agent_session(
                    config=cfg,
                    workspace_path=ws,
                    issue=_issue(),
                    prompt="go",
                    attempt=None,
                    max_turns=5,
                    fetch_issue_state=fail_refresh,
                )

    @pytest.mark.asyncio
    async def test_max_turns_respected(self, tmp_path):
        ws = _ws(tmp_path)
        cfg = _cfg(tmp_path)

        mock_session = _mock_sdk_session()
        mock_client = _mock_sdk_client(mock_session)

        with patch("symphony.runner.CopilotClient", return_value=mock_client):
            result = await run_agent_session(
                config=cfg,
                workspace_path=ws,
                issue=_issue(),
                prompt="go",
                attempt=None,
                max_turns=3,
            )
        assert result.turn_count == 3
        assert mock_session.send_and_wait.call_count == 3
