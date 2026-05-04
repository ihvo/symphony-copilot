"""Integration tests for the Claude Agent SDK harness.

Tests ClaudeHarness lifecycle, multi-turn sessions, error handling,
stall-timeout detection, and orchestrator dispatch via the ``agent_harness``
config field.  Mocks the ``claude_agent_sdk`` module boundary so no real
Claude CLI is required.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from symphony.config import ServiceConfig
from symphony.errors import (
    AgentNotFoundError,
    AgentStartupError,
    TurnFailedError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, LiveSession, WorkflowDefinition
from symphony.orchestrator import Orchestrator

from .conftest import FakeGitHub, agent_command, wait_until


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


def _cfg(tmp_path, **overrides) -> ServiceConfig:
    ws_root = str(tmp_path / "workspaces")
    os.makedirs(ws_root, exist_ok=True)
    raw: dict = {
        "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
        "copilot": {"command": "echo unused", "turn_timeout_ms": 10000},
        "agent": {"harness": "claude"},
        "claude": {
            "command": "claude",
            "turn_timeout_ms": 5000,
            "stall_timeout_ms": 60000,
            **overrides,
        },
        "workspace": {"root": ws_root},
    }
    return ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))


def _ws(tmp_path) -> str:
    p = tmp_path / "workspaces" / "issue-1"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _issue() -> Issue:
    return Issue(id="NODE_1", identifier="#1", title="Test issue", state="open")


def _mock_claude_client(messages: list | None = None):
    """Create a mock ClaudeSDKClient matching real SDK interface."""
    if messages is None:
        messages = [
            AssistantMessage(content=[TextBlock(text="Done.")]),
            ResultMessage(session_id="mock-claude-1"),
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


# ---------------------------------------------------------------------------
# ClaudeHarness multi-turn session
# ---------------------------------------------------------------------------


class TestClaudeHarnessIntegration:
    @pytest.mark.asyncio
    async def test_multi_turn_session(self, tmp_path):
        """ClaudeHarness can execute multiple turns and tracks session state."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)
        events: list[AgentEvent] = []

        messages = [
            AssistantMessage(
                content=[TextBlock(text="Turn completed")],
                usage={"input_tokens": 50, "output_tokens": 25},
            ),
            ResultMessage(session_id="mock-claude-1", usage={"input_tokens": 50, "output_tokens": 25}),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue(), on_event=events.append)
            await harness.start()

            assert harness._started is True

            # Turn 1 — uses connect()
            result = await harness.run_turn("First prompt", turn_number=1)
            assert result is True
            assert harness.session.turn_count == 1
            assert harness.session.copilot_input_tokens == 50
            assert harness.session.copilot_output_tokens == 25
            client.connect.assert_awaited_once()

            # Reset receive_messages for turn 2
            async def _receive2():
                yield AssistantMessage(content=[TextBlock(text="More work")])
                yield ResultMessage(session_id="mock-claude-1")

            client.receive_messages = _receive2

            # Turn 2 — uses query()
            result = await harness.run_turn("Second prompt", turn_number=2)
            assert result is True
            assert harness.session.turn_count == 2
            client.query.assert_awaited_once()

            await harness.stop()

        # Verify events emitted
        event_names = [e.event for e in events]
        assert "session_started" in event_names
        assert event_names.count("turn_completed") == 2

    @pytest.mark.asyncio
    async def test_streamed_error_fails_turn(self, tmp_path):
        """When Claude streams a ResultMessage with is_error, run_turn raises TurnFailedError."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)
        events: list[AgentEvent] = []

        messages = [
            AssistantMessage(content=[TextBlock(text="Partial work...")]),
            ResultMessage(is_error=True, errors=["rate limit exceeded"]),
        ]
        client = _mock_claude_client(messages)
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue(), on_event=events.append)
            await harness.start()

            with pytest.raises(TurnFailedError, match="rate limit exceeded"):
                await harness.run_turn("prompt")

            await harness.stop()

        # turn_failed event should have been emitted
        failed_events = [e for e in events if e.event == "turn_failed"]
        assert len(failed_events) == 1

    @pytest.mark.asyncio
    async def test_stall_timeout_detection(self, tmp_path):
        """When the Claude connect() hangs past turn_timeout_ms, TurnTimeoutError is raised."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path, turn_timeout_ms=200)  # Very short timeout
        ws = _ws(tmp_path)

        client = _mock_claude_client()
        # connect() will raise TimeoutError when wait_for wraps it
        client.connect = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_module = _mock_sdk_module(client)

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            await harness.start()

            with pytest.raises(TurnTimeoutError):
                await harness.run_turn("prompt")

            await harness.stop()

    @pytest.mark.asyncio
    async def test_sdk_import_error(self, tmp_path):
        """AgentNotFoundError raised when claude_agent_sdk is not installed."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        # Remove module from cache to simulate missing package
        with patch.dict("sys.modules", {"claude_agent_sdk": None}):
            harness = ClaudeHarness(cfg, ws, _issue())
            with pytest.raises(AgentNotFoundError, match="not installed"):
                await harness.start()

    @pytest.mark.asyncio
    async def test_client_construction_failure(self, tmp_path):
        """AgentStartupError raised when ClaudeSDKClient() constructor fails."""
        from symphony.claude_runner import ClaudeHarness

        cfg = _cfg(tmp_path)
        ws = _ws(tmp_path)

        mock_module = _mock_sdk_module()
        mock_module.ClaudeSDKClient = MagicMock(side_effect=RuntimeError("spawn failed"))

        with patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):
            harness = ClaudeHarness(cfg, ws, _issue())
            with pytest.raises(AgentStartupError, match="spawn failed"):
                await harness.start()


# ---------------------------------------------------------------------------
# Orchestrator dispatches Claude harness
# ---------------------------------------------------------------------------


class TestOrchestratorClaudeDispatch:
    @pytest.mark.asyncio
    async def test_orchestrator_selects_claude_harness(
        self, fake_github, make_workflow, tmp_path
    ):
        """When agent.harness=claude, orchestrator dispatches via ClaudeHarness."""
        fake_github.add_issue(1, state="open")

        # Build workflow with claude harness + mock agent command
        wf = make_workflow(
            endpoint=fake_github.base_url,
            max_turns=1,
            extra_yaml=(
                "agent:\n"
                "  harness: claude\n"
                "  max_turns: 1\n"
                "claude:\n"
                "  command: claude\n"
                "  turn_timeout_ms: 5000\n"
            ),
        )

        # Track which harness type is created
        harness_types_created: list[str] = []

        # Create a mock claude harness that succeeds
        mock_harness = MagicMock()
        mock_harness.session = LiveSession()
        mock_harness.session.thread_id = "mock-thread"
        mock_harness.session.session_id = "mock-session"
        mock_harness.start = AsyncMock()
        mock_harness.run_turn = AsyncMock(return_value=True)
        mock_harness.stop = AsyncMock()

        def _patched_create_harness(cfg, ws, issue, on_event=None):
            harness_name = cfg.agent_harness
            harness_types_created.append(harness_name)
            return mock_harness

        # Patch claude_agent_sdk so validate_dispatch() passes
        mock_sdk = MagicMock()
        with patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk}):
            with patch("symphony.runner._create_harness", side_effect=_patched_create_harness):
                orch = Orchestrator(wf)
                await orch.start()

                try:
                    ok = await wait_until(
                        lambda: len(harness_types_created) > 0,
                        timeout=8.0,
                    )
                    assert ok, "orchestrator did not dispatch agent"
                    assert "claude" in harness_types_created
                finally:
                    await orch.stop()
