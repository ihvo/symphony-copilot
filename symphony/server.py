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
    """Render the Symphony Dashboard from a state snapshot."""
    running = snapshot.get("running", [])
    retrying = snapshot.get("retrying", [])
    totals = snapshot.get("copilot_totals", {})
    counts = snapshot.get("counts", {})

    running_rows = ""
    for r in running:
        tokens = r.get("tokens", {}).get("total_tokens", 0)
        running_rows += f"""
            <tr>
                <td class="cell-id">{_esc(r.get('issue_identifier',''))}</td>
                <td><span class="badge badge-active">{_esc(r.get('state',''))}</span></td>
                <td class="cell-mono">{_esc(r.get('session_id',''))}</td>
                <td class="cell-mono">{_esc(r.get('turn_count',0))}</td>
                <td>{_esc(r.get('last_event',''))}</td>
                <td class="cell-msg">{_esc(str(r.get('last_message',''))[:60])}</td>
                <td class="cell-mono">{_esc(r.get('started_at',''))}</td>
                <td class="cell-mono">{tokens:,}</td>
            </tr>"""

    retry_rows = ""
    for r in retrying:
        retry_rows += f"""
            <tr>
                <td class="cell-id">{_esc(r.get('issue_identifier',''))}</td>
                <td class="cell-mono">{_esc(r.get('attempt',0))}</td>
                <td class="cell-mono">{_esc(r.get('due_at',''))}</td>
                <td class="cell-msg">{_esc(str(r.get('error',''))[:60])}</td>
            </tr>"""

    running_count = counts.get("running", 0)
    retrying_count = counts.get("retrying", 0)
    total_tokens = totals.get("total_tokens", 0)
    runtime_s = totals.get("seconds_running", 0)

    # Format runtime as human-readable duration
    if runtime_s >= 3600:
        runtime_display = f"{runtime_s / 3600:.1f}h"
    elif runtime_s >= 60:
        runtime_display = f"{runtime_s / 60:.0f}m"
    else:
        runtime_display = f"{runtime_s:.0f}s"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <title>Symphony Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="10">
    <link rel="icon" href="data:image/svg+xml,{FAVICON_SVG}">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

        :root {{
            --bg: #fafafa;
            --surface: #ffffff;
            --border: #e4e4e7;
            --border-subtle: #f4f4f5;
            --text-primary: #18181b;
            --text-secondary: #71717a;
            --text-muted: #a1a1aa;
            --accent: #059669;
            --accent-subtle: #ecfdf5;
            --warning: #d97706;
            --warning-subtle: #fffbeb;
            --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
            --shadow-md: 0 4px 12px -2px rgba(0,0,0,0.06);
            --radius: 12px;
        }}

        body {{
            font-family: 'Outfit', system-ui, -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text-primary);
            line-height: 1.5;
            min-height: 100dvh;
        }}

        .layout {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 3rem 2.5rem;
        }}

        /* Header — left-aligned, asymmetric */
        .header {{
            display: grid;
            grid-template-columns: 1fr auto;
            align-items: end;
            gap: 1rem;
            margin-bottom: 2.5rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border);
        }}
        .header h1 {{
            font-size: 1.5rem;
            font-weight: 600;
            letter-spacing: -0.025em;
            color: var(--text-primary);
        }}
        .header-meta {{
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: 'JetBrains Mono', monospace;
        }}

        /* Metrics — asymmetric grid: primary stat wider */
        .metrics {{
            display: grid;
            grid-template-columns: 2fr 1fr 1fr 1fr;
            gap: 1rem;
            margin-bottom: 2.5rem;
        }}
        .metric {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 1.25rem 1.5rem;
            box-shadow: var(--shadow-sm);
            transition: box-shadow 0.2s ease;
        }}
        .metric:hover {{
            box-shadow: var(--shadow-md);
        }}
        .metric-value {{
            font-size: 2rem;
            font-weight: 700;
            letter-spacing: -0.03em;
            color: var(--text-primary);
            font-family: 'JetBrains Mono', monospace;
        }}
        .metric-label {{
            font-size: 0.75rem;
            font-weight: 500;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-top: 0.25rem;
        }}
        .metric--active .metric-value {{
            color: var(--accent);
        }}

        /* Section headers */
        .section-header {{
            display: flex;
            align-items: baseline;
            gap: 0.5rem;
            margin-bottom: 0.75rem;
        }}
        .section-header h2 {{
            font-size: 0.875rem;
            font-weight: 600;
            color: var(--text-primary);
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        .section-count {{
            font-size: 0.75rem;
            font-family: 'JetBrains Mono', monospace;
            color: var(--text-muted);
        }}

        /* Tables — minimal, clean lines */
        .table-wrap {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow-sm);
            overflow: hidden;
            margin-bottom: 2rem;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8125rem;
        }}
        thead th {{
            text-align: left;
            font-size: 0.6875rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-muted);
            padding: 0.75rem 1rem;
            border-bottom: 1px solid var(--border);
            background: var(--border-subtle);
        }}
        tbody tr {{
            border-bottom: 1px solid var(--border-subtle);
            transition: background 0.15s ease;
        }}
        tbody tr:last-child {{
            border-bottom: none;
        }}
        tbody tr:hover {{
            background: #fafafa;
        }}
        td {{
            padding: 0.75rem 1rem;
            color: var(--text-primary);
            vertical-align: middle;
        }}
        .cell-id {{
            font-weight: 600;
            color: var(--accent);
        }}
        .cell-mono {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            color: var(--text-secondary);
        }}
        .cell-msg {{
            color: var(--text-secondary);
            max-width: 20rem;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        /* Badges */
        .badge {{
            display: inline-block;
            font-size: 0.6875rem;
            font-weight: 500;
            padding: 0.2rem 0.5rem;
            border-radius: 6px;
            text-transform: lowercase;
        }}
        .badge-active {{
            background: var(--accent-subtle);
            color: var(--accent);
        }}
        .badge-retry {{
            background: var(--warning-subtle);
            color: var(--warning);
        }}

        /* Empty state */
        .empty {{
            text-align: center;
            padding: 2.5rem 1rem;
            color: var(--text-muted);
            font-size: 0.8125rem;
        }}
        .empty-icon {{
            width: 2rem;
            height: 2rem;
            margin: 0 auto 0.75rem;
            opacity: 0.4;
        }}

        /* Responsive collapse */
        @media (max-width: 768px) {{
            .layout {{ padding: 1.5rem 1rem; }}
            .metrics {{ grid-template-columns: 1fr 1fr; }}
            .table-wrap {{ overflow-x: auto; }}
            table {{ min-width: 600px; }}
        }}
    </style>
</head>
<body>
    <div class="layout">
        <header class="header">
            <h1>Symphony Dashboard</h1>
            <span class="header-meta">{_esc(snapshot.get('generated_at',''))}</span>
        </header>

        <div class="metrics">
            <div class="metric metric--active">
                <div class="metric-value">{running_count}</div>
                <div class="metric-label">Active Sessions</div>
            </div>
            <div class="metric">
                <div class="metric-value">{retrying_count}</div>
                <div class="metric-label">Retrying</div>
            </div>
            <div class="metric">
                <div class="metric-value">{total_tokens:,}</div>
                <div class="metric-label">Tokens Used</div>
            </div>
            <div class="metric">
                <div class="metric-value">{runtime_display}</div>
                <div class="metric-label">Runtime</div>
            </div>
        </div>

        <div class="section-header">
            <h2>Running</h2>
            <span class="section-count">{running_count}</span>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Issue</th>
                        <th>State</th>
                        <th>Session</th>
                        <th>Turns</th>
                        <th>Last Event</th>
                        <th>Message</th>
                        <th>Started</th>
                        <th>Tokens</th>
                    </tr>
                </thead>
                <tbody>
                    {running_rows or '<tr><td colspan="8"><div class="empty"><svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4m0 4h.01"/></svg>No active sessions</div></td></tr>'}
                </tbody>
            </table>
        </div>

        <div class="section-header">
            <h2>Retry Queue</h2>
            <span class="section-count">{retrying_count}</span>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Issue</th>
                        <th>Attempt</th>
                        <th>Due At</th>
                        <th>Error</th>
                    </tr>
                </thead>
                <tbody>
                    {retry_rows or '<tr><td colspan="4"><div class="empty"><svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>No retries queued</div></td></tr>'}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""
