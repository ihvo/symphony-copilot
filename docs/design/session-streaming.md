# Session Streaming

**Status:** Proposed  
**Author:** Ihar / Copilot  
**Package:** symphony

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 0.1 | 2026-05-03 | Initial design |
| 0.2 | 2026-05-03 | Incorporated review feedback: atomic handoff, overflow signaling, full lifecycle events |

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Current Architecture](#2-current-architecture)
3. [Requirements](#3-requirements)
4. [Options Evaluation](#4-options-evaluation)
5. [Recommended Approach](#5-recommended-approach)
6. [Migration Plan](#6-migration-plan)
7. [Test Strategy](#7-test-strategy)
8. [Risk Assessment](#8-risk-assessment)
9. [Decision Records](#9-decision-records-adrs)

---

## 1. Problem Statement

Users operating Symphony have no way to observe an agent session in real-time. The current HTTP
server extension provides only point-in-time snapshots via polling (`GET /api/v1/state`) with a
10-second meta-refresh on the dashboard. This creates several pain points:

1. **No event history** — `AgentEvent` objects are consumed by the orchestrator and state is mutated,
   but individual events are discarded. A user connecting mid-session cannot see what happened before.

2. **No push-based streaming** — Clients must poll, which is either too slow (stale data) or too
   frequent (unnecessary load). There is no WebSocket or SSE endpoint.

3. **No per-session subscription** — The only view is system-wide. Users cannot focus on a single
   issue/session and follow its progress in real-time.

4. **Message truncation** — `last_copilot_message` stores only the first 200 chars of the latest
   message. Previous messages are lost entirely.

**Impact of not fixing:** As Symphony manages more concurrent agents, operators lose visibility into
what each agent is doing. Debugging failed runs requires log-file inspection rather than live
observation. External dashboards and tools cannot build on real-time event feeds.

---

## 2. Current Architecture

### Event Flow (today)

```
┌───────────────────────────┐
│  CopilotAgentSession      │
│  (runner.py)              │
│                           │
│  SDK events → _emit() ────┼──→ on_event callback
└───────────────────────────┘            │
                                         ▼
                              ┌─────────────────────┐
                              │  Orchestrator        │
                              │  _event_queue        │
                              │                     │
                              │  _process_events()  │
                              │    ├─ mutate state  │
                              │    └─ _notify_obs() │
                              └──────────┬──────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │  HTTP Server         │
                              │  (polling only)      │
                              │  GET /api/v1/state   │
                              └─────────────────────┘
```

### What exists today

| Component | Capability | Limitation |
|-----------|-----------|------------|
| `AgentEvent` dataclass | Rich structured event with timestamp, tokens, message | Events are consumed, not stored |
| `_notify_observers()` | Callback pattern for state changes | Fire-and-forget, no payload |
| `get_snapshot()` | Point-in-time state view | No history, must poll |
| Dashboard `/` | HTML with 10s meta-refresh | Not real-time |
| `LiveSession` | Tracks latest state | Only "last" values, no timeline |

### Key integration points

- `orchestrator.py:813` — `add_observer(callback)` already supports registering listeners
- `orchestrator.py:538-571` — `_handle_agent_event()` is the single place where events mutate state
- `server.py` — FastAPI app, easy to add SSE/WebSocket routes
- `models.py:155-170` — `AgentEvent` already has all fields needed for streaming

---

## 3. Requirements

### Must-have

| ID | Requirement |
|----|-------------|
| R1 | SSE endpoint that streams events for a specific session (by issue identifier) |
| R2 | On connect, replay recent event history so client sees what happened before connecting |
| R3 | Stream `AgentEvent` objects as structured JSON with consistent schema |
| R4 | In-memory bounded event ring buffer per session (no persistence requirement) |
| R5 | System-wide SSE endpoint that streams events across all sessions |
| R6 | Clean disconnection handling — no resource leaks on client drop |
| R7 | Compatible with existing observer pattern — no orchestrator mutations from streaming layer |

### Nice-to-have

| ID | Requirement |
|----|-------------|
| N1 | WebSocket alternative for bidirectional use cases (future) |
| N2 | Event filtering by type (e.g., only `turn_completed`, skip `notification`) |
| N3 | Persistent event log to disk for post-mortem analysis |
| N4 | Dashboard auto-updates via SSE instead of meta-refresh |

### Constraints

- MUST NOT affect orchestrator correctness (SPEC §13.7: "observability surfaces MUST NOT become
  REQUIRED for orchestrator correctness")
- MUST NOT introduce backpressure that slows agent execution
- MUST remain optional — if no clients are connected, zero overhead beyond event buffering
- MUST use the existing FastAPI server (no additional ports or processes)

---

## 4. Options Evaluation

### Option A: Server-Sent Events (SSE)

**Mechanism:** HTTP `text/event-stream` response that stays open. Server pushes events as they occur.

| Aspect | Assessment |
|--------|-----------|
| Simplicity | Very simple — standard HTTP, works through proxies, no special client library |
| Browser support | Native `EventSource` API in all browsers |
| Direction | Server → Client only (sufficient for observability) |
| Reconnection | Built-in `Last-Event-ID` header for replay on reconnect |
| Framework support | FastAPI `StreamingResponse` or `sse-starlette` package |
| Connection overhead | One long-lived HTTP connection per subscriber |

### Option B: WebSocket

**Mechanism:** Upgraded HTTP connection with full-duplex messaging.

| Aspect | Assessment |
|--------|-----------|
| Simplicity | More complex — upgrade handshake, frame protocol, ping/pong |
| Browser support | Native `WebSocket` API |
| Direction | Bidirectional (overkill for observability) |
| Reconnection | Must implement manually (no standard reconnect) |
| Framework support | FastAPI `WebSocket` class (Starlette) |
| Connection overhead | Similar to SSE but more protocol complexity |

### Option C: Long-polling with cursors

**Mechanism:** Client polls `GET /api/v1/events?since=<cursor>`, server holds request until new events arrive or timeout.

| Aspect | Assessment |
|--------|-----------|
| Simplicity | Simple API, complex server-side wait logic |
| Browser support | Plain `fetch()` in a loop |
| Direction | Pull-based (client controls timing) |
| Reconnection | Implicit via cursor |
| Framework support | Manual asyncio event/condition pattern |
| Connection overhead | Repeated TCP connections (HTTP/1.1) or single (HTTP/2) |

### Comparison Matrix

| Criterion | SSE | WebSocket | Long-poll |
|-----------|-----|-----------|-----------|
| Implementation complexity | Low | Medium | Medium |
| Real-time latency | ~0ms | ~0ms | 0-timeout |
| Proxy/firewall compatibility | High | Medium | High |
| Auto-reconnect | Built-in | Manual | Manual |
| Resource efficiency | Good | Good | Moderate |
| Bidirectional | No | Yes | No |
| Codebase fit (FastAPI) | Natural | Natural | Awkward |

### Recommendation: **Option A — Server-Sent Events (SSE)**

SSE is the right tool for this job. We only need server→client push for observability. SSE gives us
built-in reconnection with `Last-Event-ID`, trivial client-side code (`EventSource`), and clean
integration with FastAPI's async generators. WebSocket is overkill (we don't need client→server
messages) and long-polling adds unnecessary complexity.

---

## 5. Recommended Approach

### Architecture Overview

```
┌───────────────────────────┐
│  CopilotAgentSession      │
│  (runner.py)              │
│  _emit() → on_event ─────┼───┐
└───────────────────────────┘   │
                                ▼
                    ┌───────────────────────┐
                    │  Orchestrator         │
                    │  _process_events()    │
                    │    ├─ mutate state    │
                    │    ├─ _notify_obs()   │
                    │    └─ _publish(evt) ──┼───┐  NEW: publish to event bus
                    └───────────────────────┘   │
                                                ▼
                              ┌────────────────────────────┐
                              │  EventBus (streaming.py)    │
                              │                            │
                              │  ┌──────────────────────┐  │
                              │  │ Ring Buffer (global)  │  │
                              │  │ maxlen=1000           │  │
                              │  └──────────────────────┘  │
                              │                            │
                              │  ┌──────────────────────┐  │
                              │  │ Ring Buffer (per-issue)│  │
                              │  │ maxlen=200 each       │  │
                              │  └──────────────────────┘  │
                              │                            │
                              │  subscribers: set[Queue]   │
                              └─────────────┬──────────────┘
                                            │
                    ┌───────────────────────┬┴──────────────────────┐
                    ▼                       ▼                       ▼
          ┌─────────────────┐   ┌─────────────────┐    ┌─────────────────┐
          │ SSE client #1   │   │ SSE client #2   │    │ Dashboard (SSE) │
          │ /stream/MT-649  │   │ /stream          │    │ replaces polling│
          └─────────────────┘   └─────────────────┘    └─────────────────┘
```

### Component Design

#### 5.1 EventBus (`symphony/streaming.py` — new file)

The EventBus is a publish/subscribe broker that buffers events and fans them out to SSE clients.

```python
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("symphony.streaming")

# Monotonically increasing event ID for SSE Last-Event-ID support
_next_id: int = 0


@dataclass
class StreamEvent:
    """A serialized event ready for SSE transmission."""

    id: int                         # Monotonic sequence number
    event_type: str                 # SSE event: field (e.g., "agent_event", "state_change")
    issue_id: str                   # Which session this belongs to
    issue_identifier: str           # Human-readable key
    timestamp: str                  # ISO-8601
    data: dict[str, Any]            # Full event payload (JSON-serializable)


class EventBus:
    """In-memory publish/subscribe event bus with bounded history.

    - Publishes AgentEvents to all subscribers
    - Maintains a global ring buffer for replay on connect
    - Maintains per-issue ring buffers for session-specific replay
    - Subscribers receive events via asyncio.Queue (backpressure-safe)
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
        # Reserve 1 slot in queue for the overflow sentinel (None).
        # Events use slots 0..(maxsize-2), sentinel uses the last slot.
        self._subscriber_queue_size = subscriber_queue_size
        self._global_subscribers: set[asyncio.Queue[StreamEvent | None]] = set()
        self._issue_subscribers: dict[str, set[asyncio.Queue[StreamEvent | None]]] = {}
        self._seq = 0

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to all relevant subscribers (non-blocking).

        IMPORTANT: This method MUST NOT await. It runs synchronously on the
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
                # Queue is at capacity (minus reserved sentinel slot) — disconnect
                self._global_subscribers.discard(q)
                q.put_nowait(None)  # Guaranteed to succeed: 1 slot reserved
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
    ) -> tuple[list[StreamEvent], asyncio.Queue[StreamEvent | None]]:
        """Subscribe to all events. Returns (replay_history, live_queue).

        ATOMIC HANDOFF: The subscriber is registered BEFORE replay is computed.
        This means live events arriving during replay may duplicate replay items,
        but no events are ever lost. The SSE generator deduplicates by ID.
        """
        q: asyncio.Queue[StreamEvent | None] = asyncio.Queue(
            maxsize=self._subscriber_queue_size
        )
        # Register FIRST — ensures no events are missed between replay and live
        self._global_subscribers.add(q)

        # Then compute replay (may overlap with live events — that's fine)
        replay = []
        if last_event_id is not None:
            if self._global_history and self._global_history[0].id > last_event_id:
                # Requested ID is older than oldest retained — signal gap
                replay = None  # Caller should send a "gap" event to client
            else:
                replay = [e for e in self._global_history if e.id > last_event_id]
        else:
            replay = list(self._global_history)
        return replay, q

    def unsubscribe_global(self, q: asyncio.Queue[StreamEvent | None]) -> None:
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
        replay = []
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
        subs = self._issue_subscribers.get(issue_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self._issue_subscribers[issue_id]

    def clear_issue(self, issue_id: str) -> None:
        """Clear history for an issue (call on session end/cleanup)."""
        self._issue_history.pop(issue_id, None)
```

#### 5.2 Integration with Orchestrator

Minimal change — add `publish()` calls at **all state mutation points**, not just AgentEvent
handling. The orchestrator emits stream events whenever the system state changes observably:

```python
# orchestrator.py — EventBus integration points:

# 1. In _handle_agent_event() — after existing state mutations:
def _handle_agent_event(self, event: AgentEvent) -> None:
    entry = self._state.running.get(event.issue_id)
    if not entry:
        return
    # ... existing state mutation code ...
    self._publish_stream_event(event.event, entry, event)

# 2. In _handle_worker_exit() — session ended (success or failure):
def _handle_worker_exit(self, result: WorkerResult) -> None:
    entry = self._state.running.pop(result.issue_id, None)
    # ... existing completion/retry logic ...
    if self._event_bus and entry:
        self._event_bus.publish(StreamEvent(
            id=self._event_bus.next_id(),
            event_type="session_ended",
            issue_id=result.issue_id,
            issue_identifier=result.identifier,
            timestamp=datetime.now(timezone.utc).isoformat(),
            data={
                "event": "session_ended",
                "success": result.success,
                "error": result.error,
                "turns": result.session.turn_count,
                "tokens": {
                    "input_tokens": result.session.copilot_input_tokens,
                    "output_tokens": result.session.copilot_output_tokens,
                    "total_tokens": result.session.copilot_total_tokens,
                },
            },
        ))

# 3. In _schedule_retry() — retry queued:
def _schedule_retry(self, issue_id, attempt, identifier, error, delay_ms):
    # ... existing retry logic ...
    if self._event_bus:
        self._event_bus.publish(StreamEvent(
            id=self._event_bus.next_id(),
            event_type="retry_scheduled",
            issue_id=issue_id,
            issue_identifier=identifier,
            timestamp=datetime.now(timezone.utc).isoformat(),
            data={
                "event": "retry_scheduled",
                "attempt": attempt,
                "error": error,
                "delay_ms": delay_ms,
            },
        ))

# Helper method on orchestrator:
def _publish_stream_event(self, event_type: str, entry: RunningEntry, event: AgentEvent) -> None:
    """Publish to event bus. Failures are logged but never crash the orchestrator."""
    if not self._event_bus:
        return
    try:
        self._event_bus.publish(StreamEvent(
            id=self._event_bus.next_id(),
            event_type=event_type,
            issue_id=event.issue_id,
            issue_identifier=entry.identifier,
            timestamp=event.timestamp.isoformat() if event.timestamp else "",
            data={
                "event": event.event,
                "message": event.message,
                "error": event.error,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "usage": event.usage,
                "rate_limits": event.rate_limits,
            },
        ))
    except Exception:
        logger.error("event_bus_publish_failed", exc_info=True)
```

The `EventBus` instance is created by the orchestrator (or injected from CLI) and passed to the
server. If no server is started, the event bus is simply never created — zero overhead.

**Lifecycle events published:**

| Source | Event Type | When |
|--------|-----------|------|
| `_handle_agent_event()` | `session_started`, `notification`, `turn_completed`, `turn_failed` | Any agent event |
| `_handle_worker_exit()` | `session_ended` | Worker finishes (success or failure) |
| `_handle_worker_exit()` | `session_cancelled` | Worker cancelled (issue closed mid-run) |
| `_schedule_retry()` | `retry_scheduled` | Retry queued with delay |
| `_dispatch()` | `session_dispatched` | New session started for an issue |
| `_reconcile()` | `session_killed` | Stale session removed during reconciliation |

**Last-Event-ID edge cases:**
- `Last-Event-ID < oldest_buffered_id` → `gap` event sent, then live stream continues
- `Last-Event-ID > newest_id` → Empty replay, only new events streamed (client ahead after restart)
- `Last-Event-ID = newest_id` → Empty replay, live events only (normal reconnect with no gap)

#### 5.3 SSE Endpoints in Server

Two new routes added to `server.py`:

```python
# GET /api/v1/stream — global event stream (all sessions)
# GET /api/v1/stream/{identifier} — per-issue event stream

from starlette.responses import StreamingResponse

async def _sse_generator(
    queue: asyncio.Queue[StreamEvent | None],
    replay: list[StreamEvent] | None,
    unsubscribe: Callable[[], None],
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events from replay + live queue.

    Handles:
    - Gap detection (replay=None means Last-Event-ID is stale)
    - Replay deduplication (live queue may contain replay items)
    - Overflow sentinel (None in queue = desynced, close stream)
    - Keepalive comments every 30s
    - Cleanup via finally block
    """
    last_sent_id = 0
    try:
        # Gap detection — client's Last-Event-ID is older than our buffer
        if replay is None:
            yield 'event: gap\ndata: {"reason":"history_expired"}\n\n'
            # Still stream live events going forward
        else:
            # Phase 1: Replay history
            for event in replay:
                last_sent_id = event.id
                yield _format_sse(event)

        # Phase 2: Live stream
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue

            if event is None:
                # Overflow sentinel — subscriber was desynced
                yield 'event: overflow\ndata: {"reason":"slow_consumer"}\n\n'
                break

            # Deduplicate: skip events already sent during replay
            if event.id <= last_sent_id:
                continue

            last_sent_id = event.id
            yield _format_sse(event)

    except asyncio.CancelledError:
        pass  # Client disconnected — normal cleanup
    finally:
        try:
            unsubscribe()
        except Exception:
            pass  # Best-effort cleanup; never propagate from finally


def _format_sse(event: StreamEvent) -> str:
    """Format a StreamEvent as an SSE message."""
    data_json = json.dumps(event.data, default=str)
    return (
        f"id: {event.id}\n"
        f"event: {event.event_type}\n"
        f"data: {data_json}\n\n"
    )


# Route handlers:

async def handle_stream_global(request: Request) -> StreamingResponse:
    last_id = _parse_last_event_id(request)
    replay, queue = event_bus.subscribe_global(last_event_id=last_id)
    return StreamingResponse(
        _sse_generator(queue, replay, lambda: event_bus.unsubscribe_global(queue)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def handle_stream_issue(request: Request, identifier: str) -> StreamingResponse:
    # Resolve identifier to issue_id via orchestrator.
    # New method on Orchestrator: scans running + retry_attempts for matching identifier.
    # Returns issue_id or None. This is a read-only scan of in-memory state.
    issue_id = orchestrator.resolve_identifier(identifier)
    if not issue_id:
        # Also check event bus history — issue may have ended but events are buffered
        issue_id = event_bus.resolve_identifier(identifier)
    if not issue_id:
        return _error_response("issue_not_found", f"Issue {identifier} not found", 404)

    last_id = _parse_last_event_id(request)
    replay, queue = event_bus.subscribe_issue(issue_id, last_event_id=last_id)
    return StreamingResponse(
        _sse_generator(
            queue, replay, lambda: event_bus.unsubscribe_issue(issue_id, queue)
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _parse_last_event_id(request: Request) -> int | None:
    raw = request.headers.get("Last-Event-ID")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
```

#### 5.4 SSE Protocol Details

**Wire format (per SSE spec):**
```
id: 42
event: notification
data: {"event":"notification","message":"Installing dependencies...","session_id":"t1-turn-3","usage":null}

id: 43
event: turn_completed
data: {"event":"turn_completed","message":"Turn 3 complete","session_id":"t1-turn-3","usage":{"input_tokens":1200,"output_tokens":800}}

: keepalive

```

**Special control events (no `id` field — not replayable):**

| Event | Meaning | Client Action |
|-------|---------|---------------|
| `gap` | Client's `Last-Event-ID` is older than buffer | History is incomplete; display warning |
| `overflow` | Client was too slow, events were dropped | Reconnect immediately to resync |

**Reconnection:** When a client reconnects, the browser sends `Last-Event-ID: 43` header. The
server replays events with `id > 43` from the ring buffer before switching to live stream.
If the requested ID is older than the oldest event in the buffer, a `gap` event is sent first.

**Deduplication:** Because the subscribe→replay→live handoff registers the subscriber before
computing replay, live events may arrive that overlap with replay. The SSE generator tracks
`last_sent_id` and skips any event with `id <= last_sent_id`.

**Keepalive:** A comment line (`: keepalive\n\n`) is sent every 30 seconds to prevent intermediate
proxies from closing idle connections.

**Payload size:** Event `data` fields SHOULD be kept small. The `message` field is truncated
to 500 characters. Large payloads (e.g., full tool outputs) are not included — use the
`GET /api/v1/{identifier}` endpoint for full session detail.

#### 5.5 Client Usage Examples

**Browser (EventSource):**
```javascript
// Subscribe to a specific session
const source = new EventSource('/api/v1/stream/MT-649');

source.addEventListener('notification', (e) => {
  const data = JSON.parse(e.data);
  console.log(`[${data.event}] ${data.message}`);
});

source.addEventListener('turn_completed', (e) => {
  const data = JSON.parse(e.data);
  console.log(`Turn complete! Tokens: ${data.usage?.total_tokens}`);
});

// Auto-reconnects with Last-Event-ID on disconnect
```

**curl:**
```bash
# Stream all events
curl -N http://localhost:8080/api/v1/stream

# Stream events for a specific issue
curl -N http://localhost:8080/api/v1/stream/MT-649

# Reconnect from event 42
curl -N -H "Last-Event-ID: 42" http://localhost:8080/api/v1/stream/MT-649
```

**Python (httpx-sse):**
```python
import httpx
from httpx_sse import connect_sse

with httpx.Client() as client:
    with connect_sse(client, "GET", "http://localhost:8080/api/v1/stream/MT-649") as sse:
        for event in sse.iter_sse():
            print(f"[{event.event}] {event.data}")
```

### Key Design Decisions

1. **Event bus is optional** — Only instantiated when server extension is enabled. No overhead
   if Symphony runs without `--port`.

2. **Bounded ring buffers** — 1000 global, 200 per-issue. Events beyond this are dropped from
   history. This bounds memory usage regardless of session duration. Payloads are truncated
   (message ≤ 500 chars) to prevent memory growth from large events.

3. **Non-blocking fan-out with overflow disconnect** — `put_nowait()` with `QueueFull` handling.
   On overflow, the subscriber is removed and sent a `None` sentinel (which triggers an `overflow`
   SSE event and stream close). This is better than silent drops because the client knows to
   reconnect.

4. **Monotonic IDs** — Simple integer sequence. No need for UUIDs or timestamps as IDs since
   the event bus is in-memory and resets on restart.

5. **Per-issue subscription** — The common use case is watching one session. Per-issue buffers
   and subscriber sets make this efficient without filtering the global stream.

6. **Atomic subscribe→replay handoff** — Subscriber is registered BEFORE replay is computed.
   Live events may duplicate replay items, but deduplication by `last_sent_id` ensures no
   event is ever missed and no event is ever delivered twice.

7. **Full lifecycle coverage** — Stream events are published from all orchestrator state mutation
   points: agent events, worker exit, retry scheduling, and dispatch. Clients see the complete
   session lifecycle without gaps.

8. **Identifier resolution at publish time** — Both `issue_id` and `issue_identifier` are
   captured from authoritative orchestrator state when the event is published, not resolved
   later from potentially-stale data.

---

## 6. Migration Plan

### Phase 1: EventBus + SSE Endpoints

**Scope:** Core streaming infrastructure.

**Changes:**
1. Create `symphony/streaming.py` with `EventBus` and `StreamEvent`
2. Add `_event_bus: EventBus | None` to `Orchestrator.__init__`
3. Add `publish()` call in `_handle_agent_event()` and `_handle_worker_exit()`
4. Add SSE routes to `server.py`: `GET /api/v1/stream` and `GET /api/v1/stream/{identifier}`
5. Wire up: server creates EventBus, passes to orchestrator (or orchestrator creates and shares)

**Verification:**
- Unit tests for EventBus (publish, subscribe, replay, unsubscribe)
- Integration test: start orchestrator + server, connect SSE client, verify events arrive

### Phase 2: Dashboard Enhancement

**Scope:** Replace meta-refresh with SSE-powered live updates.

**Changes:**
1. Add JavaScript to dashboard HTML that connects to `/api/v1/stream`
2. Update DOM on incoming events (tokens, turn count, last message)
3. Remove `<meta http-equiv="refresh">` — no more full-page reloads

**Verification:**
- Manual testing of dashboard live updates
- Existing dashboard tests still pass (initial render is unchanged)

### Phase 3: Event Enrichment (Nice-to-have)

**Scope:** Add richer event types for better observability.

**Changes:**
1. Emit `session_ended` event with summary (total turns, total tokens, duration)
2. Emit `retry_scheduled` event when retry is queued
3. Add `?filter=turn_completed,turn_failed` query param for event filtering (N2)

---

## 7. Test Strategy

### Unit Tests (`tests/test_streaming.py`)

```python
# EventBus core behavior
def test_publish_delivers_to_global_subscribers():
    """Event published → appears in global subscriber queue."""

def test_publish_delivers_to_issue_subscribers():
    """Event published → appears in matching issue subscriber queue only."""

def test_subscribe_replays_history():
    """New subscriber receives buffered events as replay."""

def test_subscribe_with_last_event_id_replays_from_cursor():
    """Replay starts after the specified Last-Event-ID."""

def test_subscribe_stale_last_event_id_returns_none_replay():
    """If Last-Event-ID is older than buffer, replay is None (gap signal)."""

def test_overflow_disconnects_subscriber():
    """Full queue → subscriber removed, None sentinel sent."""

def test_atomic_handoff_no_lost_events():
    """Events published between subscribe and replay consumption are not lost."""

def test_deduplication_in_sse_generator():
    """Events with id <= last_sent_id are skipped (replay/live overlap)."""

def test_unsubscribe_removes_queue():
    """After unsubscribe, no more events delivered."""

def test_ring_buffer_bounds_memory():
    """Publishing beyond maxlen evicts oldest events."""

def test_clear_issue_removes_history():
    """clear_issue() wipes per-issue buffer."""

def test_publish_does_not_await():
    """publish() is synchronous — never yields control to event loop."""

def test_publish_exception_does_not_propagate():
    """If publish() has an internal bug, orchestrator catch-all prevents crash."""

def test_overflow_sentinel_always_delivered():
    """When queue reaches capacity-1, sentinel is placed in reserved slot."""
```

### Integration Tests (`tests/integration/test_streaming.py`)

```python
async def test_sse_stream_receives_agent_events():
    """Start orchestrator + server, trigger agent run, verify SSE client gets events."""

async def test_sse_stream_receives_lifecycle_events():
    """Verify session_ended and retry_scheduled events arrive via SSE."""

async def test_sse_replay_on_connect():
    """Events emitted before client connects are replayed on connection."""

async def test_sse_per_issue_stream():
    """Connecting to /stream/{id} only receives events for that issue."""

async def test_sse_reconnect_with_last_event_id():
    """Client reconnects with Last-Event-ID, gets only newer events."""

async def test_sse_gap_on_stale_last_event_id():
    """Client with very old Last-Event-ID gets gap event."""

async def test_sse_overflow_disconnects_slow_client():
    """Simulate slow consumer — verify overflow event and stream close."""

async def test_sse_keepalive():
    """After 30s idle, server sends keepalive comment."""

async def test_client_disconnect_cleanup():
    """When SSE client disconnects, subscriber queue is cleaned up."""

async def test_orchestrator_continues_on_event_bus_failure():
    """If EventBus.publish() raises, orchestrator event loop continues normally."""

async def test_sse_disconnect_during_replay():
    """Client disconnects mid-replay — generator cleanup runs without error."""
```

### Test Infrastructure Additions

- Add `httpx-sse` as a test dependency for consuming SSE in integration tests
- Use `fake_github` + `mock_agent` fixtures as-is (behaviors: `success`, `fail`)
- Verify event delivery by collecting events via `httpx_sse.connect_sse()`

---

## 8. Risk Assessment

### Risks of Implementing

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Memory growth from unbounded subscribers | Medium | Bounded queues with overflow-disconnect policy; cleanup in `finally` |
| Slow subscriber blocking event loop | Low | `put_nowait()` — never blocks; overflow removes subscriber |
| SSE connection leak on abnormal disconnect | Medium | `finally` block in generator ensures `unsubscribe()`; `CancelledError` caught |
| Added complexity to orchestrator | Low | Single helper method `_publish_stream_event()`; EventBus is fully decoupled |
| Replay/live deduplication overhead | Low | Simple integer comparison per event; negligible CPU cost |
| Payload memory from large events | Low | Fields truncated at publish time (message ≤ 500 chars) |
| No external dependency needed | — | Uses FastAPI `StreamingResponse` with `text/event-stream` — no `sse-starlette` |

### Risks of NOT Implementing

| Risk | Severity |
|------|----------|
| Operators lack real-time visibility into agent behavior | High |
| Debugging failed sessions requires log-file inspection | High |
| Dashboard remains polling-based and wasteful | Medium |
| Cannot build external tooling (CLI watchers, Slack bots) on event feeds | Medium |

---

## 8.1 Multi-SDK Implications (Claude SDK Support)

The spec (§10) is written around "the targeted Copilot SDK" but uses language that implies
SDK-swappability. Adding Claude SDK (Anthropic) support has specific implications for the
streaming design:

### Why the streaming design is already SDK-agnostic

The EventBus operates at the **normalized event layer** — it publishes `StreamEvent` objects
derived from `AgentEvent`, which is Symphony's internal event vocabulary (SPEC §10.4):

```
┌─────────────────────┐     ┌─────────────────────┐
│ CopilotAgentSession │     │ ClaudeAgentSession  │   (future)
│                     │     │                     │
│ SDK events:         │     │ SDK events:         │
│ • assistant.usage   │     │ • content_block_*   │
│ • session.idle      │     │ • message_start     │
│ • assistant.message │     │ • message_delta     │
│ • session.error     │     │ • message_stop      │
└────────┬────────────┘     └────────┬────────────┘
         │ _emit()                   │ _emit()
         ▼                           ▼
    ┌──────────────────────────────────────┐
    │  AgentEvent (normalized vocabulary)  │
    │  • session_started                   │
    │  • turn_completed                    │
    │  • turn_failed                       │
    │  • notification (+ message/usage)    │
    └──────────────────┬───────────────────┘
                       │
                       ▼
              ┌────────────────┐
              │   EventBus     │  ← SDK-agnostic
              │   StreamEvent  │
              └────────────────┘
```

**The runner is the normalization adapter.** Each SDK runner translates its SDK-specific event
types into Symphony's standard `AgentEvent` vocabulary. The EventBus never sees raw SDK events.

### What changes with Claude SDK (does NOT affect streaming)

| Concern | Impact on Streaming | Why |
|---------|-------------------|-----|
| Different event type names (`content_block_delta` vs `assistant.message`) | None | Runner normalizes to `notification` before publish |
| Different token reporting shape | None | Runner normalizes to `usage: {input_tokens, output_tokens, total_tokens}` |
| Different session/thread ID format | None | `session_id` is opaque string — EventBus doesn't parse it |
| No subprocess (HTTP API instead of stdio) | None | Runner abstracts transport; AgentEvent interface unchanged |
| Different approval/permission model | None | Handled by runner, not streaming |

### What changes with Claude SDK (DOES affect streaming — enrichment opportunity)

| Concern | Impact | Design Consideration |
|---------|--------|---------------------|
| Richer streaming content (Claude streams token-by-token) | Optional | Could emit higher-frequency `notification` events with partial content. Buffer sizing may need tuning. |
| Model identification | Additive | `StreamEvent.data` could include `"model": "claude-3.5-sonnet"` or `"copilot"` for observability |
| Cost tracking (different token pricing per model) | Additive | `usage` dict could include `"model"` field for cost attribution |
| Runner selection config (`agent.kind: copilot \| claude`) | Config layer | Streaming doesn't care which runner was selected — same event format |

### Design principle: StreamEvent carries SDK provenance but not SDK coupling

To support multi-SDK observability without coupling the streaming layer:

```python
# StreamEvent.data includes optional provenance field:
{
    "event": "notification",
    "message": "Installing dependencies...",
    "session_id": "thread-abc-turn-1",
    "runner": "copilot",       # or "claude" — informational only
    "model": "gpt-4o",        # or "claude-sonnet-4-20250514" — if available
    "usage": {"input_tokens": 500, "output_tokens": 200, "total_tokens": 700},
}
```

The `runner` and `model` fields are optional enrichment. SSE clients can display them in UI
but MUST NOT depend on them for protocol correctness. The EventBus treats them as opaque
payload data.

### Conclusion

**The session streaming design requires zero changes to support Claude SDK.** The architectural
decision to publish at the `AgentEvent` level (post-normalization) means the EventBus and SSE
endpoints are inherently multi-SDK. The only work needed is implementing a `ClaudeAgentSession`
runner that normalizes Claude SDK events into the same `AgentEvent` format — which is a runner
concern, not a streaming concern.

---

## 9. Decision Records (ADRs)

### ADR-1: SSE over WebSocket

**Context:** Need real-time event delivery to clients.  
**Decision:** Use Server-Sent Events (SSE) via `text/event-stream`.  
**Rationale:**
- Unidirectional (server→client) is sufficient for observability
- Built-in reconnection with `Last-Event-ID` in the browser
- Simpler implementation (no upgrade handshake, no frame protocol)
- Works through HTTP proxies without special configuration
- No external dependency — FastAPI `StreamingResponse` is sufficient

**Consequences:**
- Cannot receive commands from clients over the same connection (use REST for that)
- If bidirectional streaming is needed later, can add WebSocket alongside SSE

### ADR-2: In-Memory Ring Buffer (No Persistence)

**Context:** Need event history for replay on client connect.  
**Decision:** Use `collections.deque(maxlen=N)` — bounded in-memory buffer.  
**Rationale:**
- Events are transient operational data, not audit records
- Memory is bounded regardless of session duration
- No disk I/O in the hot path
- On restart, event history is acceptably lost (sessions restart anyway)

**Consequences:**
- Events older than buffer capacity cannot be replayed
- Post-mortem analysis requires separate log files (existing capability)
- If persistence is needed later (N3), can add a write-behind to disk without changing the API

### ADR-3: EventBus Decoupled from Orchestrator

**Context:** Streaming must not affect orchestrator correctness (SPEC §13.7).  
**Decision:** EventBus is a separate object; orchestrator publishes to it but never reads from it.  
**Rationale:**
- Preserves the "single authority" invariant — orchestrator owns state mutations
- EventBus failure (e.g., OOM) cannot crash or stall the orchestrator
- EventBus is created only when server extension is enabled

**Consequences:**
- Slight duplication: event data exists in both OrchestratorState and EventBus buffer
- This is intentional — the EventBus is a derived projection, not source of truth

### ADR-4: Non-Blocking Fan-Out with Overflow Disconnect

**Context:** Slow SSE clients must not slow down agent execution.  
**Decision:** Use `queue.put_nowait()` and disconnect subscribers that overflow.  
**Rationale:**
- Agent execution and orchestrator event processing are latency-sensitive
- Silent drops leave clients unaware they're desynced — unacceptable for observability
- Sending a `None` sentinel triggers an `overflow` SSE event, cleanly closing the stream
- `EventSource` in browsers auto-reconnects, replaying from `Last-Event-ID`
- Disconnected subscribers are immediately removed (no accumulation)

**Consequences:**
- Clients will occasionally reconnect on high-throughput bursts
- The ring buffer provides replay on reconnect — no data loss unless buffer overflows too

### ADR-5: Atomic Subscribe→Replay Handoff

**Context:** Race condition between registering a subscriber and computing replay history.  
**Decision:** Register subscriber FIRST, compute replay SECOND, deduplicate in the generator.  
**Rationale:**
- If we replay first and then subscribe, events arriving between those two steps are lost forever
- Subscribing first means live events may overlap with replay — trivially handled by skipping
  events with `id <= last_sent_id`
- This is a well-known pattern for gap-free streaming (same approach used by Redis Streams, NATS)

**Consequences:**
- Generator must track `last_sent_id` and skip duplicates — minimal overhead
- Slightly more events in the queue initially (duplicates are discarded quickly)

### ADR-6: Full Lifecycle Event Coverage

**Context:** Review identified that publishing only from `_handle_agent_event()` misses important
state transitions (worker exit, retry scheduling, dispatch).  
**Decision:** Publish stream events from ALL orchestrator state mutation points.  
**Rationale:**
- Users watching a session need to see it start, progress, end, and retry — not just mid-session
  agent chatter
- The `session_ended` event carries summary data (total turns, tokens, success/failure) that is
  the most important signal for operators
- `retry_scheduled` events let operators understand why sessions are delayed

**Consequences:**
- More publish call sites in orchestrator (4 vs 1) — still minimal (one-liner helper)
- Richer event stream that tells the complete story of each session
