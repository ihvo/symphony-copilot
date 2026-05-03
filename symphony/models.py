"""Domain model dataclasses for Symphony."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class BlockerRef:
    """A reference to a blocking issue."""

    id: str | None = None
    identifier: str | None = None
    state: str | None = None


@dataclass
class Issue:
    """Normalized issue record."""

    id: str
    identifier: str
    title: str
    description: str | None = None
    priority: int | None = None
    state: str = ""
    branch_name: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[BlockerRef] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_template_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for template rendering."""
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description or "",
            "priority": self.priority,
            "state": self.state,
            "branch_name": self.branch_name or "",
            "url": self.url or "",
            "labels": list(self.labels),
            "blocked_by": [
                {"id": b.id, "identifier": b.identifier, "state": b.state}
                for b in self.blocked_by
            ],
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
        }


@dataclass
class WorkflowDefinition:
    """Parsed WORKFLOW.md payload."""

    config: dict[str, Any]
    prompt_template: str


@dataclass
class Workspace:
    """Filesystem workspace assigned to one issue identifier."""

    path: str
    workspace_key: str
    created_now: bool = False


@dataclass
class LiveSession:
    """State tracked while a coding-agent subprocess is running."""

    session_id: str = ""
    thread_id: str = ""
    turn_id: str = ""
    copilot_pid: str | None = None
    last_copilot_event: str | None = None
    last_copilot_timestamp: datetime | None = None
    last_copilot_message: str = ""
    copilot_input_tokens: int = 0
    copilot_output_tokens: int = 0
    copilot_total_tokens: int = 0
    last_reported_input_tokens: int = 0
    last_reported_output_tokens: int = 0
    last_reported_total_tokens: int = 0
    turn_count: int = 0


@dataclass
class RunningEntry:
    """State for a currently running issue."""

    issue_id: str
    identifier: str
    issue: Issue
    session: LiveSession = field(default_factory=LiveSession)
    worker_task: Any = None  # asyncio.Task reference
    retry_attempt: int | None = None
    started_at: datetime | None = None
    state: str = ""  # tracked issue state for per-state concurrency


@dataclass
class RetryEntry:
    """Scheduled retry state for an issue."""

    issue_id: str
    identifier: str
    attempt: int = 1
    due_at_ms: float = 0.0  # monotonic clock
    timer_handle: Any = None  # asyncio.TimerHandle
    error: str | None = None


@dataclass
class CopilotTotals:
    """Aggregate token counts and runtime."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: float = 0.0


@dataclass
class RateLimitInfo:
    """Latest rate-limit snapshot from agent events."""

    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestratorState:
    """Single authoritative in-memory state owned by the orchestrator."""

    poll_interval_ms: int = 30000
    max_concurrent_agents: int = 10
    running: dict[str, RunningEntry] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_attempts: dict[str, RetryEntry] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    copilot_totals: CopilotTotals = field(default_factory=CopilotTotals)
    copilot_rate_limits: RateLimitInfo | None = None


# --- Agent runner events (worker -> orchestrator) ---


@dataclass
class AgentEvent:
    """Structured event emitted from agent runner to orchestrator."""

    event: str
    issue_id: str
    timestamp: datetime | None = None
    copilot_pid: str | None = None
    usage: dict[str, int] | None = None
    message: str = ""
    error: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    rate_limits: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


# --- Worker result ---


@dataclass
class WorkerResult:
    """Result of a worker attempt."""

    issue_id: str
    identifier: str
    success: bool
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    session: LiveSession = field(default_factory=LiveSession)
