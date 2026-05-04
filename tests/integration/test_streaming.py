"""Integration tests for SSE streaming (EventBus + server routes).

Tests the full stack: orchestrator → EventBus → SSE endpoint → client.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from symphony.orchestrator import Orchestrator
from symphony.server import SymphonyServer
from symphony.streaming import EventBus

from .conftest import agent_command, wait_until


@pytest.mark.asyncio
async def test_stream_global_receives_agent_events(fake_github, make_workflow, tmp_path):
    """SSE /api/v1/stream delivers events when an agent session runs."""
    fake_github.add_issue(1, state="open", labels=["bug"])

    wf = make_workflow(
        endpoint=fake_github.base_url,
        agent_cfg={"turns": 1, "behavior": "success"},
    )
    orch = Orchestrator(wf)
    event_bus = EventBus()
    orch.set_event_bus(event_bus)
    await orch.start()
    server = SymphonyServer(orch, event_bus=event_bus)
    port = await server.start(0)

    try:
        # Wait for the agent to dispatch
        await wait_until(lambda: len(orch.state.running) > 0 or orch.state.completed, timeout=10)

        # Connect SSE client and collect events (with short timeout)
        events = []
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/api/v1/stream") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")

                # Collect events until we get session_dispatched or timeout
                deadline = asyncio.get_event_loop().time() + 10
                async for line in resp.aiter_lines():
                    if asyncio.get_event_loop().time() > deadline:
                        break
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        events.append(data)
                        # Stop after we get a meaningful event
                        if data.get("event") in ("session_ended", "session_dispatched"):
                            break

        # Should have received at least one event
        assert len(events) >= 1
        event_types = [e.get("event") for e in events]
        assert any(t in event_types for t in ("session_dispatched", "notification", "session_ended"))

    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_stream_disabled_returns_503(fake_github, make_workflow, tmp_path):
    """SSE /api/v1/stream returns 503 when no EventBus is configured."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)  # No event_bus
    port = await server.start(0)

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/v1/stream")
            assert r.status_code == 503
            data = r.json()
            assert data["error"]["code"] == "streaming_disabled"
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_stream_issue_not_found(fake_github, make_workflow, tmp_path):
    """SSE /api/v1/stream/{identifier} returns 404 for unknown issue."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    event_bus = EventBus()
    orch.set_event_bus(event_bus)
    await orch.start()
    server = SymphonyServer(orch, event_bus=event_bus)
    port = await server.start(0)

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/v1/stream/999")
            assert r.status_code == 404
            data = r.json()
            assert data["error"]["code"] == "issue_not_found"
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_stream_replay_with_last_event_id(fake_github, make_workflow, tmp_path):
    """SSE reconnection with Last-Event-ID replays missed events."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    event_bus = EventBus()
    orch.set_event_bus(event_bus)
    await orch.start()
    server = SymphonyServer(orch, event_bus=event_bus)
    port = await server.start(0)

    try:
        # Manually publish some events to the bus
        from symphony.streaming import StreamEvent

        for i in range(5):
            event_bus.publish(StreamEvent(
                id=event_bus.next_id(),
                event_type="notification",
                issue_id="test-id",
                issue_identifier="#99",
                timestamp="2025-01-01T00:00:00+00:00",
                data={"event": "notification", "message": f"msg-{i}"},
            ))

        # Connect with Last-Event-ID: 3 — should get events 4 and 5
        events = []
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET",
                f"http://127.0.0.1:{port}/api/v1/stream",
                headers={"Last-Event-ID": "3"},
            ) as resp:
                assert resp.status_code == 200
                deadline = asyncio.get_event_loop().time() + 3
                async for line in resp.aiter_lines():
                    if asyncio.get_event_loop().time() > deadline:
                        break
                    if line.startswith("id: "):
                        events.append(int(line[4:]))
                    # Stop after getting the replay
                    if len(events) >= 2:
                        break

        assert events == [4, 5]

    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_event_bus_publishes_on_dispatch(fake_github, make_workflow, tmp_path):
    """EventBus receives session_dispatched when orchestrator dispatches an issue."""
    fake_github.add_issue(1, state="open", labels=["bug"])

    wf = make_workflow(
        endpoint=fake_github.base_url,
        agent_cfg={"turns": 1, "behavior": "success"},
    )
    orch = Orchestrator(wf)
    event_bus = EventBus()
    orch.set_event_bus(event_bus)
    await orch.start()

    try:
        # Wait for dispatch
        await wait_until(lambda: len(orch.state.running) > 0 or orch.state.completed, timeout=10)

        # Check event bus got the dispatch event
        replay, _ = event_bus.subscribe_global()
        event_types = [e.event_type for e in replay]
        assert "session_dispatched" in event_types

    finally:
        await orch.stop()
