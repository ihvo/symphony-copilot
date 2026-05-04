"""Agent harness protocol — the contract all harnesses implement."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from symphony.models import LiveSession


@runtime_checkable
class AgentHarness(Protocol):
    """Protocol for agent session harnesses.

    Implementors MUST satisfy these behavioral invariants:
    - Call on_event(AgentEvent) for: session_started, turn_completed, turn_failed, notification
    - Update session.last_copilot_timestamp on every SDK event (for stall detection)
    - Update session.copilot_input_tokens / copilot_output_tokens on usage events
    - Update session.turn_count in run_turn() before executing
    - Set session.thread_id and session.session_id in start()
    - Raise only AgentError subclasses (never raw SDK exceptions)
    """

    @property
    def session(self) -> LiveSession:
        """Return the current session state."""
        ...

    async def start(self) -> None:
        """Initialize and launch the agent subprocess.

        Raises:
            AgentNotFoundError: Binary/SDK not available.
            AgentStartupError: Subprocess failed to start.
            InvalidWorkspaceCwdError: Workspace path invalid.
        """
        ...

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Execute one agent turn. Returns True on success.

        Raises:
            TurnTimeoutError: Turn exceeded timeout.
            TurnFailedError: Turn failed for any reason.
            TurnCancelledError: Turn was cancelled.
            TurnInputRequiredError: Agent requires user input.
        """
        ...

    async def stop(self) -> None:
        """Shut down the agent session and subprocess. Must not raise."""
        ...
