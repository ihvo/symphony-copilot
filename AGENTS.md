# AGENTS.md

Symphony is a Python asyncio service that polls GitHub Issues, creates per-issue workspaces, and runs Copilot SDK agent sessions. Spec: `SPEC.md`. Entry point: `symphony/cli.py`.

## Setup and Run

1. `uv sync`
2. `uv run pytest` — runs all 206 tests (~30s)
3. `uv run pytest tests/ -q` — unit tests only (<1s)
4. `uv run symphony WORKFLOW.md` — run the service
5. `uv run symphony WORKFLOW.md --port 8080` — with HTTP dashboard

### Dashboard (optional — requires Node.js 18+)

```bash
cd dashboard && npm install && npm run build
```

This produces `dashboard/out/` which FastAPI serves automatically. Without it, the dashboard shows a placeholder with build instructions.

**Quick start (build + run):**

```bash
cd dashboard && npm install && npm run build && cd .. && uv run symphony WORKFLOW.md --port 8080
```

**During development:**

```bash
cd dashboard && npm run dev      # starts Next.js on :3000 (proxies API to :8080)
cd dashboard && npm test         # runs 22 Vitest component tests
```

See `dashboard/AGENTS.md` for frontend-specific conventions.

## Module Map

```
cli.py           → arg parsing, starts orchestrator + optional HTTP server
orchestrator.py  → single-authority poll loop, all mutable state lives here
config.py        → typed getters over WORKFLOW.md front matter (defaults, $VAR, ~)
workflow.py      → parses WORKFLOW.md → {config map, prompt body}
prompt.py        → Jinja2 strict-mode rendering
tracker.py       → GitHub Issues REST client via httpx (paginate, normalize, refresh)
workspace.py     → per-issue directory lifecycle + hook execution + safety checks
runner.py        → Copilot SDK JSONRPC-over-stdio subprocess (multi-turn)
server.py        → optional FastAPI HTTP extension (dashboard + /api/v1/*)
models.py        → dataclasses (Issue, RunningEntry, RetryEntry, OrchestratorState, …)
errors.py        → typed error hierarchy with stable .code strings
dashboard/       → Next.js 15 static-export frontend (see dashboard/AGENTS.md)
```

## Making Changes

### Adding a new config field

1. Add the property to `ServiceConfig` in `config.py` with a default value.
2. If dispatch-critical, add validation in `ServiceConfig.validate_dispatch()`.
3. Add a unit test in `tests/test_config.py`.

### Adding a new agent protocol event

1. Handle the event type in `CopilotSession.run_turn()` in `runner.py`.
2. Emit via `self._emit("event_name", ...)` so the orchestrator receives it.
3. If it affects orchestrator state, update `_handle_agent_event()` in `orchestrator.py`.
4. Add a behavior to `tests/integration/mock_agent.py` and write an integration test.

### Adding a new workspace hook

1. Add the hook property in `config.py` (return `str | None`).
2. Call `run_hook()` at the right lifecycle point in `workspace.py` or `orchestrator.py`.
3. Document failure semantics: fatal (like `after_create`) or best-effort (like `after_run`).

## Where to Put Things

| Need | Location |
|---|---|
| New error type | `errors.py` — subclass `SymphonyError`, set `.code` |
| New dataclass | `models.py` |
| Config with default + validation | `config.py` `ServiceConfig` property |
| Issue tracker API call | `tracker.py` `GitHubTrackerClient` method |
| Filesystem operation on workspaces | `workspace.py` — always use `validate_workspace_path()` |
| Scheduling state mutation | `orchestrator.py` — only in event-processing or tick methods |
| New HTTP endpoint | `server.py` `_setup_routes()` |
| New dashboard component | `dashboard/src/components/` — see `dashboard/AGENTS.md` |
| New React hook | `dashboard/src/hooks/` |
| Dashboard test | `dashboard/src/__tests__/` — Vitest + RTL |

## Critical Invariants

- **Don't** mutate `OrchestratorState` from worker tasks. **Do** emit `WorkerResult` or `AgentEvent` to `self._event_queue` and let the orchestrator's event loop apply the mutation.
- **Don't** construct workspace paths with string concatenation. **Do** use `workspace_path_for()` and `validate_workspace_path()` from `workspace.py`.
- **Don't** read `WORKFLOW.md` directly in business logic. **Do** access values through `ServiceConfig` properties which handle defaults and `$VAR` resolution.
- **Don't** create ad-hoc exception classes. **Do** add them to `errors.py` with a stable `.code` string.
- **Don't** log raw API tokens. **Do** validate secret presence without printing values.

## Test Infrastructure

Unit tests in `tests/`, integration tests in `tests/integration/`.

### Integration test components

- **`FakeGitHub`** (`conftest.py`): in-process aiohttp server. Use `fake_github.add_issue(N, state="open")` to set up state, `fake_github.inject_error("list", 500)` to simulate failures.
- **`mock_agent.py`**: standalone subprocess speaking JSONRPC. Configure via `agent_command({"turns": 2, "behavior": "success"})`. Behaviors: `success`, `fail`, `cancel`, `input_required`, `exit`, `hang`, `error_response`.
- **`make_workflow`** fixture: generates `WORKFLOW.md` wired to fake server + mock agent.

### Writing an integration test

1. Use `fake_github` fixture for issue state.
2. Use `make_workflow(endpoint=fake_github.base_url, agent_cfg={...})`.
3. Start orchestrator: `orch = Orchestrator(wf); await orch.start()`.
4. Assert with `await wait_until(lambda: ..., timeout=5.0)`.
5. Always `await orch.stop()` in a `finally` block.

## Spec Compliance

Targets all Core Conformance requirements (SPEC §18.1) plus the HTTP Server Extension (§13.7). Approval policy is high-trust: auto-approve commands/files, fail on user-input-required (SPEC §10.5). Not implemented: `github_graphql` tool, persistent retry queue, non-GitHub trackers.
