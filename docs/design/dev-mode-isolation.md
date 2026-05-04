# Dev Mode: Local Multi-Instance Isolation

**Status:** Proposed
**Author:** Symphony Team
**Package:** symphony

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 1.0 | 2026-05-04 | Initial design |
| 1.1 | 2026-05-04 | Incorporated review feedback: fixed startup ordering, introduced DevHarness (mock_agent.py is incompatible with SDK subprocess path), persistent dev overlay on reload, dummy tracker.repo, route prefix fix |

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Current Architecture](#2-current-architecture)
3. [Requirements](#3-requirements)
4. [Options Evaluation](#4-options-evaluation)
5. [Recommended Approach](#5-recommended-approach)
6. [Migration Plan](#6-migration-plan)
7. [Test Strategy](#7-test-strategy)
8. [Risk Assessment](#8-risk-assessment)
9. [Decision Records](#9-decision-records)

## 1. Problem Statement

Symphony orchestrates real coding agents against real GitHub Issues. When developing Symphony itself, running multiple instances locally creates four contention vectors:

1. **Issue contention**: Multiple instances poll the same GitHub repo and dispatch the same issues—agents compete, create conflicting PRs, or duplicate comments.
2. **Workspace collisions**: All instances write to the same `workspace.root` directory, causing race conditions on per-issue directories.
3. **Port conflicts**: All instances try to bind the same HTTP server port, preventing multi-instance observation.
4. **Real work execution**: Agents perform real work (commits, PRs, issue comments) even when the developer is only testing orchestration logic.

### Evidence

- `orchestrator.py:131-137` — single `GitHubTrackerClient` instance, no isolation mechanism
- `config.py:109-119` — `workspace_root` is a single global path
- `server.py:108-139` — binds a single port, no auto-assignment
- `runner.py:145-169` — launches real `CopilotClient` subprocess against real APIs
- `WORKFLOW.md:2-3` — points at `ihvo/symphony-copilot` (real repo)

### Impact of Not Fixing

- Developers cannot safely iterate on orchestrator logic without risking real API mutations.
- Running integration-level tests against real infrastructure is slow and costly.
- Multi-developer teams cannot run Symphony locally without manual coordination of which issues each person "owns."

## 2. Current Architecture

### Contention Points

```
┌─────────────────────────────────────────────────────────┐
│                 Symphony Instance                         │
│                                                          │
│  Orchestrator → GitHubTrackerClient → GitHub REST API   │
│       │                                     ↑            │
│       ├→ workspace.root (shared filesystem) │ CONTENTION │
│       ├→ HTTP :8111 (single port)           │            │
│       └→ Agent Runner → Copilot SDK → REAL WORK         │
└─────────────────────────────────────────────────────────┘
```

### Existing Test Infrastructure

The integration test suite already solves these problems for tests:

- **`FakeGitHub`** (`tests/integration/conftest.py:50-143`): In-process mock via `respx` that intercepts httpx calls. Provides `add_issue()`, `set_state()`, `inject_error()`.
- **`mock_agent.py`** (`tests/integration/mock_agent.py`): Standalone JSONRPC-over-stdio process. Configurable behaviors: `success`, `fail`, `cancel`, `input_required`, `exit`, `hang`.
- **`make_workflow`** fixture: Generates WORKFLOW.md wired to fake server + mock agent.

However, these are **test-only** constructs using `respx` request interception—they cannot be used as standalone servers for interactive development.

### Agent Harness Design

The existing design document (`docs/design/claude-sdk-agent-harness.md`) defines an `AgentHarness` Protocol that abstracts the agent runner:

```python
class AgentHarness(Protocol):
    @property
    def session(self) -> LiveSession: ...
    async def start(self) -> None: ...
    async def run_turn(self, prompt: str, turn_number: int) -> bool: ...
    async def stop(self) -> None: ...
```

Dev mode will use the real runner code path with the harness protocol pointed at `mock_agent.py`.

## 3. Requirements

### Must-Have

- **R1**: A `--dev` CLI flag that activates isolated development mode.
- **R2**: A mock issue tracker as a real HTTP server that speaks the GitHub Issues REST API subset (list issues, get single issue).
- **R3**: Mock tracker controllable via CLI subcommands (`symphony dev add-issue`, `symphony dev set-state`, etc.) while the orchestrator is running.
- **R4**: Mock tracker controllable by the orchestrator itself (it talks to the same HTTP endpoint, no special code path).
- **R5**: Agent runner uses the real harness protocol with `mock_agent.py` as the agent binary (testing full runner lifecycle without real Copilot/Claude).
- **R6**: Each dev instance gets an isolated workspace root (no collisions between instances).
- **R7**: Each dev instance auto-selects an available HTTP port (or accepts explicit override).
- **R8**: Multiple dev instances can run simultaneously with zero interference.

### Nice-to-Have

- **N1**: Mock tracker persists state to disk so it survives restarts.
- **N2**: A `--dev-seed` option that pre-populates the mock tracker with N synthetic issues for quick testing.
- **N3**: Mock tracker exposes a web UI showing its current state (piggyback on the existing dashboard).
- **N4**: `symphony dev status` subcommand to list all running dev instances with their ports and workspace roots.

### Constraints

- **C1**: Dev mode must not introduce any runtime dependencies beyond what's already in `pyproject.toml`.
- **C2**: Production code paths remain unchanged when `--dev` is not passed.
- **C3**: Mock tracker must be compatible with the existing `GitHubTrackerClient`—no tracker code changes needed.
- **C4**: The mock agent binary (`mock_agent.py`) behavior must be configurable from WORKFLOW.md (via the existing `copilot.command` or `agent.harness` mechanism).

## 4. Options Evaluation

### Option A: Embedded Mock Tracker (In-Process)

**Approach**: Start a FastAPI/aiohttp HTTP server inside the orchestrator process that serves the GitHub Issues REST API. The orchestrator's `GitHubTrackerClient` points at `http://127.0.0.1:{port}` (the local mock). CLI commands hit the same server via a control API.

**Pros**: Single process, simple lifecycle, shared memory state.
**Cons**: Harder to run mock tracker independently; crashes take down both; harder to attach debugger to orchestrator vs tracker separately.

### Option B: Standalone Mock Tracker Server (Separate Process)

**Approach**: A separate long-running process (`symphony dev tracker start`) that serves both the GitHub API subset and a control API. The orchestrator and CLI commands both talk to it via HTTP.

**Pros**: Independent lifecycle, can outlive the orchestrator, easy to attach debuggers separately.
**Cons**: Process coordination (need to start tracker before orchestrator), port discovery, more moving parts.

### Option C: Embedded Mock Tracker + Control Sidecar

**Approach**: Mock tracker runs embedded in the orchestrator process (like Option A), but a separate CLI sidecar discovers the running instance and sends control commands to it via the same HTTP API.

**Pros**: Single process for the core system (simple startup), CLI control works against any running instance, no separate process to manage.
**Cons**: Slightly more complex than pure Option A because the control API must be discoverable.

### Comparison Matrix

| Criterion | A: Embedded | B: Standalone | C: Embedded + Sidecar |
|-----------|:-----------:|:-------------:|:---------------------:|
| Startup simplicity | ★★★★★ | ★★★ | ★★★★ |
| Independent debugging | ★★ | ★★★★★ | ★★★ |
| CLI controllability | ★★★ | ★★★★★ | ★★★★★ |
| Single `symphony dev` command | ★★★★★ | ★★ | ★★★★★ |
| Instance isolation | ★★★★ | ★★★★★ | ★★★★ |
| No extra process mgmt | ★★★★★ | ★ | ★★★★★ |

### Recommendation: **Option C — Embedded Mock Tracker + CLI Sidecar**

The mock tracker runs inside the orchestrator process (zero extra process management), and the control API is exposed on the same HTTP server (alongside the existing dashboard/API). A thin CLI sidecar (`symphony dev` subcommands) discovers the running instance by port and sends commands.

This gives us:
- `symphony WORKFLOW.md --dev` — one command starts everything
- `symphony dev add-issue --port 8234 --title "Fix login"` — control from another terminal
- Orchestrator talks to its own embedded mock tracker at `http://127.0.0.1:{port}`
- No process coordination, no port discovery files

## 5. Recommended Approach

### 5.1 Architecture Overview

```
Terminal 1:                        Terminal 2:
symphony WORKFLOW.md --dev         symphony dev add-issue --port 8234 ...
         │                                    │
         ▼                                    │
┌─────────────────────────────────┐           │
│  Orchestrator Process            │           │
│                                  │           │
│  ┌──────────────┐               │           │
│  │ Mock Tracker  │◄─────────────│───────────┘
│  │ (in-process)  │              │   (HTTP POST /dev/issues)
│  │               │              │
│  │ GET /repos/…/issues ────►────│──→ GitHubTrackerClient
│  └──────────────┘               │      (unchanged code)
│                                  │
│  ┌──────────────┐               │
│  │ Agent Runner  │              │
│  │ (real harness)│              │
│  │ → mock_agent  │              │
│  └──────────────┘               │
│                                  │
│  ┌──────────────┐               │
│  │ HTTP Server   │ :auto-port   │
│  │ /dashboard    │              │
│  │ /api/v1/*     │              │
│  │ /dev/*  ←── control API      │
│  └──────────────┘               │
│                                  │
│  workspace: /tmp/symphony_dev_{instance}/    │
└─────────────────────────────────┘
```

### 5.2 Component Design

#### 5.2.1 Mock Tracker (Embedded HTTP Routes)

The mock tracker adds routes to the existing FastAPI server under a `/_dev/github/` prefix that mimic the GitHub Issues REST API:

```python
# Routes that GitHubTrackerClient will hit (tracker.endpoint = http://127.0.0.1:{port}/_dev/github):
GET  /_dev/github/repos/{owner}/{repo}/issues          → list issues (with state/pagination)
GET  /_dev/github/repos/{owner}/{repo}/issues/{number} → get single issue

# Control API (for CLI sidecar):
POST   /dev/issues                         → create/add an issue
PATCH  /dev/issues/{number}                → update issue state/labels/title
DELETE /dev/issues/{number}                → remove an issue
GET    /dev/issues                         → list all issues (unfiltered, for debugging)
POST   /dev/issues/seed                    → bulk-create N synthetic issues
POST   /dev/errors                         → inject error responses (like FakeGitHub.inject_error)
DELETE /dev/errors                         → clear injected errors
```

**Why `/_dev/github/` prefix**: Avoids any route conflicts with the existing dashboard routes (`/`, `/api/v1/*`, `/{identifier}`). The `GitHubTrackerClient` is pointed at `http://127.0.0.1:{port}/_dev/github` as its endpoint, so it constructs URLs like `/_dev/github/repos/dev/local/issues` — no ambiguity.

**State storage**: In-memory dict (like `FakeGitHub.issues`). Optionally persisted to `{workspace_root}/.tracker-state.json` if `N1` is implemented.

#### 5.2.2 Config Overlay in Dev Mode (Persistent)

The dev overlay is a persistent layer applied **after every config load/reload**. It is not a one-time mutation — it survives `_check_workflow_reload()`.

When `--dev` is active, config resolution applies these overrides:

```python
# Applied on every config load (initial + reload):
tracker.endpoint  → "http://127.0.0.1:{actual_port}/_dev/github"  (self-referential)
tracker.api_key   → "dev-token"  (dummy, mock tracker ignores auth)
tracker.repo      → "dev/local"  (required by GitHubTrackerClient URL construction)
workspace.root    → "{original_workspace.root}/_dev_{instance}/"
agent.harness     → "dev"  (selects DevHarness which launches mock_agent.py)
polling.interval_ms → dev.poll_interval_ms (5000, from dev: section)
```

Note: `tracker.repo` is set to a dummy value `"dev/local"` because `GitHubTrackerClient` constructs URLs like `/repos/{repo}/issues` — it cannot be empty. The mock tracker accepts any owner/repo path.

The `dev:` WORKFLOW.md section configures agent behavior:

```yaml
dev:
  agent_behavior: success     # success | fail | multi-turn | slow
  agent_turns: 3              # turns for multi-turn mode
  agent_delay_ms: 2000        # simulated work duration per turn
  poll_interval_ms: 5000      # faster iteration in dev
```

#### 5.2.3 CLI Interface

```
# Start dev mode (one command does everything):
symphony WORKFLOW.md --dev [--instance NAME] [--port PORT] [--dev-seed N]

# Control a running dev instance (from another terminal):
symphony dev add-issue --port PORT --title "Fix login" [--state open] [--labels bug,p1]
symphony dev set-state --port PORT --number 5 --state closed
symphony dev list-issues --port PORT
symphony dev inject-error --port PORT --key list --status 500
symphony dev clear-errors --port PORT
symphony dev seed --port PORT --count 10
```

The `symphony dev *` subcommands are thin HTTP clients that POST/PATCH to the control API.

#### 5.2.4 Agent Runner in Dev Mode: DevHarness

**Problem**: The current `CopilotAgentSession` creates `SubprocessConfig(...)` and lets the Copilot SDK manage its own binary discovery. There is no way to tell the SDK to launch `mock_agent.py` instead — `copilot_command` exists in config but is not wired into the SDK's subprocess path.

**Solution**: Introduce a `DevHarness` that implements the `AgentHarness` protocol (from the harness design doc) and directly launches `mock_agent.py` as a raw subprocess with JSONRPC-over-stdio. This bypasses the Copilot SDK entirely while exercising the same runner contract.

```python
# symphony/dev_harness.py

class DevHarness:
    """Agent harness for dev mode — launches mock_agent.py directly.
    
    Implements the AgentHarness protocol (start/run_turn/stop/session)
    using raw JSONRPC-over-stdio, matching the mock_agent.py protocol.
    """

    def __init__(
        self,
        config: ServiceConfig,
        workspace_path: str,
        issue: Issue,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace_path
        self._issue = issue
        self._on_event = on_event
        self._session = LiveSession()
        self._proc: asyncio.subprocess.Process | None = None
        self._started = False

    @property
    def session(self) -> LiveSession:
        return self._session

    async def start(self) -> None:
        """Launch mock_agent.py as a subprocess."""
        cfg = {
            "turns": self._config.dev_agent_turns,
            "behavior": self._config.dev_agent_behavior,
            "slow_turn_ms": self._config.dev_agent_delay_ms,
        }
        cmd = f"{sys.executable} {MOCK_AGENT_PATH} {shlex.quote(json.dumps(cfg))}"
        
        self._proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=self._workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        # Send initialize request
        await self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        resp = await self._recv()  # capabilities response
        
        # Send thread/create
        await self._send({"jsonrpc": "2.0", "id": 2, "method": "thread/create", "params": {}})
        resp = await self._recv()
        self._session.thread_id = resp.get("result", {}).get("threadId", "dev-thread")
        self._session.session_id = self._session.thread_id
        self._started = True
        self._emit("session_started")

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Execute one turn via JSONRPC."""
        self._session.turn_count += 1
        await self._send({"jsonrpc": "2.0", "id": 100 + turn_number, "method": "turn/start", "params": {"prompt": prompt}})
        resp = await self._recv()  # turnId ack
        
        # Read events until terminal
        while True:
            event = await self._recv()
            method = event.get("method", "")
            if method == "turn/completed":
                self._emit("turn_completed")
                return True
            elif method == "turn/failed":
                raise TurnFailedError(str(event.get("params", {}).get("error", "mock failure")))
            elif method == "turn/cancelled":
                raise TurnCancelledError()
            elif method == "turn/inputRequired":
                raise TurnInputRequiredError()
            # Other events (token usage, notifications) — emit and continue
            elif method:
                self._emit("notification", message=method)

    async def stop(self) -> None:
        """Terminate the mock agent subprocess."""
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()
        self._started = False
```

This `DevHarness` is selected by the harness factory when `agent.harness` is set to `"dev"`:

```python
# In runner.py factory:
def _create_harness(...) -> AgentHarness:
    harness_name = config.agent_harness
    if harness_name == "dev":
        from symphony.dev_harness import DevHarness
        return DevHarness(config, workspace_path, issue, on_event=on_event)
    elif harness_name == "copilot":
        return CopilotHarness(config, workspace_path, issue, on_event=on_event)
    # ...
```

**Why this works**: The `DevHarness` speaks the same JSONRPC protocol that `mock_agent.py` expects (proven by integration tests). It exercises the full orchestrator → workspace → runner → harness lifecycle. The only difference is the agent subprocess is `mock_agent.py` instead of real Copilot SDK.

#### 5.2.5 Instance Isolation

Each `--dev` invocation generates or receives an instance ID:

```python
instance_id = args.instance or uuid.uuid4().hex[:8]
```

This ID namespaces:
- **Workspace root**: `{configured_root}/_dev_{instance_id}/`
- **Log context**: All log lines include `instance={instance_id}`
- **PID file** (optional): `{workspace_root}/.symphony-dev.pid` for discoverability

#### 5.2.6 Port Auto-Assignment

When dev mode is active and no explicit `--port` is given, the server binds port `0` (OS assigns an available port). The actual port is:
- Printed to stdout on startup: `Dev mode listening on port 8234`
- Written to `{workspace_root}/.symphony-dev.port` for CLI sidecar discovery
- Available via the dashboard at `http://127.0.0.1:{port}/`

### 5.3 Module Design

#### New file: `symphony/dev.py`

```python
"""Dev mode components — mock tracker and HTTP control API routes."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("symphony.dev")


class MockTracker:
    """In-memory mock GitHub Issues tracker with HTTP control API."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.errors: dict[str, tuple[int, str]] = {}
        self._next_id = 1

    def add_issue(
        self,
        number: int | None = None,
        title: str = "Dev issue",
        state: str = "open",
        labels: list[str] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        """Add or update an issue."""
        if number is None:
            number = self._next_id
            self._next_id += 1
        elif number >= self._next_id:
            self._next_id = number + 1

        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": number * 1000,
            "node_id": f"DEV_{number}",
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "html_url": f"http://localhost/dev/issues/{number}",
            "labels": [{"name": lbl} for lbl in (labels or [])],
            "created_at": now,
            "updated_at": now,
        }
        self.issues[number] = entry
        return entry

    def set_state(self, number: int, state: str) -> bool:
        if number not in self.issues:
            return False
        self.issues[number]["state"] = state
        self.issues[number]["updated_at"] = datetime.now(timezone.utc).isoformat()
        return True

    def remove_issue(self, number: int) -> bool:
        return self.issues.pop(number, None) is not None

    def seed(self, count: int) -> list[dict[str, Any]]:
        """Generate N synthetic issues."""
        created = []
        for i in range(count):
            issue = self.add_issue(
                title=f"Synthetic issue {self._next_id}",
                state="open",
                labels=[f"priority/{(i % 4) + 1}"],
                body=f"Auto-generated dev issue #{self._next_id}",
            )
            created.append(issue)
        return created

    def list_issues(self, state: str | None = None) -> list[dict[str, Any]]:
        issues = sorted(self.issues.values(), key=lambda x: x["number"])
        if state:
            issues = [i for i in issues if i["state"].lower() == state.lower()]
        return issues

    def get_issue(self, number: int) -> dict[str, Any] | None:
        return self.issues.get(number)

    def inject_error(self, key: str, status: int, body: str = "error") -> None:
        self.errors[key] = (status, body)

    def clear_errors(self) -> None:
        self.errors.clear()


def mount_dev_routes(app: FastAPI, tracker: MockTracker) -> None:
    """Mount mock GitHub API routes and control API on the FastAPI app."""

    # --- GitHub API compatible routes under /_dev/github/ prefix ---
    # (orchestrator's GitHubTrackerClient points tracker.endpoint here)

    @app.get("/_dev/github/repos/{owner}/{repo}/issues")
    async def github_list_issues(owner: str, repo: str, request: Request):
        params = dict(request.query_params)
        # Check for injected errors
        for key, (status, body) in tracker.errors.items():
            if key == "list":
                return JSONResponse({"message": body}, status_code=status)

        state = params.get("state", "open")
        per_page = int(params.get("per_page", "50"))
        page = int(params.get("page", "1"))

        issues = tracker.list_issues(state)
        start = (page - 1) * per_page
        page_data = issues[start : start + per_page]
        return JSONResponse(page_data)

    @app.get("/_dev/github/repos/{owner}/{repo}/issues/{number}")
    async def github_get_issue(owner: str, repo: str, number: int):
        for key, (status, body) in tracker.errors.items():
            if key == f"issue:{number}":
                return JSONResponse({"message": body}, status_code=status)

        issue = tracker.get_issue(number)
        if issue is None:
            return JSONResponse({"message": "Not Found"}, status_code=404)
        return JSONResponse(issue)

    # --- Control API (CLI sidecar talks to these) ---

    @app.post("/dev/issues")
    async def dev_create_issue(request: Request):
        data = await request.json()
        issue = tracker.add_issue(
            number=data.get("number"),
            title=data.get("title", "Untitled"),
            state=data.get("state", "open"),
            labels=data.get("labels"),
            body=data.get("body", ""),
        )
        return JSONResponse(issue, status_code=201)

    @app.patch("/dev/issues/{number}")
    async def dev_update_issue(number: int, request: Request):
        data = await request.json()
        if number not in tracker.issues:
            return JSONResponse({"error": "not found"}, status_code=404)
        if "state" in data:
            tracker.set_state(number, data["state"])
        if "title" in data:
            tracker.issues[number]["title"] = data["title"]
        if "labels" in data:
            tracker.issues[number]["labels"] = [
                {"name": lbl} for lbl in data["labels"]
            ]
        return JSONResponse(tracker.issues[number])

    @app.delete("/dev/issues/{number}")
    async def dev_delete_issue(number: int):
        if tracker.remove_issue(number):
            return JSONResponse({"deleted": True})
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/dev/issues")
    async def dev_list_all_issues():
        return JSONResponse(tracker.list_issues())

    @app.post("/dev/issues/seed")
    async def dev_seed_issues(request: Request):
        data = await request.json()
        count = data.get("count", 5)
        created = tracker.seed(count)
        return JSONResponse({"created": len(created), "issues": created})

    @app.post("/dev/errors")
    async def dev_inject_error(request: Request):
        data = await request.json()
        tracker.inject_error(data["key"], data["status"], data.get("body", "error"))
        return JSONResponse({"injected": True})

    @app.delete("/dev/errors")
    async def dev_clear_errors():
        tracker.clear_errors()
        return JSONResponse({"cleared": True})


def generate_instance_id() -> str:
    """Generate a short random instance ID."""
    return uuid.uuid4().hex[:8]


def dev_workspace_root(base_root: str, instance_id: str) -> str:
    """Compute isolated workspace root for a dev instance."""
    return os.path.join(base_root, f"_dev_{instance_id}")
```

#### New file: `symphony/dev_harness.py`

See §5.2.4 above for the full `DevHarness` implementation. The harness:
- Launches `mock_agent.py` as a raw subprocess (no SDK dependency)
- Speaks JSONRPC-over-stdio (same protocol as the integration tests)
- Implements the `AgentHarness` Protocol (start/run_turn/stop/session)
- Is selected by the harness factory when `agent.harness == "dev"`
- The mock_agent.py script is bundled as package data at `symphony/dev_mock_agent.py` (copied from `tests/integration/mock_agent.py` to avoid runtime test-directory dependency)

#### Modified: `symphony/cli.py`

Add `--dev`, `--instance`, `--dev-seed` flags and `dev` subcommand group:

```python
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(...)

    subparsers = parser.add_subparsers(dest="command")

    # Main run mode (default, no subcommand)
    parser.add_argument("workflow_path", nargs="?", ...)
    parser.add_argument("--port", ...)
    parser.add_argument("--dev", action="store_true", help="Enable dev mode with mock tracker")
    parser.add_argument("--instance", type=str, default=None, help="Dev instance ID (auto-generated if omitted)")
    parser.add_argument("--dev-seed", type=int, default=0, help="Pre-populate mock tracker with N issues")

    # Dev subcommands (CLI sidecar)
    dev_parser = subparsers.add_parser("dev", help="Control a running dev instance")
    dev_sub = dev_parser.add_subparsers(dest="dev_command")

    add_p = dev_sub.add_parser("add-issue")
    add_p.add_argument("--port", type=int, required=True)
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--state", default="open")
    add_p.add_argument("--labels", default="")
    add_p.add_argument("--body", default="")
    add_p.add_argument("--number", type=int, default=None)

    state_p = dev_sub.add_parser("set-state")
    state_p.add_argument("--port", type=int, required=True)
    state_p.add_argument("--number", type=int, required=True)
    state_p.add_argument("--state", required=True)

    # ... more subcommands ...
```

#### Modified: `symphony/orchestrator.py`

When dev mode is active, the orchestrator:
1. Creates `MockTracker` and mounts dev routes on the HTTP server BEFORE polling starts
2. Starts HTTP server first to get actual port
3. Applies persistent dev overlay (survives workflow reloads)
4. Points `tracker.endpoint` at its own mock server
5. Selects `DevHarness` via `agent.harness = "dev"` config overlay
6. Seeds initial issues if `--dev-seed` was given

```python
class Orchestrator:
    def __init__(
        self,
        workflow_path: str,
        port: int | None = None,
        dev_mode: bool = False,
        dev_instance: str | None = None,
        dev_seed: int = 0,
    ) -> None:
        # ... existing init ...
        self._dev_mode = dev_mode
        self._dev_instance = dev_instance or generate_instance_id()
        self._dev_seed = dev_seed
        self._mock_tracker: MockTracker | None = None
        self._dev_actual_port: int | None = None

    async def start(self) -> None:
        """Start the orchestrator. In dev mode, HTTP server starts first."""
        self._loop = asyncio.get_running_loop()
        self._running = True

        # Load workflow
        errors = self._load_and_apply_workflow()
        if errors:
            raise SymphonyError(...)

        if self._dev_mode:
            # 1. Apply dev overlay (workspace root, agent harness, etc.)
            self._apply_dev_overlay()
            # 2. Create mock tracker
            self._mock_tracker = MockTracker()
            if self._dev_seed > 0:
                self._mock_tracker.seed(self._dev_seed)
            # 3. Start HTTP server to get port (mount dev routes first)
            # ... server startup with dev routes ...
            # 4. Finalize tracker endpoint with actual port
            self._finalize_dev_endpoint(actual_port)
            # 5. Create tracker client pointing at self
            self._tracker = GitHubTrackerClient(
                endpoint=f"http://127.0.0.1:{actual_port}/_dev/github",
                api_key="dev-token",
                repo="dev/local",
                active_states=cfg.active_states,
                terminal_states=cfg.terminal_states,
            )

        # ... rest of startup (cleanup, event processor, tick) ...

    def _apply_dev_overlay(self) -> None:
        """Apply persistent dev mode overrides to current config.
        Called on initial load AND every workflow reload."""
        # Mutate the raw config dict to inject dev values
        ...

    def _check_workflow_reload(self) -> None:
        """Reload workflow, then reapply dev overlay if in dev mode."""
        # ... existing reload logic ...
        if not errors and self._dev_mode:
            self._apply_dev_overlay()
```

#### Modified: `symphony/config.py`

Add `dev_*` property accessors:

```python
@property
def dev_agent_behavior(self) -> str:
    return str(_get(self._raw, "dev", "agent_behavior", default="success") or "success")

@property
def dev_agent_turns(self) -> int:
    val = _get(self._raw, "dev", "agent_turns", default=3)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 3

@property
def dev_agent_delay_ms(self) -> int:
    val = _get(self._raw, "dev", "agent_delay_ms", default=2000)
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 2000

@property
def dev_poll_interval_ms(self) -> int:
    val = _get(self._raw, "dev", "poll_interval_ms", default=5000)
    try:
        return max(1000, int(val))
    except (TypeError, ValueError):
        return 5000
```

### 5.4 Startup Sequence in Dev Mode

The critical constraint: the HTTP server must be listening BEFORE the tracker client is initialized, because the tracker client needs a real endpoint to connect to.

```
1.  Parse CLI: --dev --instance alice --dev-seed 5
2.  Load WORKFLOW.md normally (config, prompt)
3.  Generate instance_id = "alice" (or random 8-char hex)
4.  Apply dev overlay (persistent — reapplied on every reload):
    - workspace.root → /tmp/symphony_workspaces/_dev_alice/
    - tracker.repo → "dev/local" (dummy, mock tracker accepts any)
    - tracker.api_key → "dev-token" (dummy)
    - polling.interval_ms → dev.poll_interval_ms (5000)
    - agent.harness → "dev" (selects DevHarness)
5.  Create MockTracker, seed 5 issues
6.  Create HTTP server, mount dev routes under /_dev/github/ prefix
7.  Start HTTP server on port 0 (auto-assign) → get actual_port
8.  Set tracker.endpoint → http://127.0.0.1:{actual_port}/_dev/github
9.  Create GitHubTrackerClient with the self-referential endpoint
10. Write port to {workspace_root}/.symphony-dev.port
11. Print: "Dev mode [alice] listening on port {actual_port}"
12. Run startup terminal cleanup (hits mock tracker — safe, returns empty)
13. Begin polling (hits mock tracker, dispatches seeded issues)
```

**On workflow reload** (`_check_workflow_reload()`): The dev overlay is reapplied AFTER the new config is loaded. This prevents a file save from reverting dev mode settings to production values.

```python
def _check_workflow_reload(self) -> None:
    # ... existing reload logic ...
    errors = self._load_and_apply_workflow()
    if not errors and self._dev_mode:
        self._apply_dev_overlay()  # persistent — never loses dev settings
```

### 5.5 CLI Sidecar Commands

The `symphony dev` subcommands are stateless HTTP clients:

```python
async def _dev_command(args: argparse.Namespace) -> int:
    """Execute a dev control command against a running instance."""
    import httpx

    base = f"http://127.0.0.1:{args.port}"

    if args.dev_command == "add-issue":
        resp = await httpx.AsyncClient().post(f"{base}/dev/issues", json={
            "title": args.title,
            "state": args.state,
            "labels": [l.strip() for l in args.labels.split(",") if l.strip()],
            "body": args.body,
            "number": args.number,
        })
        print(json.dumps(resp.json(), indent=2))

    elif args.dev_command == "set-state":
        resp = await httpx.AsyncClient().patch(
            f"{base}/dev/issues/{args.number}",
            json={"state": args.state},
        )
        print(json.dumps(resp.json(), indent=2))

    # ... etc ...
    return 0
```

### 5.6 Interaction Examples

```bash
# Terminal 1: Start Symphony in dev mode
$ uv run symphony WORKFLOW.md --dev --instance alice --dev-seed 3
[INFO] Dev mode [alice] listening on port 8234
[INFO] Mock tracker seeded with 3 issues
[INFO] Workspace root: /tmp/symphony_workspaces/_dev_alice/
[INFO] Agent command: python3 mock_agent.py '{"behavior":"success","turns":3}'
[INFO] orchestrator_started poll_interval_ms=5000
[INFO] dispatch issue_id=1000 issue_identifier=#1 attempt=None
...

# Terminal 2: Add a new issue while it's running
$ uv run symphony dev add-issue --port 8234 --title "Urgent hotfix" --labels bug,priority/1
{"number": 4, "title": "Urgent hotfix", "state": "open", ...}

# Terminal 2: Close an issue to test reconciliation
$ uv run symphony dev set-state --port 8234 --number 1 --state closed

# Terminal 2: Inject a tracker error to test retry logic
$ uv run symphony dev inject-error --port 8234 --key list --status 500

# Terminal 3: Start ANOTHER instance simultaneously
$ uv run symphony WORKFLOW.md --dev --instance bob --dev-seed 2
[INFO] Dev mode [bob] listening on port 8301
[INFO] Workspace root: /tmp/symphony_workspaces/_dev_bob/
...
```

### 5.7 Validation Bypass in Dev Mode

When `--dev` is active, `ServiceConfig.validate_dispatch()` skips only the API key check (since the mock tracker ignores auth). `tracker.repo` is NOT skipped — instead, the dev overlay injects `"dev/local"` as a deterministic dummy value.

The bypass is conditional and minimal:

```python
def validate_dispatch(self, dev_mode: bool = False) -> list[str]:
    errors: list[str] = []
    if not self.tracker_kind:
        errors.append("tracker.kind is required")
    elif self.tracker_kind != "github":
        errors.append(f"Unsupported tracker.kind: {self.tracker_kind!r}")
    if not dev_mode:
        if not self.tracker_api_key:
            errors.append("tracker.api_key is missing")
    if self.tracker_kind == "github" and not self.tracker_repo:
        errors.append("tracker.repo is required")
    return errors
```

## 6. Migration Plan

### Phase 1: Mock Tracker + Dev Routes

- Implement `MockTracker` class (elevated from `FakeGitHub` test fixture)
- Implement `mount_dev_routes()` for FastAPI
- Add `--dev` flag to CLI with workspace namespacing
- Add port auto-assignment (already partially supported by uvicorn port=0 code)

**Verification**: Start in dev mode, see mock tracker serving issues to the orchestrator.

### Phase 2: CLI Control Sidecar

- Add `symphony dev` subcommand group
- Implement `add-issue`, `set-state`, `list-issues`, `inject-error`, `clear-errors`, `seed`
- Write port file for discovery

**Verification**: Control running instance from second terminal.

### Phase 3: DevHarness Implementation

- Create `symphony/dev_harness.py` implementing `AgentHarness` Protocol
- Copy `mock_agent.py` to `symphony/dev_mock_agent.py` (bundled as package data)
- Implement raw JSONRPC-over-stdio client (initialize, thread/create, turn/start, event reading)
- Wire into harness factory: `agent.harness == "dev"` → `DevHarness`
- Read `dev.agent_behavior`, `dev.agent_turns`, `dev.agent_delay_ms` from config

**Verification**: Issues dispatch, DevHarness launches mock agent, turns execute, worker completes, continuation retries fire.

### Phase 4: Multi-Instance Smoke Test

- Run 2+ instances with different `--instance` values
- Verify workspace isolation, port isolation, independent issue state

**Verification**: Both instances run simultaneously without any interference.

## 7. Test Strategy

### Unit Tests (`tests/test_dev.py`)

- `MockTracker.add_issue()` — assigns auto-incrementing numbers
- `MockTracker.set_state()` — updates state, returns False for missing
- `MockTracker.seed()` — creates N issues with varied priorities
- `MockTracker.list_issues(state=)` — filters correctly
- `MockTracker.inject_error()` / `clear_errors()` — error injection
- `dev_workspace_root()` — correct path construction
- `mock_agent_command()` — produces valid shell command

### Integration Tests (`tests/integration/test_dev_mode.py`)

- Full lifecycle: start orchestrator in dev mode, verify it dispatches seeded issues
- CLI sidecar: add issue via control API, verify orchestrator picks it up on next tick
- State change: close issue via control API, verify orchestrator stops/reconciles
- Error injection: inject 500 on list, verify orchestrator logs error and retries
- Multi-instance: two orchestrators with different instance IDs, verify isolated workspaces

### Existing Test Compatibility

All existing tests continue to use `FakeGitHub` + `respx` (no changes). The dev mode is a runtime concern, not a test-infrastructure change.

## 8. Risk Assessment

### Risks of Implementing

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Route conflicts between mock GitHub API and real dashboard API | Medium | High | Mount GitHub routes under a dev-only prefix OR use path matching order carefully |
| Port file stale after crash | Low | Low | Delete port file on startup; CLI retries with timeout |
| Mock agent binary path resolution fails in installed mode | Medium | Medium | Bundle mock_agent.py as package data; or embed the behavior inline |

### Risks of NOT Implementing

| Risk | Likelihood | Impact |
|------|-----------|--------|
| Developers accidentally dispatch real agents while testing | High | High |
| Multiple devs block each other when testing concurrently | High | Medium |
| Integration test patterns diverge from dev experience | Medium | Medium |

## 9. Decision Records

### ADR-1: Embedded vs. Standalone Mock Tracker

**Context**: The mock tracker could run as a separate process or embedded in the orchestrator.

**Decision**: Embedded in the orchestrator process, exposed as routes on the same HTTP server.

**Rationale**: Single `symphony --dev` command starts everything. No process coordination needed. The orchestrator's `GitHubTrackerClient` talks to `http://127.0.0.1:{own_port}` which routes to the in-memory `MockTracker`.

**Consequences**: Cannot run mock tracker independently of the orchestrator. Acceptable for dev-mode use case.

### ADR-2: DevHarness Instead of Config Override

**Context**: The design initially assumed we could override `copilot.command` to point at `mock_agent.py`. However, `CopilotAgentSession` uses `SubprocessConfig` which doesn't expose a command override — the SDK manages its own binary discovery internally.

**Decision**: Introduce `DevHarness` — a new `AgentHarness` protocol implementation that directly launches `mock_agent.py` via raw subprocess + JSONRPC-over-stdio, bypassing the Copilot SDK entirely.

**Rationale**: 
- The harness protocol (from the agent harness design doc) already defines the contract: `start()`, `run_turn()`, `stop()`, `session`.
- `mock_agent.py` already speaks the JSONRPC protocol correctly (proven by 200+ integration tests).
- A dedicated harness is cleaner than hacking the SDK's internal binary selection.
- The orchestrator dispatches identically regardless of harness (per R4 of the harness design).

**Consequences**: 
- `mock_agent.py` must be bundled as package data (not just in `tests/`). We'll copy it to `symphony/dev_mock_agent.py`.
- A new file `symphony/dev_harness.py` contains ~100 lines of JSONRPC client code.
- The `agent.harness` config field must be implemented (from the harness design doc) for the factory to route to `DevHarness`.

### ADR-3: Port Auto-Assignment Strategy

**Context**: Need multiple instances on the same machine without port conflicts.

**Decision**: Default to port `0` (OS-assigned) in dev mode. Write actual port to `{workspace_root}/.symphony-dev.port`. Print to stdout.

**Rationale**: Zero configuration needed for multi-instance. CLI sidecar discovers port from the port file or via explicit `--port` flag.

**Consequences**: CLI sidecar needs explicit `--port` if port file isn't in a known location. Acceptable UX.

### ADR-4: Route Prefix for GitHub API Mimicry

**Context**: The mock tracker needs to serve GitHub-compatible routes. The existing server only has `/`, `/favicon.ico`, `/api/v1/*` — no actual conflict with `/repos/...` — but using a prefix is cleaner for clarity and future-proofing.

**Decision**: Mount mock GitHub routes under `/_dev/github/` prefix. Set `tracker.endpoint` to `http://127.0.0.1:{port}/_dev/github`.

**Rationale**: The tracker endpoint is configurable, so we don't need root-level `/repos/...` paths. The prefix makes it immediately obvious which routes are for the mock tracker vs. the real API/dashboard. No risk of route ordering issues with any existing or future routes.

**Consequences**: `GitHubTrackerClient` constructs URLs like `/_dev/github/repos/dev/local/issues?state=open` — standard URL concatenation, no special handling needed.

### ADR-5: Instance Lock File for Duplicate Prevention

**Context**: Two processes with `--dev --instance alice` would share workspace namespace and port file, defeating isolation.

**Decision**: On startup, write a PID lock file at `{workspace_root}/.symphony-dev.pid`. If it already exists and the PID is still alive, fail fast with a clear error message.

**Rationale**: Simple, portable, prevents the most common user error (accidentally starting the same instance twice).

**Consequences**: Must clean up lock file on graceful shutdown. Stale lock files (after crashes) are handled by checking if the PID is still alive.
