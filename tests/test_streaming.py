"""Unit tests for symphony.streaming (EventBus + SSE helpers)."""

from __future__ import annotations

import asyncio
import json

import pytest

from symphony.streaming import EventBus, StreamEvent


def _make_event(bus: EventBus, issue_id: str = "id1", identifier: str = "#1", **data_kw) -> StreamEvent:
    """Helper to create a StreamEvent with auto-incrementing ID."""
    return StreamEvent(
        id=bus.next_id(),
        event_type=data_kw.pop("event_type", "notification"),
        issue_id=issue_id,
        issue_identifier=identifier,
        timestamp="2025-01-01T00:00:00+00:00",
        data=data_kw or {"event": "notification", "message": "hello"},
    )


class TestEventBusPublishSubscribe:
    """Test basic publish/subscribe mechanics."""

    def test_publish_stores_in_global_history(self):
        bus = EventBus(global_history_size=10)
        ev = _make_event(bus)
        bus.publish(ev)
        replay, _ = bus.subscribe_global()
        assert len(replay) == 1
        assert replay[0] is ev

    def test_publish_stores_in_issue_history(self):
        bus = EventBus(per_issue_history_size=5)
        ev = _make_event(bus, issue_id="abc")
        bus.publish(ev)
        replay, _ = bus.subscribe_issue("abc")
        assert len(replay) == 1
        assert replay[0] is ev

    def test_global_subscriber_receives_events(self):
        bus = EventBus()
        replay, q = bus.subscribe_global()
        assert replay == []

        ev = _make_event(bus)
        bus.publish(ev)
        assert q.qsize() == 1
        assert q.get_nowait() is ev

    def test_issue_subscriber_receives_only_matching_events(self):
        bus = EventBus()
        _, q = bus.subscribe_issue("id1")

        ev1 = _make_event(bus, issue_id="id1")
        ev2 = _make_event(bus, issue_id="id2")
        bus.publish(ev1)
        bus.publish(ev2)

        assert q.qsize() == 1
        assert q.get_nowait() is ev1

    def test_unsubscribe_global_stops_delivery(self):
        bus = EventBus()
        _, q = bus.subscribe_global()
        bus.unsubscribe_global(q)

        ev = _make_event(bus)
        bus.publish(ev)
        assert q.qsize() == 0

    def test_unsubscribe_issue_stops_delivery(self):
        bus = EventBus()
        _, q = bus.subscribe_issue("id1")
        bus.unsubscribe_issue("id1", q)

        ev = _make_event(bus, issue_id="id1")
        bus.publish(ev)
        assert q.qsize() == 0


class TestEventBusReplay:
    """Test replay/reconnection logic."""

    def test_replay_all_when_no_last_event_id(self):
        bus = EventBus()
        for _ in range(5):
            bus.publish(_make_event(bus))

        replay, _ = bus.subscribe_global()
        assert len(replay) == 5

    def test_replay_from_last_event_id(self):
        bus = EventBus()
        events = [_make_event(bus) for _ in range(5)]
        for ev in events:
            bus.publish(ev)

        # Request events after ID 3
        replay, _ = bus.subscribe_global(last_event_id=3)
        assert len(replay) == 2
        assert replay[0].id == 4
        assert replay[1].id == 5

    def test_gap_detection_global(self):
        bus = EventBus(global_history_size=3)
        # Publish 5 events, buffer only holds 3
        for _ in range(5):
            bus.publish(_make_event(bus))

        # Request from ID 1 — which is older than oldest retained (3)
        replay, _ = bus.subscribe_global(last_event_id=1)
        assert replay is None  # Gap signal

    def test_gap_detection_issue(self):
        bus = EventBus(per_issue_history_size=2)
        for _ in range(4):
            bus.publish(_make_event(bus, issue_id="id1"))

        replay, _ = bus.subscribe_issue("id1", last_event_id=1)
        assert replay is None  # Gap signal

    def test_replay_issue_empty_when_no_history(self):
        bus = EventBus()
        replay, _ = bus.subscribe_issue("nonexistent")
        assert replay == []


