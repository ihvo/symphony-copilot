"""Tests for Claude harness and harness factory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from symphony.config import ServiceConfig
from symphony.errors import (
    AgentNotFoundError,
    AgentStartupError,
    ConfigValidationError,
    InvalidWorkspaceCwdError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, WorkflowDefinition

# ---------------------------------------------------------------------------
# Mock SDK message types (match real class names for isinstance-by-name checks)
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    text: str = ""


@dataclass
class ToolUseBlock:
    id: str = "tool-1"
    name: str = "bash"
    input: dict = field(default_factory=dict)


@dataclass
class AssistantMessage:
    content: list = field(default_factory=list)
    model: str = "claude-sonnet-4-20250514"
    error: str | None = None
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    stop_reason: str | None = None


@dataclass
class ResultMessage:
    is_error: bool = False
    session_id: str = "default"
    num_turns: int = 1
    duration_ms: int = 1000
    usage: dict[str, Any] | None = None
    errors: list[str] | None = None
    result: str | None = None
    stop_reason: str | None = None


@dataclass
class RateLimitEvent:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(tmp_path, harness: str = "claude", claude_cfg: dict | None = None) -> ServiceConfig:
    raw = {
        "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
        "agent": {"harness": harness},
        "claude": claude_cfg or {"turn_timeout_ms": 5000, "stall_timeout_ms": 60000},
    }
    return ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))


def _ws(tmp_path, name: str = "ws") -> str:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _issue() -> Issue:
    return Issue(id="id1", identifier="#1", title="Test issue", state="open")


def _mock_claude_client(messages: list | None = None):
    """Create a mock ClaudeSDKClient matching real SDK interface.

    Real interface:
      - connect(prompt) → awaitable
      - query(prompt, session_id) → awaitable
      - receive_messages() → async iterator
      - disconnect() → sync
    """
    if messages is None:
        messages = [
            AssistantMessage(content=[TextBlock(text="Done.")]),
            ResultMessage(session_id="claude-sess-1"),
        ]

    client = MagicMock()
    client.connect = AsyncMock()
    client.query = AsyncMock()
    client.disconnect = AsyncMock()

    async def _receive():
        for m in messages:
            yield m

    client.receive_messages = _receive
    return client


def _mock_sdk_module(client=None):
    """Create a mock claude_agent_sdk module."""
    if client is None:
        client = _mock_claude_client()

    mock_module = MagicMock()
    mock_module.ClaudeSDKClient = MagicMock(return_value=client)
    mock_module.ClaudeAgentOptions = MagicMock()
    mock_module.CLINotFoundError = type("CLINotFoundError", (Exception,), {})
    return mock_module


class TestClaudeHarnessLifecycle:
    @pytest.mark.asyncio
    async def test_invalid_workspace_raises(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        mock_module = _mock_sdk_module()

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, str(tmp_path / "nonexistent"), _issue())
            with pytest.raises(InvalidWorkspaceCwdError):
                await harness.start()

    @pytest.mark.asyncio
    async def test_missing_sdk_raises_agent_not_found(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)
        harness = ClaudeHarness(cfg, ws, _issue())

        with patch.dict("sys.modules", {"claude_agent_sdk": None}):
            with pytest.raises(AgentNotFoundError, match="claude"):
                await harness.start()

    @pytest.mark.asyncio
    async def test_start_creates_session(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)
        events: list[AgentEvent] = []

        mock_module = _mock_sdk_module()

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue(), on_event=events.append)
            await harness.start()

            assert harness._started is True
            assert any(e.event == "session_started" for e in events)

            await harness.stop()

    @pytest.mark.asyncio
    async def test_startup_failure_raises_agent_startup_error(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        mock_module = _mock_sdk_module()
        mock_module.ClaudeSDKClient = MagicMock(side_effect=RuntimeError("spawn failed"))

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            with pytest.raises(AgentStartupError, match="claude"):
                await harness.start()


class TestClaudeHarnessTurns:
    @pytest.mark.asyncio
    async def test_run_turn_increments_count(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        messages = [
            AssistantMessage(content=[TextBlock(text="Done.")]),
            ResultMessage(session_id="sess-1"),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()

            await harness.run_turn("prompt 1", turn_number=1)
            assert harness.session.turn_count == 1
            # connect() called for turn 1
            client.connect.assert_awaited_once()

            # Reset receive_messages for turn 2
            async def _receive2():
                yield AssistantMessage(content=[TextBlock(text="More")])
                yield ResultMessage(session_id="sess-1")

            client.receive_messages = _receive2

            await harness.run_turn("prompt 2", turn_number=2)
            assert harness.session.turn_count == 2
            # query() called for turn 2
            client.query.assert_awaited_once()

            await harness.stop()

    @pytest.mark.asyncio
    async def test_turn_timeout_raises(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        client = _mock_claude_client()
        # connect() will timeout
        client.connect = AsyncMock(side_effect=TimeoutError())
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            with pytest.raises(TurnTimeoutError):
                await harness.run_turn("prompt")
            await harness.stop()

    @pytest.mark.asyncio
    async def test_turn_cancelled_raises(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        client = _mock_claude_client()
        client.connect = AsyncMock(side_effect=asyncio.CancelledError())
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            with pytest.raises(TurnCancelledError):
                await harness.run_turn("prompt")
            await harness.stop()

    @pytest.mark.asyncio
    async def test_turn_failure_raises(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        client = _mock_claude_client()
        client.connect = AsyncMock(side_effect=RuntimeError("model refused"))
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            with pytest.raises(TurnFailedError, match="model refused"):
                await harness.run_turn("prompt")
            await harness.stop()

    @pytest.mark.asyncio
    async def test_turn_input_required_from_exception(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        client = _mock_claude_client()
        client.connect = AsyncMock(side_effect=RuntimeError("user input required"))
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            with pytest.raises(TurnInputRequiredError):
                await harness.run_turn("prompt")
            await harness.stop()


class TestClaudeHarnessEvents:
    @pytest.mark.asyncio
    async def test_usage_event_updates_tokens(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)
        events: list[AgentEvent] = []

        messages = [
            AssistantMessage(
                content=[TextBlock(text="Done.")],
                usage={"input_tokens": 100, "output_tokens": 50},
            ),
            ResultMessage(
                session_id="sess-1",
                usage={"input_tokens": 100, "output_tokens": 50},
            ),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue(), on_event=events.append)
            await harness.start()
            await harness.run_turn("prompt")

            assert harness.session.copilot_input_tokens == 100
            assert harness.session.copilot_output_tokens == 50
            assert harness.session.copilot_total_tokens == 150

            # Verify usage event was emitted
            usage_events = [e for e in events if e.usage is not None]
            assert len(usage_events) >= 1
            assert usage_events[0].usage["input_tokens"] == 100

            await harness.stop()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        client = _mock_claude_client()
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            await harness.stop()
            await harness.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_streamed_error_fails_turn(self, tmp_path):
        """A ResultMessage with is_error=True should cause the turn to fail."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)
        events: list[AgentEvent] = []

        messages = [
            AssistantMessage(content=[TextBlock(text="Partial work...")]),
            ResultMessage(is_error=True, errors=["something went wrong"]),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue(), on_event=events.append)
            await harness.start()
            with pytest.raises(TurnFailedError, match="something went wrong"):
                await harness.run_turn("prompt")
            await harness.stop()

    @pytest.mark.asyncio
    async def test_assistant_error_field_fails_turn(self, tmp_path):
        """An AssistantMessage with error field set should cause the turn to fail."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        messages = [
            AssistantMessage(content=[], error="rate_limit"),
            ResultMessage(session_id="sess-1"),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            with pytest.raises(TurnFailedError, match="rate_limit"):
                await harness.run_turn("prompt")
            await harness.stop()

    @pytest.mark.asyncio
    async def test_streamed_input_required_raises_correct_error(self, tmp_path):
        """A ResultMessage with 'input required' in errors raises TurnInputRequiredError."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        messages = [
            ResultMessage(is_error=True, errors=["user input required for confirmation"]),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            with pytest.raises(TurnInputRequiredError):
                await harness.run_turn("prompt")
            await harness.stop()

    @pytest.mark.asyncio
    async def test_tool_use_emits_notification(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)
        events: list[AgentEvent] = []

        messages = [
            AssistantMessage(content=[ToolUseBlock(id="t1", name="bash", input={})]),
            ResultMessage(session_id="sess-1"),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue(), on_event=events.append)
            await harness.start()
            await harness.run_turn("prompt")

            notif_events = [e for e in events if e.message and "Tool use" in e.message]
            assert len(notif_events) >= 1
            await harness.stop()

    @pytest.mark.asyncio
    async def test_session_id_tracked_across_turns(self, tmp_path):
        """ResultMessage.session_id is stored for multi-turn continuations."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        messages = [
            AssistantMessage(content=[TextBlock(text="Done")]),
            ResultMessage(session_id="real-session-abc"),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            await harness.run_turn("prompt", turn_number=1)

            assert harness._sdk_session_id == "real-session-abc"
            assert harness.session.thread_id == "real-session-abc"
            await harness.stop()

    @pytest.mark.asyncio
    async def test_stream_without_result_message_fails(self, tmp_path):
        """If stream ends without a ResultMessage, the turn should fail."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        # Only AssistantMessage, no ResultMessage — stream ends unexpectedly
        messages = [
            AssistantMessage(content=[TextBlock(text="Partial output")]),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()
            with pytest.raises(TurnFailedError, match="Stream ended without result"):
                await harness.run_turn("prompt")
            await harness.stop()


class TestHarnessFactory:
    @pytest.mark.asyncio
    async def test_factory_copilot(self, tmp_path):
        from symphony.runner import CopilotHarness, _create_harness

        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                    "agent": {"harness": "copilot"},
                },
                prompt_template="",
            ),
            str(tmp_path),
        )
        ws = _ws(tmp_path)
        harness = _create_harness(cfg, ws, _issue())
        assert isinstance(harness, CopilotHarness)

    @pytest.mark.asyncio
    async def test_factory_claude(self, tmp_path):
        from symphony.claude_runner import ClaudeHarness
        from symphony.runner import _create_harness

        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                    "agent": {"harness": "claude"},
                    "claude": {},
                },
                prompt_template="",
            ),
            str(tmp_path),
        )
        ws = _ws(tmp_path)
        harness = _create_harness(cfg, ws, _issue())
        assert isinstance(harness, ClaudeHarness)

    @pytest.mark.asyncio
    async def test_factory_unknown_raises(self, tmp_path):
        from symphony.runner import _create_harness

        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                    "agent": {"harness": "unknown"},
                },
                prompt_template="",
            ),
            str(tmp_path),
        )
        ws = _ws(tmp_path)
        with pytest.raises(ConfigValidationError, match="Unknown agent harness"):
            _create_harness(cfg, ws, _issue())

    @pytest.mark.asyncio
    async def test_factory_default_is_copilot(self, tmp_path):
        from symphony.runner import CopilotHarness, _create_harness

        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                },
                prompt_template="",
            ),
            str(tmp_path),
        )
        ws = _ws(tmp_path)
        harness = _create_harness(cfg, ws, _issue())
        assert isinstance(harness, CopilotHarness)
