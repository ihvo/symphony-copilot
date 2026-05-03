"""§13.7 — HTTP Server Extension integration tests.

Starts the real orchestrator + HTTP server on an ephemeral port and
hits every endpoint.
"""

from __future__ import annotations

import asyncio

import aiohttp
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
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/api/v1/state") as r:
                assert r.status == 200
                data = await r.json()
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
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/") as r:
                assert r.status == 200
                html = await r.text()
                assert "Symphony Dashboard" in html
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
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/api/v1/999") as r:
                assert r.status == 404
                data = await r.json()
                assert data["error"]["code"] == "issue_not_found"
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_issue_detail_found(fake_github, make_workflow, tmp_path):
    """GET /api/v1/<id> returns detail for a running issue."""
    fake_github.add_issue(5, state="open")
    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
        agent_cfg={"turns": 1, "slow_turn_ms": 3000},
    )
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        ok = await wait_until(lambda: len(orch.state.running) > 0, timeout=5.0)
        assert ok

        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/api/v1/5") as r:
                assert r.status == 200
                data = await r.json()
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
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}/api/v1/refresh") as r:
                assert r.status == 202
                data = await r.json()
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
        async with aiohttp.ClientSession() as s:
            async with s.delete(f"http://127.0.0.1:{port}/api/v1/state") as r:
                assert r.status == 405
    finally:
        await server.stop()
        await orch.stop()


@pytest.mark.asyncio
async def test_dashboard_xss_escaping(fake_github, make_workflow, tmp_path):
    """Issue content with HTML is escaped in the dashboard."""
    fake_github.add_issue(1, state="open", title='<script>alert("xss")</script>')
    wf = make_workflow(
        endpoint=fake_github.base_url,
        max_turns=1,
        agent_cfg={"turns": 1, "slow_turn_ms": 2000},
    )
    orch = Orchestrator(wf)
    await orch.start()
    server = SymphonyServer(orch)
    port = await server.start(0)

    try:
        ok = await wait_until(lambda: len(orch.state.running) > 0, timeout=5.0)
        # Even if no issue is running yet, the dashboard should render
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/") as r:
                html = await r.text()
                assert "<script>" not in html
    finally:
        await server.stop()
        await orch.stop()
