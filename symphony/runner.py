"""Agent runner – harness-agnostic session lifecycle.

Provides ``run_agent_session()`` (the public entry point for the orchestrator)
and the ``CopilotHarness`` implementation using ``github-copilot-sdk``.
A factory (``_create_harness``) selects the active harness based on config.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from copilot import CopilotClient, SubprocessConfig
from copilot.session import PermissionHandler

from symphony.config import ServiceConfig
from symphony.errors import (
    ConfigValidationError,
    CopilotNotFoundError,
    InvalidWorkspaceCwdError,
    PortExitError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from symphony.harness import AgentHarness
from symphony.models import AgentEvent, Issue, LiveSession

logger = logging.getLogger("symphony.runner")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _make_session_id(thread_id: str, turn_id: str) -> str:
    return f"{thread_id}-{turn_id}"


class CopilotHarness:
    """Manages a live Copilot SDK session with multi-turn support.

    Wraps ``CopilotClient`` + ``CopilotSession`` and translates SDK events
    into Symphony ``AgentEvent`` objects for the orchestrator.

    Implements the ``AgentHarness`` protocol.
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
        self._client: CopilotClient | None = None
        self._sdk_session: Any = None  # CopilotSession from SDK
        self._session = LiveSession()
        self._started = False
        self._session_id_str: str = ""

    @property
    def session(self) -> LiveSession:
        return self._session

    def _emit(self, event_name: str, **kwargs: Any) -> None:
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

    def _handle_sdk_event(self, event: Any) -> None:
        """Process a SessionEvent from the SDK and update session state."""
        event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
        data = event.data

        # Update last-event timestamp
        self._session.last_copilot_event = event_type
        self._session.last_copilot_timestamp = _now_utc()

        # Extract token usage from usage events
        if event_type == "assistant.usage" and data:
            usage = getattr(data, "usage", None) or getattr(data, "content", None)
            if usage:
                inp = getattr(usage, "input_tokens", 0) or 0
                out = getattr(usage, "output_tokens", 0) or 0
                total = getattr(usage, "total_tokens", inp + out) or (inp + out)
                self._session.copilot_input_tokens = int(inp)
                self._session.copilot_output_tokens = int(out)
                self._session.copilot_total_tokens = int(total)
                self._emit(
                    "notification",
                    usage={
                        "input_tokens": int(inp),
                        "output_tokens": int(out),
                        "total_tokens": int(total),
                    },
                )

        elif event_type == "session.idle":
            self._emit("turn_completed", message="Session idle")

        elif event_type == "session.error":
            msg = str(getattr(data, "message", data)) if data else "unknown error"
            self._emit("turn_failed", error=msg, message=msg)

        elif event_type == "assistant.message":
            content = str(getattr(data, "content", "")) if data else ""
            self._session.last_copilot_message = content[:200]
            self._emit("notification", message=content[:200])

        elif event_type == "assistant.turn_start":
            self._emit("notification", message="Turn started")

        elif event_type == "assistant.turn_end":
            self._emit("notification", message="Turn ended")

        elif event_type == "session.usage_info":
            # Rate-limit or usage info
            if data:
                self._emit("notification", rate_limits={"data": str(data)})

    async def start(self) -> None:
        """Launch the Copilot SDK client and create a session."""
        if not os.path.isdir(self._workspace):
            raise InvalidWorkspaceCwdError(self._workspace, "does not exist")

        logger.info(
            "agent_launch cwd=%s issue=%s",
            self._workspace,
            self._issue.identifier,
        )

        # Build SubprocessConfig
        subprocess_cfg = SubprocessConfig(
            cwd=self._workspace,
            github_token=self._config.tracker_api_key or None,
            use_logged_in_user=True,
        )

        try:
            self._client = CopilotClient(subprocess_cfg)
            await self._client.start()
        except FileNotFoundError as exc:
            raise CopilotNotFoundError(str(exc)) from exc
        except Exception as exc:
            raise PortExitError(None) from exc

        # Create SDK session
        try:
            self._sdk_session = await self._client.create_session(
                on_permission_request=PermissionHandler.approve_all,
                working_directory=self._workspace,
                on_event=self._handle_sdk_event,
            )
        except Exception as exc:
            logger.error("session_create_failed issue=%s error=%s", self._issue.identifier, exc)
            await self.stop()
            raise PortExitError(None) from exc

        self._session.thread_id = str(getattr(self._sdk_session, "session_id", "") or "")
        self._session.session_id = self._session.thread_id
        self._started = True

        self._emit("session_started", message=f"Session {self._session.thread_id} created")

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Run one coding-agent turn using send_and_wait. Returns True on success."""
        if not self._started or not self._sdk_session:
            raise PortExitError(None)

        self._session.turn_count += 1
        self._session.turn_id = f"turn-{turn_number}"
        self._session.session_id = _make_session_id(self._session.thread_id, self._session.turn_id)

        turn_timeout = self._config.copilot_turn_timeout_ms / 1000.0

        try:
            result = await self._sdk_session.send_and_wait(
                prompt,
                timeout=turn_timeout,
            )
        except TimeoutError:
            raise TurnTimeoutError() from None
        except Exception as exc:
            err_str = str(exc).lower()
            if "cancel" in err_str:
                raise TurnCancelledError() from exc
            elif "input" in err_str and "required" in err_str:
                raise TurnInputRequiredError() from exc
            raise TurnFailedError(str(exc)) from exc

        # Check result event type if available
        if result:
            event_type = getattr(result.type, "value", "") if hasattr(result, "type") else ""
            if event_type == "session.error":
                msg = (
                    str(getattr(result.data, "message", result.data))
                    if result.data
                    else "turn failed"
                )
                raise TurnFailedError(msg)

        self._emit("turn_completed", message=f"Turn {turn_number} completed")
        return True

    async def stop(self) -> None:
        """Shut down the SDK session and client."""
        if self._sdk_session:
            try:
                await self._sdk_session.disconnect()
            except Exception:
                pass
            self._sdk_session = None

        if self._client:
            try:
                await self._client.stop()
            except Exception:
                pass
            self._client = None

        self._started = False


# Backward-compat alias
CopilotAgentSession = CopilotHarness


def _create_harness(
    config: ServiceConfig,
    workspace_path: str,
    issue: Issue,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> AgentHarness:
    """Factory: select harness based on config.agent_harness."""
    harness_name = config.agent_harness

    if harness_name == "copilot":
        return CopilotHarness(config, workspace_path, issue, on_event=on_event)
    elif harness_name == "claude":
        from symphony.claude_runner import ClaudeHarness

        return ClaudeHarness(config, workspace_path, issue, on_event=on_event)
    else:
        raise ConfigValidationError(f"Unknown agent harness: {harness_name!r}")


async def run_agent_session(
    config: ServiceConfig,
    workspace_path: str,
    issue: Issue,
    prompt: str,
    attempt: int | None,
    on_event: Callable[[AgentEvent], None] | None = None,
    max_turns: int = 20,
    fetch_issue_state: Callable[[str], Any] | None = None,
) -> LiveSession:
    """Run a full agent session: start, multi-turn loop, stop.

    Returns the final :class:`LiveSession` state.
    """
    session = _create_harness(config, workspace_path, issue, on_event=on_event)

    try:
        await session.start()
    except Exception as exc:
        logger.error("agent_startup_failed issue=%s error=%s", issue.identifier, exc)
        raise

    try:
        turn_number = 1
        current_prompt = prompt

        while True:
            await session.run_turn(current_prompt, turn_number)

            # Re-check issue state if callback is provided (spec §16.5)
            if fetch_issue_state:
                refreshed = await fetch_issue_state(issue.id)
                if refreshed and hasattr(refreshed, "state"):
                    issue_state = refreshed.state.lower()
                    active_states = config.active_states
                    if issue_state not in active_states:
                        logger.info(
                            "issue_no_longer_active issue=%s state=%s",
                            issue.identifier,
                            issue_state,
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
        await session.stop()

    return session.session
