"""EventBus-based streaming infrastructure for real-time SSE delivery.

Provides a publish/subscribe event bus with bounded ring-buffer history,
per-issue filtering, and atomic subscribe→replay handoff for reconnection.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("symphony.streaming")


@dataclass
class StreamEvent:
    """A serialized event ready for SSE transmission."""

    id: int  # Monotonic sequence number
    event_type: str  # SSE event: field (e.g., "agent_event", "session_ended")
    issue_id: str  # Which session this belongs to
    issue_identifier: str  # Human-readable key (e.g., "#42")
    timestamp: str  # ISO-8601
    data: dict[str, Any]  # Full event payload (JSON-serializable)


class EventBus:
    """In-memory publish/subscribe event bus with bounded history.

    - Publishes events to all subscribers (synchronous, non-blocking)
    - Maintains a global ring buffer for replay on connect
    - Maintains per-issue ring buffers for session-specific replay
    - Subscribers receive events via asyncio.Queue (overflow → disconnect)
    """

    def __init__(
        self,
        global_history_size: int = 1000,
        per_issue_history_size: int = 200,
        subscriber_queue_size: int = 256,
    ) -> None:
        self._global_history: deque[StreamEvent] = deque(maxlen=global_history_size)
        self._issue_history: dict[str, deque[StreamEvent]] = {}
        self._per_issue_size = per_issue_history_size
        self._subscriber_queue_size = subscriber_queue_size
        self._global_subscribers: set[asyncio.Queue[StreamEvent | None]] = set()
        self._issue_subscribers: dict[str, set[asyncio.Queue[StreamEvent | None]]] = {}
        self._seq = 0

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to all relevant subscribers (non-blocking).

        This method MUST NOT await. It runs synchronously on the
        orchestrator's event loop. All subscriber iteration uses snapshot copies
        to avoid mutation-during-iteration.

        Overflow policy: When a subscriber's queue reaches (maxsize - 1) items,
        the subscriber is removed and a None sentinel is placed in the last
        reserved slot to signal desync.
        """
        self._global_history.append(event)

        # Per-issue buffer
        if event.issue_id not in self._issue_history:
            self._issue_history[event.issue_id] = deque(maxlen=self._per_issue_size)
        self._issue_history[event.issue_id].append(event)

        # Fan-out to global subscribers (snapshot to avoid mutation during iteration)
        for q in tuple(self._global_subscribers):
            if q.qsize() >= q.maxsize - 1:
                self._global_subscribers.discard(q)
                q.put_nowait(None)  # Guaranteed: 1 slot reserved
            else:
                q.put_nowait(event)

        # Fan-out to issue-specific subscribers
        issue_subs = self._issue_subscribers.get(event.issue_id)
        if issue_subs:
            for q in tuple(issue_subs):
                if q.qsize() >= q.maxsize - 1:
                    issue_subs.discard(q)
                    q.put_nowait(None)  # Guaranteed: reserved slot
                else:
                    q.put_nowait(event)

    def next_id(self) -> int:
        """Generate the next monotonic event ID."""
        self._seq += 1
        return self._seq

    def subscribe_global(
        self, last_event_id: int | None = None
    ) -> tuple[list[StreamEvent] | None, asyncio.Queue[StreamEvent | None]]:
        """Subscribe to all events. Returns (replay_history, live_queue).

        ATOMIC HANDOFF: The subscriber is registered BEFORE replay is computed.
        This means live events arriving during replay may duplicate replay items,
        but no events are ever lost. The SSE generator deduplicates by ID.

        Returns replay=None if Last-Event-ID is older than retained history (gap).
        """
        q: asyncio.Queue[StreamEvent | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        # Register FIRST — ensures no events are missed between replay and live
        self._global_subscribers.add(q)

        # Then compute replay (may overlap with live events — that's fine)
        replay: list[StreamEvent] | None
        if last_event_id is not None:
            if self._global_history and self._global_history[0].id > last_event_id:
                replay = None  # Gap — stale Last-Event-ID
            else:
                replay = [e for e in self._global_history if e.id > last_event_id]
        else:
            replay = list(self._global_history)
        return replay, q

    def unsubscribe_global(self, q: asyncio.Queue[StreamEvent | None]) -> None:
        """Remove a global subscriber."""
        self._global_subscribers.discard(q)

    def subscribe_issue(
        self, issue_id: str, last_event_id: int | None = None
    ) -> tuple[list[StreamEvent] | None, asyncio.Queue[StreamEvent | None]]:
        """Subscribe to events for a specific issue. Returns (replay, queue).

        ATOMIC HANDOFF: Same pattern as subscribe_global — register first,
        replay second. Returns replay=None if Last-Event-ID is stale (gap).
        """
        q: asyncio.Queue[StreamEvent | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        if issue_id not in self._issue_subscribers:
            self._issue_subscribers[issue_id] = set()
        # Register FIRST
        self._issue_subscribers[issue_id].add(q)

        # Then compute replay
        replay: list[StreamEvent] | None
        history = self._issue_history.get(issue_id, deque())
        if last_event_id is not None:
            if history and history[0].id > last_event_id:
                replay = None  # Gap — stale Last-Event-ID
            else:
                replay = [e for e in history if e.id > last_event_id]
        else:
            replay = list(history)
        return replay, q

    def unsubscribe_issue(
        self, issue_id: str, q: asyncio.Queue[StreamEvent | None]
    ) -> None:
        """Remove an issue-specific subscriber."""
        subs = self._issue_subscribers.get(issue_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self._issue_subscribers[issue_id]

    def clear_issue(self, issue_id: str) -> None:
        """Clear history for an issue (call on session end/cleanup)."""
        self._issue_history.pop(issue_id, None)

    def resolve_identifier(self, identifier: str) -> str | None:
        """Resolve a human identifier to an issue_id from event history.

        Scans issue_history keys by checking stored events' issue_identifier.
        Returns None if no match found.
        """
        for issue_id, history in self._issue_history.items():
            if history and history[0].issue_identifier == identifier:
                return issue_id
        return None

    @property
    def subscriber_count(self) -> int:
        """Total number of active subscribers (global + per-issue)."""
        count = len(self._global_subscribers)
        for subs in self._issue_subscribers.values():
            count += len(subs)
        return count
