"""Claude Agent SDK harness implementation.

Uses the ``claude-agent-sdk`` Python package which spawns a Claude CLI
subprocess and communicates via structured message streams.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from symphony.config import ServiceConfig
from symphony.errors import (
    AgentNotFoundError,
    AgentStartupError,
    InvalidWorkspaceCwdError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, LiveSession

logger = logging.getLogger("symphony.claude_runner")


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ClaudeHarness:
    """Manages a live Claude Agent SDK session with multi-turn support.

    Implements the ``AgentHarness`` protocol.

    Real SDK flow:
      1. connect(prompt) — spawns CLI subprocess, sends first prompt
      2. receive_messages() — async iterator yielding typed messages
      3. query(prompt, session_id) — sends subsequent prompts
      4. disconnect() — kills subprocess
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
        self._started = False
        self._client: Any = None  # ClaudeSDKClient
        self._sdk_session_id: str = "default"

    @property
    def session(self) -> LiveSession:
        return self._session

    def _emit(self, event_name: str, **kwargs: Any) -> None:
        """Emit an AgentEvent — identical pattern to CopilotHarness."""
        evt = AgentEvent(
            event=event_name,
            issue_id=self._issue.id,
            timestamp=_now_utc(),
            copilot_pid=self._session.copilot_pid,
            session_id=self._session.session_id or None,
            **kwargs,
        )
        self._session.last_copilot_event = event_name
        self._session.last_copilot_timestamp = evt.timestamp
        if kwargs.get("message"):
            self._session.last_copilot_message = kwargs["message"]
        if self._on_event:
            self._on_event(evt)

    async def start(self) -> None:
        """Construct Claude SDK client (subprocess spawned on first turn)."""
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions,
                ClaudeSDKClient,
                CLINotFoundError,
            )
        except ImportError as exc:
            raise AgentNotFoundError(
                "claude",
                "Package not installed. Install with: pip install symphony[claude]",
            ) from exc

        if not os.path.isdir(self._workspace):
            raise InvalidWorkspaceCwdError(self._workspace, "does not exist")

        logger.info(
            "agent_launch harness=claude cwd=%s issue=%s",
            self._workspace,
            self._issue.identifier,
        )

        options = ClaudeAgentOptions(
            cwd=self._workspace,
            cli_path=self._config.claude_command or None,
            model=self._config.claude_model,
            system_prompt=self._config.claude_system_prompt or None,
            allowed_tools=self._config.claude_allowed_tools or [],
            permission_mode=self._config.claude_permission_mode,
        )

        try:
            self._client = ClaudeSDKClient(options)
        except CLINotFoundError as exc:
            raise AgentNotFoundError("claude", str(exc)) from exc
        except Exception as exc:
            raise AgentStartupError("claude", str(exc)) from exc

        self._started = True
        self._emit("session_started", message="Claude session started")

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Execute one turn via Claude SDK streaming."""
        if not self._started or not self._client:
            raise AgentStartupError("claude", "Session not started")

        self._session.turn_count += 1
        self._session.turn_id = f"turn-{turn_number}"
        self._session.session_id = f"{self._session.thread_id}-{self._session.turn_id}"

        turn_timeout = self._config.claude_turn_timeout_ms / 1000.0
        self._turn_error: str | None = None
        self._saw_result: bool = False

        try:
            await asyncio.wait_for(self._run_turn_impl(prompt, turn_number), timeout=turn_timeout)
        except TimeoutError:
            raise TurnTimeoutError() from None
        except asyncio.CancelledError:
            raise TurnCancelledError() from None
        except (TurnTimeoutError, TurnCancelledError, TurnFailedError, TurnInputRequiredError):
            raise
        except Exception as exc:
            # On turn 1, CLI startup errors should be classified properly
            if turn_number == 1:
                exc_class = type(exc).__name__
                if "NotFound" in exc_class or "Connection" in exc_class:
                    raise AgentNotFoundError("claude", str(exc)) from exc
            err_str = str(exc).lower()
            if "input" in err_str and "required" in err_str:
                raise TurnInputRequiredError() from exc
            raise TurnFailedError(str(exc)) from exc

        # If a streamed error was received, fail the turn
        if self._turn_error:
            err_lower = self._turn_error.lower()
            if "input" in err_lower and "required" in err_lower:
                raise TurnInputRequiredError()
            raise TurnFailedError(self._turn_error)

        # Stream ended without a ResultMessage — abnormal termination
        if not self._saw_result:
            raise TurnFailedError("Stream ended without result")

        self._emit("turn_completed", message=f"Turn {turn_number} completed")
        return True

    async def _run_turn_impl(self, prompt: str, turn_number: int) -> None:
        """Inner coroutine wrapped by wait_for for total turn timeout."""
        if turn_number == 1:
            await self._client.connect(prompt)
        else:
            await self._client.query(prompt, self._sdk_session_id)

        async for message in self._client.receive_messages():
            self._handle_claude_message(message)
            if self._is_result_message(message):
                self._saw_result = True
                break

    def _is_result_message(self, message: Any) -> bool:
        """Check if message is a ResultMessage (signals turn completion)."""
        return type(message).__name__ == "ResultMessage"

    def _handle_claude_message(self, message: Any) -> None:
        """Translate Claude SDK streaming messages into Symphony events."""
        self._session.last_copilot_timestamp = _now_utc()
        msg_class = type(message).__name__

        if msg_class == "ResultMessage":
            # Extract session_id for multi-turn continuations
            session_id = getattr(message, "session_id", None)
            if session_id:
                self._sdk_session_id = session_id
                self._session.thread_id = session_id
                # Recompute session_id now that thread_id is known
                self._session.session_id = f"{self._session.thread_id}-{self._session.turn_id}"

            # Check for errors
            is_error = getattr(message, "is_error", False)
            errors = getattr(message, "errors", None) or []
            if is_error or errors:
                error_msg = "; ".join(errors) if errors else "Agent reported error"
                self._turn_error = error_msg
                self._emit("turn_failed", error=error_msg, message=error_msg)
                return

            # Extract usage from result
            usage = getattr(message, "usage", None)
            if usage and isinstance(usage, dict):
                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                total = inp + out
                self._session.copilot_input_tokens = int(inp)
                self._session.copilot_output_tokens = int(out)
                self._session.copilot_total_tokens = int(total)
                self._emit(
                    "notification",
                    usage={
                        "input_tokens": int(inp),
                        "output_tokens": int(out),
                        "total_tokens": total,
                    },
                )

        elif msg_class == "AssistantMessage":
            # Check for assistant-level errors (rate limit, auth, etc.)
            error = getattr(message, "error", None)
            if error:
                self._turn_error = f"Claude error: {error}"
                self._emit("turn_failed", error=self._turn_error, message=self._turn_error)
                return

            # Extract text content
            content_blocks = getattr(message, "content", [])
            text_parts = []
            has_tool_use = False
            for block in content_blocks:
                block_class = type(block).__name__
                if block_class == "TextBlock":
                    text_parts.append(getattr(block, "text", ""))
                elif block_class == "ToolUseBlock":
                    has_tool_use = True

            if text_parts:
                content = " ".join(text_parts)[:200]
                self._session.last_copilot_message = content
                self._emit("notification", message=content)
            elif has_tool_use:
                self._emit("notification", message="Tool use in progress")

            # Extract usage from assistant message
            usage = getattr(message, "usage", None)
            if usage and isinstance(usage, dict):
                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                total = inp + out
                self._session.copilot_input_tokens = int(inp)
                self._session.copilot_output_tokens = int(out)
                self._session.copilot_total_tokens = int(total)

        elif msg_class == "RateLimitEvent":
            self._emit("notification", message="Rate limited — waiting")

        elif msg_class == "StreamEvent":
            # Progress signal from SDK — no action needed
            logger.debug("stream_event session=%s", getattr(message, "session_id", ""))

    async def stop(self) -> None:
        """Disconnect Claude SDK client."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._started = False
