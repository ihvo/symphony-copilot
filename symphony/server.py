"""OPTIONAL HTTP server extension for observability and operational control."""

from __future__ import annotations

import html as html_mod
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from symphony.orchestrator import Orchestrator

logger = logging.getLogger("symphony.server")

# Path to the Next.js static export build output
DASHBOARD_BUILD_DIR = Path(__file__).parent.parent / "dashboard" / "out"

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
        async def handle_dashboard() -> Response:
            # Serve static Next.js build if available, otherwise placeholder
            index_path = DASHBOARD_BUILD_DIR / "index.html"
            if index_path.is_file():
                return HTMLResponse(content=index_path.read_text())
            return HTMLResponse(content=_render_placeholder())

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

        # Mount static assets from Next.js build (after explicit routes to avoid conflicts)
        if DASHBOARD_BUILD_DIR.is_dir():
            next_assets = DASHBOARD_BUILD_DIR / "_next"
            if next_assets.is_dir():
                self._app.mount(
                    "/_next",
                    StaticFiles(directory=str(next_assets)),
                    name="next-assets",
                )

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


def _render_placeholder() -> str:
    """Render a minimal placeholder when the dashboard build is absent."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <title>Symphony Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" href="data:image/svg+xml,{FAVICON_SVG}">
    <style>
        body {{
            font-family: system-ui, -apple-system, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            background: #fafafa;
            color: #18181b;
        }}
        .card {{
            text-align: center;
            padding: 3rem;
            background: #fff;
            border: 1px solid #e4e4e7;
            border-radius: 12px;
            max-width: 480px;
        }}
        h1 {{ font-size: 1.25rem; margin-bottom: 1rem; }}
        p {{ color: #71717a; font-size: 0.875rem; line-height: 1.6; }}
        code {{
            display: inline-block;
            margin-top: 1rem;
            padding: 0.5rem 1rem;
            background: #f4f4f5;
            border-radius: 6px;
            font-size: 0.8125rem;
            font-family: ui-monospace, monospace;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Symphony Dashboard</h1>
        <p>The dashboard has not been built yet. Build it to get the full interface:</p>
        <code>cd dashboard && npm install && npm run build</code>
    </div>
</body>
</html>"""
