"""DevHarness — agent harness for dev mode.

Launches dev_mock_agent.py directly via raw subprocess + JSONRPC-over-stdio,
bypassing the Copilot SDK entirely while exercising the same runner contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from symphony.config import ServiceConfig
from symphony.errors import (
    PortExitError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
)
from symphony.models import AgentEvent, Issue, LiveSession

logger = logging.getLogger("symphony.dev_harness")

# Path to the bundled mock agent script
_MOCK_AGENT_PATH = os.path.join(os.path.dirname(__file__), "dev_mock_agent.py")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class DevHarness:
    """Agent harness for dev mode — launches mock_agent.py directly.

    Implements the same interface as CopilotAgentSession (start/run_turn/stop/session)
    using raw JSONRPC-over-stdio, matching the mock_agent.py protocol.
    """

    def __init__(
        self,
        config: ServiceConfig,
        workspace_path: str,
        issue: Issue,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._config = config
        self._workspace = os.path.abspath(workspace_path)
        self._issue = issue
        self._on_event = on_event
        self._session = LiveSession()
        self._proc: asyncio.subprocess.Process | None = None
        self._started = False
        self._request_id = 0

    @property
    def session(self) -> LiveSession:
        return self._session

    def _emit(self, event_name: str, **kwargs: Any) -> None:
        evt = AgentEvent(
            event=event_name,
            issue_id=self._issue.id,
            timestamp=_now_utc(),
            copilot_pid=self._proc.pid if self._proc else None,
            session_id=self._session.session_id or None,
            **kwargs,
        )
        self._session.last_copilot_event = event_name
        self._session.last_copilot_timestamp = evt.timestamp
        if kwargs.get("message"):
            self._session.last_copilot_message = kwargs["message"]
        if self._on_event:
            self._on_event(evt)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _send(self, msg: dict) -> None:
        """Send a JSONRPC message to the subprocess."""
        if not self._proc or not self._proc.stdin:
            raise PortExitError(None)
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        await self._proc.stdin.drain()

    async def _recv(self) -> dict:
        """Read one JSONRPC message from the subprocess."""
        if not self._proc or not self._proc.stdout:
            raise PortExitError(None)
        line = await asyncio.wait_for(
            self._proc.stdout.readline(),
            timeout=self._config.copilot_turn_timeout_ms / 1000.0,
        )
        if not line:
            raise PortExitError(self._proc.pid)
        return json.loads(line.decode().strip())

    async def start(self) -> None:
        """Launch dev_mock_agent.py as a subprocess."""
        if not os.path.isdir(self._workspace):
            os.makedirs(self._workspace, exist_ok=True)

        cfg = {
            "turns": self._config.dev_agent_turns,
            "behavior": self._config.dev_agent_behavior,
            "slow_turn_ms": self._config.dev_agent_delay_ms,
        }

        logger.info(
            "dev_agent_launch cwd=%s issue=%s config=%s",
            self._workspace, self._issue.identifier, cfg,
        )

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, _MOCK_AGENT_PATH, json.dumps(cfg),
            cwd=self._workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._session.copilot_pid = self._proc.pid

        # Send initialize request
        await self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize", "params": {}})
        resp = await self._recv()

        # Send thread/create
        await self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "thread/create", "params": {}})
        resp = await self._recv()
        thread_id = resp.get("result", {}).get("threadId", "dev-thread")
        self._session.thread_id = thread_id
        self._session.session_id = thread_id
        self._started = True

        self._emit("session_started", message=f"Dev session {thread_id} created")

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Execute one turn via JSONRPC. Returns True on success."""
        if not self._started or not self._proc:
            raise PortExitError(None)

        self._session.turn_count += 1
        self._session.turn_id = f"turn-{turn_number}"
        self._session.session_id = f"{self._session.thread_id}-turn-{turn_number}"

        # Send turn/start
        await self._send({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "turn/start",
            "params": {"prompt": prompt},
        })
        # Read turnId ack
        resp = await self._recv()
        if "error" in resp:
            raise TurnFailedError(str(resp["error"]))

        # Read events until terminal
        while True:
            event = await self._recv()

            # Handle JSONRPC error responses
            if "error" in event:
                raise TurnFailedError(str(event["error"].get("message", "protocol error")))

            method = event.get("method", "")
            params = event.get("params", {})

            if method == "turn/completed":
                self._emit("turn_completed", message=f"Turn {turn_number} completed")
                return True
            elif method == "turn/failed":
                error_msg = params.get("error", "mock failure")
                raise TurnFailedError(str(error_msg))
            elif method == "turn/cancelled":
                raise TurnCancelledError()
            elif method == "turn/inputRequired":
                raise TurnInputRequiredError()
            elif method == "thread/tokenUsage/updated":
                usage = params.get("usage", {})
                self._session.copilot_input_tokens = usage.get("inputTokens", 0)
                self._session.copilot_output_tokens = usage.get("outputTokens", 0)
                self._session.copilot_total_tokens = usage.get("totalTokens", 0)
                self._emit("notification", usage=usage)
            elif method == "rateLimits/updated":
                self._emit("notification", rate_limits=params)
            elif "id" in event and method:
                # Server-initiated request (approval, tool call) — auto-approve
                await self._send({
                    "jsonrpc": "2.0",
                    "id": event["id"],
                    "result": {"approved": True},
                })
            elif method:
                self._emit("notification", message=method)

    async def stop(self) -> None:
        """Terminate the mock agent subprocess."""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                self._proc.kill()
        self._started = False


async def run_dev_agent_session(
    config: ServiceConfig,
    workspace_path: str,
    issue: Issue,
    prompt: str,
    attempt: int | None,
    on_event: Callable[[AgentEvent], None] | None = None,
    max_turns: int = 20,
    fetch_issue_state: Callable[[str], Any] | None = None,
) -> LiveSession:
    """Run a full dev agent session: start, multi-turn loop, stop.

    Drop-in replacement for runner.run_agent_session() in dev mode.
    """
    harness = DevHarness(config, workspace_path, issue, on_event=on_event)

    try:
        await harness.start()
    except Exception as exc:
        logger.error("dev_agent_startup_failed issue=%s error=%s", issue.identifier, exc)
        raise

    try:
        turn_number = 1
        current_prompt = prompt

        while True:
            await harness.run_turn(current_prompt, turn_number)

            # Re-check issue state if callback is provided
            if fetch_issue_state:
                refreshed = await fetch_issue_state(issue.id)
                if refreshed and hasattr(refreshed, "state"):
                    issue_state = refreshed.state.lower()
                    if issue_state not in config.active_states:
                        logger.info(
                            "issue_no_longer_active issue=%s state=%s",
                            issue.identifier, issue_state,
                        )
                        break

            if turn_number >= max_turns:
                logger.info("max_turns_reached issue=%s turns=%d", issue.identifier, max_turns)
                break

            turn_number += 1
            current_prompt = (
                "Continue working on the issue. Review your previous progress "
                "and continue from where you left off."
            )

    finally:
        await harness.stop()

    return harness.session
