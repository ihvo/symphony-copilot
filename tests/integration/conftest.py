"""Shared fixtures for integration tests.

Provides:

*  ``FakeGitHub`` — an in-process aiohttp server that mimics the subset
   of the GitHub REST Issues API that Symphony uses.
*  ``agent_command()`` — builds a ``copilot.command`` string that runs
   ``mock_agent.py`` with a given config dict.
*  Pytest fixtures that start/stop the fake server and create workflow
   files wired to it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

MOCK_AGENT_SCRIPT = str(Path(__file__).parent / "mock_agent.py")


def agent_command(config: dict[str, Any] | None = None) -> str:
    """Return a shell command that runs the mock agent with *config*."""
    cfg_json = json.dumps(config or {})
    return f"{sys.executable} {MOCK_AGENT_SCRIPT} {shlex.quote(cfg_json)}"


# ---------------------------------------------------------------------------
# FakeGitHub
# ---------------------------------------------------------------------------

class FakeGitHub:
    """Minimal fake GitHub Issues REST API."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.request_log: list[tuple[str, str, dict[str, str]]] = []
        self.errors: dict[str, tuple[int, str]] = {}
        self._app = web.Application()
        self._app.router.add_get(
            "/repos/{owner}/{repo}/issues", self._handle_list,
        )
        self._app.router.add_get(
            "/repos/{owner}/{repo}/issues/{number:\\d+}", self._handle_get,
        )
        self.base_url: str = ""

    @property
    def app(self) -> web.Application:
        return self._app

    # -- helpers for test setup --

    def add_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        state: str = "open",
        labels: list[str] | None = None,
        body: str | None = None,
        node_id: str | None = None,
        created_at: str = "2025-01-01T00:00:00Z",
        updated_at: str = "2025-01-01T00:00:00Z",
        is_pr: bool = False,
    ) -> "FakeGitHub":
        entry: dict[str, Any] = {
            "id": number * 1000,
            "node_id": node_id or f"NODE_{number}",
            "number": number,
            "title": title or f"Issue #{number}",
            "body": body or "",
            "state": state,
            "html_url": f"https://github.com/test/repo/issues/{number}",
            "labels": [{"name": lbl} for lbl in (labels or [])],
            "created_at": created_at,
            "updated_at": updated_at,
        }
        if is_pr:
            entry["pull_request"] = {"url": "..."}
        self.issues[number] = entry
        return self

    def set_state(self, number: int, state: str) -> None:
        if number in self.issues:
            self.issues[number]["state"] = state

    def inject_error(self, key: str, status: int, body: str = "error") -> None:
        """Make requests matching *key* return *status*.

        *key* can be ``"list"`` (all list endpoints), ``"issue:<N>"``
        (single-issue fetch), or a substring of the URL path.
        """
        self.errors[key] = (status, body)

    def clear_errors(self) -> None:
        self.errors.clear()

    # -- handlers --

    async def _handle_list(self, request: web.Request) -> web.Response:
        self.request_log.append(("GET", request.path, dict(request.query)))

        for key, (status, body) in self.errors.items():
            if key == "list" or key in request.path:
                return web.Response(text=body, status=status)

        state_filter = request.query.get("state", "open").lower()
        per_page = int(request.query.get("per_page", "50"))
        page = int(request.query.get("page", "1"))

        matching = sorted(
            (i for i in self.issues.values() if i["state"].lower() == state_filter),
            key=lambda x: x["number"],
        )
        start = (page - 1) * per_page
        return web.json_response(matching[start : start + per_page])

    async def _handle_get(self, request: web.Request) -> web.Response:
        number = int(request.match_info["number"])
        self.request_log.append(("GET", request.path, {}))

        for key, (status, body) in self.errors.items():
            if key == f"issue:{number}" or key in request.path:
                return web.Response(text=body, status=status)

        issue = self.issues.get(number)
        if issue is None:
            return web.json_response({"message": "Not Found"}, status=404)
        return web.json_response(issue)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def fake_github():
    """Start a ``FakeGitHub`` server on an ephemeral port.

    Yields the ``FakeGitHub`` instance whose ``.base_url`` is already set.
    """
    gh = FakeGitHub()
    runner = web.AppRunner(gh.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    gh.base_url = f"http://127.0.0.1:{port}"
    try:
        yield gh
    finally:
        await runner.cleanup()


@pytest.fixture
def make_workflow(tmp_path):
    """Factory fixture: creates ``WORKFLOW.md`` in *tmp_path* and returns path."""

    def _make(
        *,
        endpoint: str = "https://api.github.com",
        repo: str = "test/repo",
        api_key: str = "test-token",
        poll_ms: int = 30_000,
        max_concurrent: int = 5,
        max_turns: int = 1,
        agent_cfg: dict[str, Any] | None = None,
        copilot_command: str | None = None,
        hooks: dict[str, str] | None = None,
        prompt: str = "Work on {{ issue.identifier }}: {{ issue.title }}",
        extra_yaml: str = "",
        active_states: list[str] | None = None,
        terminal_states: list[str] | None = None,
        workspace_root: str | None = None,
        copilot_overrides: dict[str, Any] | None = None,
    ) -> str:
        ws_root = workspace_root or str(tmp_path / "workspaces")
        cmd = copilot_command or agent_command(agent_cfg or {"turns": max_turns})

        active = active_states or ["open"]
        terminal = terminal_states or ["closed"]

        import yaml

        config: dict[str, Any] = {
            "tracker": {
                "kind": "github",
                "endpoint": endpoint,
                "repo": repo,
                "api_key": api_key,
                "active_states": active,
                "terminal_states": terminal,
            },
            "polling": {"interval_ms": poll_ms},
            "workspace": {"root": ws_root},
            "agent": {
                "max_concurrent_agents": max_concurrent,
                "max_turns": max_turns,
            },
            "copilot": {
                "command": cmd,
                "turn_timeout_ms": 10000,
                "read_timeout_ms": 3000,
                "stall_timeout_ms": 60000,
            },
        }

        if copilot_overrides:
            config["copilot"].update(copilot_overrides)
        if hooks:
            config["hooks"] = hooks

        # Allow extra YAML to be merged (for per-state concurrency etc.)
        if extra_yaml:
            extra = yaml.safe_load(extra_yaml)
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if k in config and isinstance(config[k], dict) and isinstance(v, dict):
                        config[k].update(v)
                    else:
                        config[k] = v

        front_matter = yaml.dump(config, default_flow_style=False, sort_keys=False)

        content = f"---\n{front_matter}---\n{prompt}\n"
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text(content)
        return str(wf_path)

    return _make


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def wait_until(predicate, *, timeout: float = 5.0, poll: float = 0.05) -> bool:
    """Poll *predicate* until truthy or *timeout* seconds elapse."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return False


@pytest.fixture
def mock_agent_runner():
    """Patch ``run_agent_session`` in the orchestrator module so that
    workers succeed instantly without needing a real Copilot SDK session.

    Yields a dict with ``calls`` list and ``fail_for`` set. Add issue IDs
    to ``fail_for`` to make the mock raise for those issues.  Add issue IDs
    to ``hang_for`` to make the mock sleep forever (for stall tests).
    """
    from unittest.mock import patch
    from symphony.models import LiveSession

    state: dict = {"calls": [], "fail_for": set(), "hang_for": set()}

    async def fake_run(*, config, workspace_path, issue, prompt, attempt,
                       on_event=None, max_turns=20, fetch_issue_state=None):
        state["calls"].append({
            "issue_id": issue.id,
            "identifier": issue.identifier,
            "prompt": prompt,
            "attempt": attempt,
        })
        if issue.id in state["hang_for"]:
            await asyncio.sleep(3600)  # hang until cancelled
            return LiveSession(turn_count=0)
        if issue.id in state["fail_for"]:
            raise RuntimeError(f"mock failure for {issue.identifier}")
        await asyncio.sleep(0.05)
        return LiveSession(turn_count=1)

    with patch("symphony.orchestrator.run_agent_session", side_effect=fake_run):
        yield state
