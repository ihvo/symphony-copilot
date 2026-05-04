"""OPTIONAL HTTP server extension for observability and operational control."""

from __future__ import annotations

import html as html_mod
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

if TYPE_CHECKING:
    from symphony.orchestrator import Orchestrator

logger = logging.getLogger("symphony.server")

# Inline SVG favicon – music notes on a dark circle (fits the "Symphony" theme).
FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<circle cx='32' cy='32' r='30' fill='%23333'/>"
    "<g fill='white'>"
    "<ellipse cx='22' cy='42' rx='6' ry='4.5'/>"
    "<ellipse cx='38' cy='36' rx='6' ry='4.5'/>"
    "<rect x='27' y='14' width='3' height='28' rx='1.5'/>"
    "<rect x='43' y='8' width='3' height='28' rx='1.5'/>"
    "<path d='M30 14 C30 14 42 8 46 8 L46 18 C42 18 30 22 30 22Z'/>"
    "</g></svg>"
)


def _json_response(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        content=json.loads(json.dumps(data, default=str)),
        status_code=status,
    )


def _error_response(code: str, message: str, status: int = 400) -> JSONResponse:
    return _json_response({"error": {"code": code, "message": message}}, status=status)


class SymphonyServer:
    """HTTP server extension providing a dashboard and JSON API."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator
        self._app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._setup_routes()
        self._server_task: object | None = None
        self._uvicorn_server: object | None = None

    @property
    def app(self) -> FastAPI:
        """Expose the FastAPI app for testing."""
        return self._app

    def _setup_routes(self) -> None:
        # Static routes BEFORE the parameterized {identifier} route
        @self._app.get("/")
        async def handle_dashboard() -> HTMLResponse:
            snapshot = self._orch.get_snapshot()
            html = _render_dashboard(snapshot)
            return HTMLResponse(content=html)

        @self._app.get("/favicon.ico")
        async def handle_favicon() -> Response:
            svg = FAVICON_SVG.replace("%23", "#")
            return Response(
                content=svg,
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @self._app.get("/api/v1/state")
        async def handle_state() -> JSONResponse:
            snapshot = self._orch.get_snapshot()
            return _json_response(snapshot)

        @self._app.post("/api/v1/refresh")
        async def handle_refresh() -> JSONResponse:
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

        @self._app.get("/api/v1/{identifier}")
        async def handle_issue(identifier: str) -> JSONResponse:
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

    async def start(self, port: int, host: str = "127.0.0.1") -> int:
        """Start the HTTP server. Returns the actual bound port."""
        import asyncio
        import uvicorn

        config = uvicorn.Config(
            app=self._app,
            host=host,
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._uvicorn_server = server

        # Start serving in a background task
        self._server_task = asyncio.create_task(server.serve())

        # Wait until the server is actually started and listening
        while not server.started:
            await asyncio.sleep(0.01)

        # Resolve actual port (for ephemeral port=0)
        actual_port = port
        if server.servers:
            for s in server.servers:
                sockets = s.sockets
                if sockets:
                    actual_port = sockets[0].getsockname()[1]
                    break

        logger.info("http_server_started host=%s port=%d", host, actual_port)
        return actual_port

    async def stop(self) -> None:
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        if self._server_task:
            await self._server_task
        logger.info("http_server_stopped")


def _esc(val: object) -> str:
    """HTML-escape a value for safe interpolation."""
    return html_mod.escape(str(val))


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
            <td>{_esc(r.get('issue_identifier',''))}</td>
            <td>{_esc(r.get('state',''))}</td>
            <td>{_esc(r.get('session_id',''))}</td>
            <td>{_esc(r.get('turn_count',0))}</td>
            <td>{_esc(r.get('last_event',''))}</td>
            <td>{_esc(str(r.get('last_message',''))[:60])}</td>
            <td>{_esc(r.get('started_at',''))}</td>
            <td>{_esc(r.get('tokens',{}).get('total_tokens',0))}</td>
        </tr>"""

    retry_rows = ""
    for r in retrying:
        retry_rows += f"""
        <tr>
            <td>{_esc(r.get('issue_identifier',''))}</td>
            <td>{_esc(r.get('attempt',0))}</td>
            <td>{_esc(r.get('due_at',''))}</td>
            <td>{_esc(str(r.get('error',''))[:60])}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Symphony Dashboard</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="10">
    <link rel="icon" href="data:image/svg+xml,{FAVICON_SVG}">
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