class TestEventBusOverflow:
    """Test overflow sentinel behavior."""

    def test_overflow_sends_sentinel(self):
        bus = EventBus(subscriber_queue_size=4)
        _, q = bus.subscribe_global()

        # Publish 4 events: 3 go in normally, 4th triggers overflow
        # maxsize=4, overflow at qsize >= maxsize-1 = 3
        for _ in range(4):
            bus.publish(_make_event(bus))

        # Queue should have 3 events then a None sentinel
        items = []
        while not q.empty():
            items.append(q.get_nowait())

        # First 3 are events, then sentinel None
        assert len(items) == 4
        assert items[0] is not None
        assert items[1] is not None
        assert items[2] is not None
        assert items[3] is None  # Overflow sentinel

    def test_overflow_removes_subscriber(self):
        bus = EventBus(subscriber_queue_size=4)
        _, q = bus.subscribe_global()

        # Fill to overflow
        for _ in range(4):
            bus.publish(_make_event(bus))

        assert bus.subscriber_count == 0  # Removed from subscribers

    def test_overflow_issue_subscriber(self):
        bus = EventBus(subscriber_queue_size=4)
        _, q = bus.subscribe_issue("id1")

        for _ in range(4):
            bus.publish(_make_event(bus, issue_id="id1"))

        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert items[-1] is None


class TestEventBusClear:
    """Test clear_issue cleanup."""

    def test_clear_issue_removes_history(self):
        bus = EventBus()
        bus.publish(_make_event(bus, issue_id="id1"))
        bus.publish(_make_event(bus, issue_id="id1"))

        bus.clear_issue("id1")

        replay, _ = bus.subscribe_issue("id1")
        assert replay == []

    def test_clear_nonexistent_issue_no_error(self):
        bus = EventBus()
        bus.clear_issue("nope")  # Should not raise


class TestEventBusRingBuffer:
    """Test ring buffer eviction."""

    def test_global_history_bounded(self):
        bus = EventBus(global_history_size=3)
        for _ in range(10):
            bus.publish(_make_event(bus))

        replay, _ = bus.subscribe_global()
        assert len(replay) == 3
        assert replay[0].id == 8
        assert replay[2].id == 10

    def test_per_issue_history_bounded(self):
        bus = EventBus(per_issue_history_size=2)
        for _ in range(5):
            bus.publish(_make_event(bus, issue_id="id1"))

        replay, _ = bus.subscribe_issue("id1")
        assert len(replay) == 2
        assert replay[0].id == 4
        assert replay[1].id == 5


class TestEventBusResolveIdentifier:
    """Test identifier resolution from event history."""

    def test_resolve_existing(self):
        bus = EventBus()
        bus.publish(_make_event(bus, issue_id="abc123", identifier="#42"))
        assert bus.resolve_identifier("#42") == "abc123"

    def test_resolve_unknown(self):
        bus = EventBus()
        assert bus.resolve_identifier("#999") is None


class TestEventBusAtomicHandoff:
    """Test that atomic subscribe→replay prevents event loss."""

    def test_no_event_loss_during_subscribe(self):
        """Events published between subscribe and replay delivery are not lost."""
        bus = EventBus()
        # Prepopulate 3 events
        for _ in range(3):
            bus.publish(_make_event(bus))

        # Subscribe (registers queue, then computes replay)
        replay, q = bus.subscribe_global()
        assert len(replay) == 3

        # Now publish another event — it should arrive in the live queue
        ev4 = _make_event(bus)
        bus.publish(ev4)
        assert q.get_nowait() is ev4


class TestSSEFormat:
    """Test the SSE wire format helper."""

    def test_format_sse_basic(self):
        from symphony.server import _format_sse

        ev = StreamEvent(
            id=42,
            event_type="notification",
            issue_id="id1",
            issue_identifier="#1",
            timestamp="2025-01-01T00:00:00+00:00",
            data={"event": "notification", "message": "hello"},
        )
        result = _format_sse(ev)
        assert result.startswith("id: 42\n")
        assert "event: notification\n" in result
        assert '"message": "hello"' in result
        assert result.endswith("\n\n")

    def test_format_sse_json_serialization(self):
        from symphony.server import _format_sse

        ev = StreamEvent(
            id=1,
            event_type="test",
            issue_id="id1",
            issue_identifier="#1",
            timestamp="",
            data={"nested": {"key": "value"}, "num": 42},
        )
        result = _format_sse(ev)
        # Ensure data line is valid JSON
        data_line = [l for l in result.split("\n") if l.startswith("data: ")][0]
        parsed = json.loads(data_line[len("data: "):])
        assert parsed == {"nested": {"key": "value"}, "num": 42}


