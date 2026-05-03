"""OPTIONAL HTTP server extension for observability and operational control."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from symphony.orchestrator import Orchestrator

logger = logging.getLogger("symphony.server")


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, default=str),
        content_type="application/json",
        status=status,
    )


def _error_response(code: str, message: str, status: int = 400) -> web.Response:
    return _json_response({"error": {"code": code, "message": message}}, status=status)


class SymphonyServer:
    """HTTP server extension providing a dashboard and JSON API."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator
        self._app = web.Application()
        self._setup_routes()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def _setup_routes(self) -> None:
        self._app.router.add_get("/", self._handle_dashboard)
        self._app.router.add_get("/api/v1/state", self._handle_state)
        self._app.router.add_get("/api/v1/{identifier}", self._handle_issue)
        self._app.router.add_post("/api/v1/refresh", self._handle_refresh)

    async def start(self, port: int, host: str = "127.0.0.1") -> int:
        """Start the HTTP server. Returns the actual bound port."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()

        # Resolve actual port (for ephemeral port=0)
        actual_port = port
        if self._site._server and self._site._server.sockets:
            actual_port = self._site._server.sockets[0].getsockname()[1]

        logger.info("http_server_started host=%s port=%d", host, actual_port)
        return actual_port

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        logger.info("http_server_stopped")

    # --- Handlers ---

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        """Serve a human-readable HTML dashboard."""
        snapshot = self._orch.get_snapshot()
        html = _render_dashboard(snapshot)
        return web.Response(text=html, content_type="text/html")

    async def _handle_state(self, request: web.Request) -> web.Response:
        """GET /api/v1/state – return runtime state snapshot."""
        snapshot = self._orch.get_snapshot()
        return _json_response(snapshot)

    async def _handle_issue(self, request: web.Request) -> web.Response:
        """GET /api/v1/<identifier> – return issue-specific detail."""
        identifier = request.match_info["identifier"]
        # Normalize: add # prefix if not present
        if not identifier.startswith("#"):
            identifier = f"#{identifier}"

        detail = self._orch.get_issue_detail(identifier)
        if detail is None:
            return _error_response(
                "issue_not_found",
                f"Issue {identifier} not found in current state",
                status=404,
            )
        return _json_response(detail)

    async def _handle_refresh(self, request: web.Request) -> web.Response:
        """POST /api/v1/refresh – trigger an immediate poll cycle."""
        from datetime import datetime, timezone

        # Schedule an immediate tick
        self._orch._schedule_tick(0)
        return _json_response(
            {
                "queued": True,
                "coalesced": False,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "operations": ["poll", "reconcile"],
            },
            status=202,
        )


def _render_dashboard(snapshot: dict) -> str:
    """Render a minimal HTML dashboard from a state snapshot."""
    running = snapshot.get("running", [])
    retrying = snapshot.get("retrying", [])
    totals = snapshot.get("copilot_totals", {})
    counts = snapshot.get("counts", {})

    running_rows = ""
    for r in running:
        running_rows += f"""
        <tr>
            <td>{r.get('issue_identifier','')}</td>
            <td>{r.get('state','')}</td>
            <td>{r.get('session_id','')}</td>
            <td>{r.get('turn_count',0)}</td>
            <td>{r.get('last_event','')}</td>
            <td>{r.get('last_message','')[:60]}</td>
            <td>{r.get('started_at','')}</td>
            <td>{r.get('tokens',{}).get('total_tokens',0)}</td>
        </tr>"""

    retry_rows = ""
    for r in retrying:
        retry_rows += f"""
        <tr>
            <td>{r.get('issue_identifier','')}</td>
            <td>{r.get('attempt',0)}</td>
            <td>{r.get('due_at','')}</td>
            <td>{r.get('error','')[:60]}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Symphony Dashboard</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="10">
    <style>
        body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #f8f9fa; }}
        h1 {{ color: #333; }}
        table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 0.9rem; }}
        th {{ background: #e9ecef; }}
        .stats {{ display: flex; gap: 2rem; margin: 1rem 0; }}
        .stat {{ background: white; padding: 1rem 1.5rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .stat-value {{ font-size: 1.5rem; font-weight: bold; color: #333; }}
        .stat-label {{ font-size: 0.8rem; color: #666; margin-top: 0.25rem; }}
    </style>
</head>
<body>
    <h1>🎵 Symphony Dashboard</h1>
    <p>Generated: {snapshot.get('generated_at','')}</p>

    <div class="stats">
        <div class="stat">
            <div class="stat-value">{counts.get('running', 0)}</div>
            <div class="stat-label">Running</div>
        </div>
        <div class="stat">
            <div class="stat-value">{counts.get('retrying', 0)}</div>
            <div class="stat-label">Retrying</div>
        </div>
        <div class="stat">
            <div class="stat-value">{totals.get('total_tokens', 0):,}</div>
            <div class="stat-label">Total Tokens</div>
        </div>
        <div class="stat">
            <div class="stat-value">{totals.get('seconds_running', 0):.0f}s</div>
            <div class="stat-label">Runtime</div>
        </div>
    </div>

    <h2>Running ({counts.get('running', 0)})</h2>
    <table>
        <tr><th>Issue</th><th>State</th><th>Session</th><th>Turns</th><th>Last Event</th><th>Message</th><th>Started</th><th>Tokens</th></tr>
        {running_rows or '<tr><td colspan="8" style="text-align:center;color:#999">No active sessions</td></tr>'}
    </table>

    <h2>Retry Queue ({counts.get('retrying', 0)})</h2>
    <table>
        <tr><th>Issue</th><th>Attempt</th><th>Due At</th><th>Error</th></tr>
        {retry_rows or '<tr><td colspan="4" style="text-align:center;color:#999">No retries queued</td></tr>'}
    </table>
</body>
</html>"""
