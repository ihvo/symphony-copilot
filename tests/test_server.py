"""Tests for HTTP server extension (SPEC §13.7, §17.6)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from symphony.server import SymphonyServer


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.get_snapshot.return_value = {
        "generated_at": "2025-01-01T00:00:00Z",
        "counts": {"running": 1, "retrying": 0},
        "running": [
            {
                "issue_id": "id1",
                "issue_identifier": "#1",
                "state": "open",
                "session_id": "t1-turn1",
                "turn_count": 3,
                "last_event": "turn_completed",
                "last_message": "done",
                "started_at": "2025-01-01T00:00:00Z",
                "last_event_at": "2025-01-01T00:01:00Z",
                "tokens": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            }
        ],
        "retrying": [],
        "copilot_totals": {
            "input_tokens": 500,
            "output_tokens": 200,
            "total_tokens": 700,
            "seconds_running": 120.5,
        },
        "rate_limits": None,
    }
    orch.get_issue_detail.return_value = None
    orch._schedule_tick = MagicMock()
    return orch


@pytest.fixture
def symphony_server(mock_orchestrator):
    return SymphonyServer(mock_orchestrator)


@pytest.fixture
def client(symphony_server):
    """Return an httpx AsyncClient wired to the FastAPI app (no real server)."""
    transport = httpx.ASGITransport(app=symphony_server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
async def test_state_endpoint(mock_orchestrator, client):
    resp = await client.get("/api/v1/state")
    assert resp.status_code == 200
    data = resp.json()
    assert data["counts"]["running"] == 1
    assert data["copilot_totals"]["total_tokens"] == 700


@pytest.mark.asyncio
async def test_dashboard_endpoint(mock_orchestrator, client):
    resp = await client.get("/")
    assert resp.status_code == 200
    text = resp.text
    assert "Symphony Dashboard" in text
    assert "#1" in text
    assert 'rel="icon"' in text
    assert "data:image/svg+xml," in text


@pytest.mark.asyncio
async def test_issue_not_found(mock_orchestrator, client):
    resp = await client.get("/api/v1/999")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"]["code"] == "issue_not_found"


@pytest.mark.asyncio
async def test_issue_found(mock_orchestrator, client):
    mock_orchestrator.get_issue_detail.return_value = {
        "issue_identifier": "#1",
        "issue_id": "id1",
        "status": "running",
    }
    resp = await client.get("/api/v1/1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["issue_identifier"] == "#1"


@pytest.mark.asyncio
async def test_refresh_endpoint(mock_orchestrator, client):
    resp = await client.post("/api/v1/refresh")
    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] is True
    assert "poll" in data["operations"]


@pytest.mark.asyncio
async def test_favicon_endpoint(client):
    resp = await client.get("/favicon.ico")
    assert resp.status_code == 200
    assert "image/svg+xml" in resp.headers["content-type"]
    text = resp.text
    assert "<svg" in text
    assert resp.headers.get("cache-control") == "public, max-age=86400"


@pytest.mark.asyncio
async def test_unsupported_method(mock_orchestrator, client):
    resp = await client.delete("/api/v1/state")
    assert resp.status_code == 405