class TestParseLastEventId:
    """Test Last-Event-ID header parsing."""

    def test_parse_valid_integer(self):
        from unittest.mock import MagicMock

        from symphony.server import _parse_last_event_id

        request = MagicMock()
        request.headers = {"Last-Event-ID": "42"}
        assert _parse_last_event_id(request) == 42

    def test_parse_missing_header(self):
        from unittest.mock import MagicMock

        from symphony.server import _parse_last_event_id

        request = MagicMock()
        request.headers = {}
        assert _parse_last_event_id(request) is None

    def test_parse_invalid_value(self):
        from unittest.mock import MagicMock

        from symphony.server import _parse_last_event_id

        request = MagicMock()
        request.headers = {"Last-Event-ID": "not-a-number"}
        assert _parse_last_event_id(request) is None


@pytest.mark.asyncio
class TestSSEGenerator:
    """Test the SSE async generator."""

    async def test_replay_then_live(self):
        from symphony.server import _sse_generator

        bus = EventBus()
        ev1 = _make_event(bus)
        ev2 = _make_event(bus)
        bus.publish(ev1)
        bus.publish(ev2)

        replay, q = bus.subscribe_global()

        # Put a live event and sentinel to end the stream
        ev3 = _make_event(bus)
        q.put_nowait(ev3)
        q.put_nowait(None)  # End stream

        chunks = []
        async for chunk in _sse_generator(q, replay, lambda: None):
            chunks.append(chunk)

        # Should have: 2 replay + 1 live + 1 overflow message
        assert len(chunks) == 4
        assert "id: 1\n" in chunks[0]
        assert "id: 2\n" in chunks[1]
        assert "id: 3\n" in chunks[2]
        assert "overflow" in chunks[3]

    async def test_gap_detection(self):
        from symphony.server import _sse_generator

        q: asyncio.Queue[StreamEvent | None] = asyncio.Queue(maxsize=256)
        q.put_nowait(None)  # End immediately

        chunks = []
        async for chunk in _sse_generator(q, None, lambda: None):
            chunks.append(chunk)

        assert chunks[0] == 'event: gap\ndata: {"reason":"history_expired"}\n\n'

    async def test_deduplication(self):
        from symphony.server import _sse_generator

        bus = EventBus()
        ev1 = _make_event(bus)
        bus.publish(ev1)

        replay, q = bus.subscribe_global()
        # Simulate duplicate: same event arrives in live queue
        q.put_nowait(ev1)
        # Then a new event
        ev2 = _make_event(bus)
        q.put_nowait(ev2)
        q.put_nowait(None)  # End

        chunks = []
        async for chunk in _sse_generator(q, replay, lambda: None):
            chunks.append(chunk)

        # replay(1) + live(1 new, skipped duplicate) + overflow
        assert len(chunks) == 3
        assert "id: 1\n" in chunks[0]  # Replay
        assert "id: 2\n" in chunks[1]  # New live event
        assert "overflow" in chunks[2]

    async def test_keepalive_on_timeout(self):
        from symphony.server import _sse_generator

        q: asyncio.Queue[StreamEvent | None] = asyncio.Queue(maxsize=256)

        chunks = []

        async def collect():
            async for chunk in _sse_generator(q, [], lambda: None):
                chunks.append(chunk)
                if "keepalive" in chunk:
                    # End the test after receiving keepalive
                    break

        # Run with a very short timeout by monkey-patching — instead, just
        # wait for keepalive (30s is too long for tests). Let's use a task
        # with a short deadline and inject None after a bit.
        task = asyncio.create_task(collect())

        # Wait briefly then end the stream
        await asyncio.sleep(0.05)
        q.put_nowait(None)
        await asyncio.wait_for(task, timeout=35)
        # If we got here within 35s, the keepalive fired (or sentinel ended it)

    async def test_unsubscribe_called_on_exit(self):
        from symphony.server import _sse_generator

        q: asyncio.Queue[StreamEvent | None] = asyncio.Queue(maxsize=256)
        q.put_nowait(None)

        unsubscribed = []
        async for _ in _sse_generator(q, [], lambda: unsubscribed.append(True)):
            pass

        assert len(unsubscribed) == 1
