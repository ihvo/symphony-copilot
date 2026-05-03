# AGENTS.md

## Project Overview

Symphony is a long-running Python service that polls GitHub Issues, creates isolated per-issue workspaces, and runs Copilot SDK coding-agent sessions against each issue. It is a scheduler/runner and tracker reader — ticket writes are performed by the coding agent, not by Symphony.

The authoritative behavior specification is `SPEC.md` at the repo root.

## Architecture

```
symphony/
  cli.py              ← Entry point. Parses args, starts orchestrator + optional HTTP server.
  orchestrator.py      ← Single-authority poll loop. Owns all mutable scheduling state.
  config.py            ← Typed config layer. Resolves WORKFLOW.md front matter → runtime values.
  workflow.py          ← Parses WORKFLOW.md (YAML front matter + prompt body).
  prompt.py            ← Jinja2 strict-mode template rendering.
  tracker.py           ← GitHub Issues REST client (fetch, paginate, normalize).
  workspace.py         ← Per-issue workspace lifecycle (create, hooks, cleanup, safety).
  runner.py            ← Copilot SDK subprocess client (JSONRPC-over-stdio, multi-turn).
  server.py            ← Optional HTTP server extension (dashboard + JSON API).
  models.py            ← Dataclasses for the domain model.
  errors.py            ← Typed error hierarchy.
  logging_config.py    ← Structured JSON logging to stderr.
```

### Key design decisions

- **asyncio single-writer**: The orchestrator is the only component that mutates scheduling state. Workers communicate back via an `asyncio.Queue` of immutable events. This avoids races without locks.
- **Workspace safety invariants**: All workspace paths are sanitized (`[A-Za-z0-9._-]`), resolved to absolute, and validated to stay inside the workspace root before any filesystem operation or agent launch.
- **Last-known-good config**: Invalid `WORKFLOW.md` reloads keep the previous working config. The service never crashes on a bad reload.
- **Multi-turn sessions**: The agent runner keeps one subprocess alive across multiple turns on the same thread. The subprocess is only stopped when the worker attempt ends.

## Development

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

### Setup

```bash
uv sync
```

### Run tests

```bash
uv run pytest             # all 206 tests
uv run pytest tests/ -q   # quick summary
uv run pytest tests/integration/ -v  # integration tests only (64 tests, ~30s)
```

### Run the service

```bash
uv run symphony WORKFLOW.md              # basic
uv run symphony WORKFLOW.md --port 8080  # with HTTP dashboard
```

## Test Infrastructure

Tests live in `tests/` (unit) and `tests/integration/` (integration).

### Unit tests (142 tests, <1s)

Fast, no I/O. Test each module in isolation with direct function calls and in-memory state.

### Integration tests (64 tests, ~30s)

Wire real components together. Two key pieces of infrastructure:

- **`FakeGitHub`** (`tests/integration/conftest.py`): An in-process aiohttp server that mimics the GitHub Issues REST API. Supports `add_issue()`, `set_state()`, `inject_error()` for test setup. Tracks all requests in `request_log`.

- **`mock_agent.py`** (`tests/integration/mock_agent.py`): A standalone Python script that speaks the JSONRPC-over-stdio protocol. Configure via JSON argument:
  ```python
  agent_command({"turns": 3, "behavior": "success"})               # 3 successful turns
  agent_command({"behavior": "fail"})                               # turn failure
  agent_command({"behavior": "hang", "turns": 1})                   # stall forever
  agent_command({"approval_turn": 0, "turns": 1})                   # sends approval request
  agent_command({"token_usage": {"input": 100, "output": 50}})      # emits token telemetry
  ```

- **`make_workflow`** fixture: Generates a `WORKFLOW.md` file wired to the fake GitHub server and mock agent. Handles YAML escaping properly via `yaml.dump`.

### Adding a new integration test

1. Pick the right file (`test_s17_N_*.py` matching the spec section).
2. Use `fake_github` fixture to set up issue state.
3. Use `make_workflow(endpoint=fake_github.base_url, ...)` to create the workflow.
4. Start the orchestrator, use `wait_until(predicate)` for async assertions.
5. Always `await orch.stop()` in a `finally` block.

## Conventions

- **Error types**: Every error class lives in `errors.py` with a stable `.code` string. Use the existing hierarchy — don't create ad-hoc exceptions.
- **Logging**: Use `logging.getLogger("symphony.<module>")`. All logs are structured JSON. Include `issue_id` and `issue_identifier` in issue-related log messages.
- **Config access**: Never read `WORKFLOW.md` directly in business logic. Use `ServiceConfig` properties which handle defaults, `$VAR` resolution, and validation.
- **Workspace operations**: Always go through `workspace.py` functions. Never construct workspace paths manually — use `workspace_path_for()` and `validate_workspace_path()`.
- **State mutations**: Only the orchestrator's event-processing loop mutates `OrchestratorState`. Workers emit `WorkerResult` or `AgentEvent` to the queue.

## Spec Compliance Notes

This implementation targets all `Core Conformance` requirements (SPEC §18.1) and the `HTTP Server Extension` (§13.7). It does **not** implement:

- `github_graphql` client-side tool extension
- Persistent retry queue across restarts
- Tracker adapters beyond GitHub Issues

The approval/sandbox policy is **high-trust**: auto-approve command execution, auto-approve file changes, fail on user-input-required. This is documented per SPEC §10.5.
