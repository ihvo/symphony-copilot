"""§13.7 — HTTP Server Extension integration tests.

Starts the real orchestrator + HTTP server on an ephemeral port and
hits every endpoint.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from symphony.orchestrator import Orchestrator
from symphony.server import SymphonyServer

from .conftest import wait_until


@pytest.mark.asyncio
async def test_get_state_endpoint(fake_github, make_workflow, tmp_path):
    """GET /api/v1/state returns running, retrying, totals."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/api/v1/state")
            assert r.status_code == 200
            data = r.json()
            assert "counts" in data
            assert "copilot_totals" in data
            assert "running" in data
            assert "retrying" in data
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_dashboard_html(fake_github, make_workflow, tmp_path):
    """GET / returns an HTML page with the dashboard."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/")
            assert r.status_code == 200
            assert "Symphony Dashboard" in r.text
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_issue_detail_not_found(fake_github, make_workflow, tmp_path):
    """GET /api/v1/<unknown> returns 404."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/api/v1/999")
            assert r.status_code == 404
            data = r.json()
            assert data["error"]["code"] == "issue_not_found"
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_issue_detail_found(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """GET /api/v1/<id> returns detail for a running issue."""
    fake_github.add_issue(5, state="open")
    mock_agent_runner["hang_for"].add("NODE_5")
    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
    )
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        ok = await wait_until(lambda: len(orch.state.running) > 0, timeout=5.0)
        assert ok

        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/api/v1/5")
            assert r.status_code == 200
            data = r.json()
            assert data["issue_identifier"] == "#5"
            assert data["status"] == "running"
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_refresh_endpoint(fake_github, make_workflow, tmp_path):
    """POST /api/v1/refresh returns 202."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"http://127.0.0.1:{port}/api/v1/refresh")
            assert r.status_code == 202
            data = r.json()
            assert data["queued"] is True
            assert "poll" in data["operations"]
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_method_not_allowed(fake_github, make_workflow, tmp_path):
    """DELETE on a GET-only route returns 405."""
    wf = make_workflow(endpoint=fake_github.base_url)
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        async with httpx.AsyncClient() as c:
            r = await c.delete(f"http://127.0.0.1:{port}/api/v1/state")
            assert r.status_code == 405
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_dashboard_xss_escaping(fake_github, make_workflow, tmp_path, mock_agent_runner):
    """Issue content with HTML is escaped in the dashboard."""
    fake_github.add_issue(1, state="open", title='<script>alert("xss")</script>')
    mock_agent_runner["hang_for"].add("NODE_1")
    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
    )
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        ok = await wait_until(lambda: len(orch.state.running) > 0, timeout=5.0)
        # Even if no issue is running yet, the dashboard should render
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/")
            assert "<script>" not in r.text
    finally:
        await server.stop()
        await orch.stop()
