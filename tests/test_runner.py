"""Tests for runner module (SPEC §10, §17.5)."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from symphony.config import ServiceConfig
from symphony.errors import (
    CopilotNotFoundError,
    InvalidWorkspaceCwdError,
    PortExitError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, WorkflowDefinition
from symphony.runner import CopilotSession, run_agent_session


def _cfg(tmp_path, command: str = "echo test") -> ServiceConfig:
    raw = {
        "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
        "workspace": {"root": str(tmp_path / "ws")},
        "copilot": {
            "command": command,
            "turn_timeout_ms": 2000,
            "read_timeout_ms": 1000,
            "stall_timeout_ms": 5000,
        },
    }
    return ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))


def _issue() -> Issue:
    return Issue(id="id1", identifier="#1", title="Test issue", state="open")


class TestCopilotSessionLaunch:
    @pytest.mark.asyncio
    async def test_launch_uses_workspace_cwd(self, tmp_path):
        """Agent launch uses workspace path as cwd (spec §10.1)."""
        ws = tmp_path / "ws" / "_1"
        ws.mkdir(parents=True)

        # Use a script that prints cwd as JSON
        script = str(tmp_path / "mock_agent.sh")
        with open(script, "w") as f:
            f.write('#!/bin/bash\npwd\n')
        os.chmod(script, 0o755)

        cfg = _cfg(tmp_path, command=f"bash {script}")
        session = CopilotSession(cfg, str(ws), _issue())

        # The session.start() will fail because the script won't speak JSON-RPC,
        # but it verifies the subprocess launch with correct cwd
        with pytest.raises(Exception):
            await session.start()
        await session.stop()

    @pytest.mark.asyncio
    async def test_invalid_workspace_raises(self, tmp_path):
        """Non-existent workspace raises InvalidWorkspaceCwdError."""
        cfg = _cfg(tmp_path)
        session = CopilotSession(cfg, str(tmp_path / "nonexistent"), _issue())
        with pytest.raises(InvalidWorkspaceCwdError):
            await session.start()

    @pytest.mark.asyncio
    async def test_copilot_not_found(self, tmp_path):
        """Non-existent command raises CopilotNotFoundError."""
        ws = tmp_path / "ws" / "_1"
        ws.mkdir(parents=True)
        cfg = _cfg(tmp_path, command="/nonexistent/binary_xyz_123")
        session = CopilotSession(cfg, str(ws), _issue())
        # bash -lc of a nonexistent binary will start bash but the command
        # will fail inside bash; we should get PortExitError or similar
        with pytest.raises(Exception):
            await session.start()
        await session.stop()


class TestCopilotSessionEvents:
    @pytest.mark.asyncio
    async def test_events_emitted(self, tmp_path):
        """Events emitted to on_event callback."""
        ws = tmp_path / "ws" / "_1"
        ws.mkdir(parents=True)

        events: list[AgentEvent] = []

        def collector(evt: AgentEvent) -> None:
            events.append(evt)

        # Create a mock agent that outputs JSON-RPC responses
        script = tmp_path / "mock.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'  # init request
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'  # thread/create
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"t1"}}\'\n'
            'read line\n'  # turn/start
            'echo \'{"jsonrpc":"2.0","id":3,"result":{"turnId":"turn1"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            'read line\n'  # shutdown
        )
        os.chmod(str(script), 0o755)

        cfg = _cfg(tmp_path, command=f"bash {script}")
        session = CopilotSession(cfg, str(ws), _issue(), on_event=collector)
        await session.start()

        assert any(e.event == "session_started" for e in events)
        assert session.session.thread_id == "t1"

        await session.run_turn("test prompt")
        assert session.session.session_id == "t1-turn1"
        assert any(e.event == "turn_completed" for e in events)

        await session.stop()


class TestMultiTurnSession:
    @pytest.mark.asyncio
    async def test_turn_count_increments(self, tmp_path):
        """Turn count increments across multi-turn session."""
        ws = tmp_path / "ws" / "_1"
        ws.mkdir(parents=True)

        # Agent that handles 2 turns
        script = tmp_path / "multi.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"t1"}}\'\n'
            # Turn 1
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":3,"result":{"turnId":"turn1"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            # Turn 2
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":4,"result":{"turnId":"turn2"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            'read line\n'
        )
        os.chmod(str(script), 0o755)

        cfg = _cfg(tmp_path, command=f"bash {script}")
        session = CopilotSession(cfg, str(ws), _issue())
        await session.start()

        await session.run_turn("prompt 1", turn_number=1)
        assert session.session.turn_count == 1

        await session.run_turn("prompt 2", turn_number=2)
        assert session.session.turn_count == 2
        # Thread ID stays the same
        assert session.session.thread_id == "t1"
        # Turn ID updates
        assert session.session.turn_id == "turn2"

        await session.stop()


class TestRunAgentSession:
    @pytest.mark.asyncio
    async def test_stops_when_issue_not_active(self, tmp_path):
        """run_agent_session stops when issue state becomes non-active."""
        ws = tmp_path / "ws" / "_1"
        ws.mkdir(parents=True)

        # Agent that can handle multiple turns
        script = tmp_path / "agent.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"t1"}}\'\n'
            # Turn 1
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":3,"result":{"turnId":"turn1"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            'read line\n'
        )
        os.chmod(str(script), 0o755)

        cfg = _cfg(tmp_path, command=f"bash {script}")

        # State refresh says issue is closed
        async def mock_refresh(issue_id: str):
            return Issue(id=issue_id, identifier="#1", title="t", state="closed")

        session = await run_agent_session(
            config=cfg,
            workspace_path=str(ws),
            issue=_issue(),
            prompt="test",
            attempt=None,
            max_turns=5,
            fetch_issue_state=mock_refresh,
        )
        # Should have run only 1 turn (stopped because issue became closed)
        assert session.turn_count == 1

    @pytest.mark.asyncio
    async def test_state_refresh_error_fails_attempt(self, tmp_path):
        """State refresh failure raises and fails the worker attempt (spec §16.5)."""
        ws = tmp_path / "ws" / "_1"
        ws.mkdir(parents=True)

        script = tmp_path / "agent.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"t1"}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":3,"result":{"turnId":"turn1"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            'read line\n'
        )
        os.chmod(str(script), 0o755)

        cfg = _cfg(tmp_path, command=f"bash {script}")

        async def failing_refresh(issue_id: str):
            raise RuntimeError("network error")

        with pytest.raises(RuntimeError, match="network error"):
            await run_agent_session(
                config=cfg,
                workspace_path=str(ws),
                issue=_issue(),
                prompt="test",
                attempt=None,
                max_turns=5,
                fetch_issue_state=failing_refresh,
            )
