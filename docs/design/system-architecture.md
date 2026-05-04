# Symphony System Architecture

**Status:** Implemented
**Author:** Symphony Team
**Package:** symphony

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 1.0 | 2026-05-04 | Consolidated from SPEC.md; reflects implemented codebase |

## Table of Contents

1. [Overview](#1-overview)
2. [Domain Model](#2-domain-model)
3. [Workflow Contract](#3-workflow-contract)
4. [Configuration](#4-configuration)
5. [Orchestration State Machine](#5-orchestration-state-machine)
6. [Polling, Scheduling, and Reconciliation](#6-polling-scheduling-and-reconciliation)
7. [Workspace Management](#7-workspace-management)
8. [Issue Tracker Integration](#8-issue-tracker-integration)
9. [Prompt Construction](#9-prompt-construction)
10. [Observability and HTTP API](#10-observability-and-http-api)
11. [Failure Model and Recovery](#11-failure-model-and-recovery)
12. [Security and Operational Safety](#12-security-and-operational-safety)

## Related Documents

| Document | Scope |
|----------|-------|
| `docs/design/agent-execution-architecture.md` | Agent harness protocol, SDK client model, turn execution, error mapping |
| `docs/design/claude-sdk-agent-harness.md` | Claude harness design rationale and options evaluation |
| `docs/design/dashboard-frontend-redesign.md` | Dashboard UI architecture (Next.js static export) |

---

## 1. Overview

Symphony is a long-running automation service that continuously reads work from GitHub Issues,
creates an isolated workspace for each issue, and runs a coding agent session inside that workspace.

### Components

```
┌──────────────────────────────────────────────────────────────────────┐
│                          CLI (cli.py)                                 │
│  Parses args, starts orchestrator + optional HTTP server             │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│                     Orchestrator (orchestrator.py)                    │
│  Single-authority poll loop. Owns all mutable scheduling state.      │
│  Dispatch • Reconcile • Retry • Stall detection • Event processing   │
└──┬──────────────┬──────────────┬─────────────────┬───────────────────┘
   │              │              │                 │
   ▼              ▼              ▼                 ▼
┌───────┐  ┌──────────┐  ┌───────────┐  ┌─────────────────┐
│Config │  │Tracker   │  │Workspace  │  │Agent Runner      │
│Layer  │  │Client    │  │Manager    │  │(run_agent_session)│
│       │  │(httpx)   │  │           │  │                  │
│config │  │tracker   │  │workspace  │  │runner.py         │
│.py    │  │.py       │  │.py        │  │claude_runner.py  │
└───────┘  └──────────┘  └───────────┘  └─────────────────┘
```

### What Symphony Does

- Polls GitHub Issues on a fixed cadence and dispatches work with bounded concurrency.
- Maintains a single authoritative in-memory state for dispatch, retries, and reconciliation.
- Creates deterministic per-issue workspaces and preserves them across runs.
- Stops active runs when issue state changes make them ineligible.
- Recovers from transient failures with exponential backoff.
- Loads runtime behavior from a repository-owned `WORKFLOW.md` contract.
- Exposes structured logs and an optional HTTP dashboard/API.

### What Symphony Does NOT Do

- Prescribe a specific UI or dashboard implementation.
- Write to the issue tracker (state transitions, comments, PRs are handled by the agent).
- Persist scheduler state across restarts (in-memory only; recovers by re-polling).
- Provide sandboxing beyond workspace isolation (host OS and agent SDK handle this).

---

## 2. Domain Model

### Issue

Normalized issue record from the tracker.

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Stable tracker-internal ID |
| `identifier` | string | Human-readable key (e.g., `#123`) |
| `title` | string | Issue title |
| `description` | string or null | Issue body |
| `priority` | integer or null | Lower = higher priority |
| `state` | string | Current tracker state name |
| `branch_name` | string or null | Tracker-provided branch metadata |
| `url` | string or null | Web URL |
| `labels` | list of strings | Normalized to lowercase |
| `blocked_by` | list of blocker refs | Each has id, identifier, state |
| `created_at` | timestamp or null | |
| `updated_at` | timestamp or null | |

### Orchestrator State

Single authoritative in-memory state owned by the orchestrator.

| Field | Type | Description |
|-------|------|-------------|
| `poll_interval_ms` | integer | Current effective poll interval |
| `max_concurrent_agents` | integer | Current global concurrency limit |
| `running` | map issue_id → RunningEntry | Active sessions |
| `claimed` | set of issue_ids | Reserved/running/retrying |
| `retry_attempts` | map issue_id → RetryEntry | Pending retries |
| `completed` | set of issue_ids | Bookkeeping only |
| `copilot_totals` | TokenTotals | Aggregate tokens + runtime |
| `copilot_rate_limits` | object or null | Latest rate-limit snapshot |

### Stable Identifiers

- **Issue ID** — tracker lookups and internal map keys
- **Issue Identifier** — human-readable logs, workspace naming
- **Workspace Key** — derived from identifier: replace non-`[A-Za-z0-9._-]` with `_`
- **Session ID** — composed as `<thread_id>-<turn_id>`
- **State comparison** — always lowercased

---

## 3. Workflow Contract

### File Discovery

1. Explicit CLI path argument
2. Default: `WORKFLOW.md` in cwd

### File Format

```
---
<YAML front matter>
---
<Markdown prompt template body>
```

- If file starts with `---`, parse until next `---` as YAML front matter
- Remaining lines = prompt body (trimmed)
- If no front matter, entire file = prompt body, empty config map
- Front matter MUST decode to a map; non-map is an error

### Front Matter Schema

Top-level keys: `tracker`, `polling`, `workspace`, `hooks`, `agent`, `copilot`, `claude`, `server`

Unknown keys are ignored for forward compatibility.

### Prompt Template

- Strict template engine (Jinja2 in implementation)
- Unknown variables MUST fail rendering
- Unknown filters MUST fail rendering
- Template variables: `issue` (object with all fields), `attempt` (integer or null)
- Empty prompt body → minimal default: `"You are working on a GitHub issue."`
- Parse/render failures are per-attempt errors (not global)

### Dynamic Reload

- WORKFLOW.md changes are detected and re-applied without restart
- Reloaded config applies to future dispatch, retry scheduling, hooks, agent launches
- In-flight sessions are NOT restarted on config change
- Invalid reloads keep last-known-good config and emit operator-visible error

---

## 4. Configuration

### Resolution Pipeline

1. Select workflow file path (CLI arg or cwd default)
2. Parse YAML front matter into raw config map
3. Apply built-in defaults for missing fields
4. Resolve `$VAR_NAME` indirection for values containing `$VAR_NAME`
5. Coerce and validate typed values

Environment variables do NOT globally override YAML. Used only when explicitly referenced.

### `tracker` Section

| Field | Default | Description |
|-------|---------|-------------|
| `kind` | — (REQUIRED) | Currently: `github` |
| `endpoint` | `https://api.github.com` | API base URL |
| `api_key` | — (REQUIRED) | Token or `$VAR_NAME` |
| `repo` | — (REQUIRED) | `owner/repo` format |
| `active_states` | `["open"]` | States eligible for dispatch |
| `terminal_states` | `["closed"]` | States triggering cleanup |

### `polling` Section

| Field | Default | Description |
|-------|---------|-------------|
| `interval_ms` | `30000` | Poll cadence (hot-reloaded) |

### `workspace` Section

| Field | Default | Description |
|-------|---------|-------------|
| `root` | `<tmpdir>/symphony_workspaces` | Supports `~` and `$VAR` |

### `hooks` Section

| Field | Default | Description |
|-------|---------|-------------|
| `after_create` | null | Shell script; failure = fatal |
| `before_run` | null | Shell script; failure = aborts attempt |
| `after_run` | null | Shell script; failure = logged, ignored |
| `before_remove` | null | Shell script; failure = logged, ignored |
| `timeout_ms` | `60000` | Applies to all hooks |

### `agent`, `copilot`, `claude` Sections

See `docs/design/agent-execution-architecture.md` §6 for full configuration contract.

### `server` Section (extension)

| Field | Default | Description |
|-------|---------|-------------|
| `port` | null (disabled) | Enables HTTP server; `0` = ephemeral |

CLI `--port` overrides `server.port`. Binds loopback by default.

### Dispatch Preflight Validation

Checked before each dispatch cycle:
- Workflow file loadable and parseable
- `tracker.kind` present and supported
- `tracker.api_key` present after `$` resolution
- `tracker.repo` present
- Agent harness command resolvable

---

## 5. Orchestration State Machine

### Issue Orchestration States

These are Symphony's internal claim states, NOT tracker states.

1. **Unclaimed** — not running, no retry scheduled
2. **Claimed** — reserved to prevent duplicate dispatch (running OR retrying)
3. **Running** — worker task active, tracked in `running` map
4. **RetryQueued** — worker not running, retry timer exists
5. **Released** — claim removed (terminal, non-active, or abandoned)

### Run Attempt Lifecycle

1. PreparingWorkspace
2. BuildingPrompt
3. LaunchingAgentProcess
4. InitializingSession
5. StreamingTurn
6. Finishing
7. Succeeded / Failed / TimedOut / Stalled / CanceledByReconciliation

### Transition Triggers

| Trigger | Action |
|---------|--------|
| Poll Tick | Reconcile → validate → fetch candidates → dispatch |
| Worker Exit (normal) | Remove from running, schedule continuation retry (1s, attempt=1) |
| Worker Exit (abnormal) | Remove from running, schedule backoff retry |
| Agent Event | Update live session fields (tokens, timestamps, messages) |
| Retry Timer Fired | Re-fetch candidates, dispatch or release |
| Reconciliation Refresh | Stop runs for terminal/non-active issues |
| Stall Timeout | Kill worker, schedule retry |

### Idempotency

- State mutations serialized through single orchestrator authority
- `claimed` and `running` checks REQUIRED before launching any worker
- Reconciliation runs before dispatch on every tick
- Restart recovery is tracker-driven + filesystem-driven (no durable DB)

---

## 6. Polling, Scheduling, and Reconciliation

### Poll Loop

Tick sequence (every `polling.interval_ms`):

1. Reconcile running issues
2. Run dispatch preflight validation
3. Fetch candidate issues from tracker
4. Sort by dispatch priority
5. Dispatch eligible issues while slots remain
6. Notify observers

### Candidate Selection

An issue is dispatch-eligible only if ALL of:
- Has `id`, `identifier`, `title`, `state`
- State in `active_states` and not in `terminal_states`
- Not in `running` or `claimed`
- Global concurrency slots available
- Per-state concurrency slots available (if configured)
- Blocker rule: `Todo` state issues must have no non-terminal blockers

Sort order: `priority` ascending → `created_at` oldest first → `identifier` lexicographic

### Concurrency Control

- Global: `available_slots = max(max_concurrent_agents - running_count, 0)`
- Per-state: `max_concurrent_agents_by_state[state.lower()]` if present, else global limit

### Retry and Backoff

| Path | Delay | Attempt |
|------|-------|---------|
| Normal exit (continuation) | `1000` ms fixed | `1` |
| Failure (backoff) | `min(10000 × 2^(attempt-1), max_retry_backoff_ms)` | Incremented |

Retry attempt count is **uncapped by design**. Tracker state transitions are the sole circuit
breaker. See `docs/design/agent-execution-architecture.md` §9 for full semantics.

### Reconciliation (every tick)

**Part A — Stall detection:**
- For each running issue: elapsed since last event (or `started_at` if none)
- If elapsed > `stall_timeout_ms` → terminate worker, schedule retry
- If `stall_timeout_ms ≤ 0` → skip stall detection

**Part B — Tracker state refresh:**
- Fetch current states for all running issue IDs
- Terminal state → terminate + clean workspace
- Active state → update in-memory issue snapshot
- Neither → terminate without cleanup
- Fetch failure → keep workers running, retry next tick

### Startup Terminal Cleanup

On startup: query tracker for terminal-state issues, remove corresponding workspace directories.

---

## 7. Workspace Management

### Layout

```
<workspace.root>/
  <sanitized_issue_identifier>/   ← per-issue workspace
  <sanitized_issue_identifier>/
  ...
```

Workspaces are reused across runs. Successful runs do NOT auto-delete.

### Creation Algorithm

1. Sanitize identifier → `workspace_key` (replace non-`[A-Za-z0-9._-]` with `_`)
2. Compute path: `<workspace.root>/<workspace_key>`
3. Ensure directory exists
4. If newly created → run `after_create` hook

### Hooks

| Hook | When | Failure Semantics |
|------|------|-------------------|
| `after_create` | New workspace created | Fatal (aborts creation) |
| `before_run` | Before each attempt | Fatal (aborts attempt) |
| `after_run` | After each attempt | Logged, ignored |
| `before_remove` | Before workspace deletion | Logged, ignored |

Execution: `sh -lc <script>` with workspace as cwd. Timeout: `hooks.timeout_ms` (default 60s).

### Safety Invariants

1. Agent cwd MUST equal workspace path
2. Workspace path MUST stay inside workspace root (prefix check on absolute paths)
3. Workspace key MUST use sanitized characters only

---

## 8. Issue Tracker Integration

### GitHub Issues (tracker.kind: github)

Operations:
- `fetch_candidate_issues()` — active-state issues for configured repo (paginated)
- `fetch_issues_by_states(states)` — for startup terminal cleanup
- `fetch_issue_states_by_ids(ids)` — for active-run reconciliation

Query semantics:
- REST API at configured endpoint
- Auth: `Bearer <token>` in Authorization header
- Repo filter: `tracker.repo` in `owner/repo` format
- Pagination required for candidates (page size: 50)
- Network timeout: 30,000 ms

### Normalization

- `labels` → lowercase strings
- `priority` → integer only (non-integers become null)
- `created_at` / `updated_at` → ISO-8601 timestamps
- `blocked_by` → implementation-defined (GitHub lacks native blocker relations)

### Error Categories

- `unsupported_tracker_kind` — invalid `tracker.kind`
- `missing_tracker_api_key` — no API key after resolution
- `missing_tracker_repo` — no repo configured
- `github_api_request` — transport failures
- `github_api_status` — non-200 HTTP responses
- `github_api_errors` — GraphQL errors
- `github_unknown_payload` — unparseable response
- `github_missing_pagination` — pagination integrity failure

### Tracker Writes Boundary

Symphony does NOT write to the tracker. Ticket mutations (state transitions, comments, PR
metadata) are handled by the coding agent via tools in the workflow prompt. Symphony is a
scheduler/runner and tracker reader only.

---

## 9. Prompt Construction

### Inputs

- `workflow.prompt_template` — Markdown body from WORKFLOW.md
- `issue` — normalized issue object (all fields accessible)
- `attempt` — integer or null (null on first run, ≥1 on retry/continuation)

### Rendering

- Jinja2 strict mode (unknown variables/filters fail)
- Issue object keys converted to strings for template compatibility
- Nested arrays/maps preserved (labels, blockers) for iteration

### Turn Behavior

| Turn | Prompt |
|------|--------|
| 1 | Rendered workflow template (with `issue` + `attempt`) |
| 2+ | Hardcoded: `"Continue working on the issue. Review your previous progress and continue from where you left off."` |

See `docs/design/agent-execution-architecture.md` §8 for rationale.

### Failure Semantics

Prompt rendering failure → fail the run attempt immediately → orchestrator retries.

---

## 10. Observability and HTTP API

### Logging

Required context fields:
- Issue-related: `issue_id`, `issue_identifier`
- Session-related: `session_id`

Format: stable `key=value` phrasing with action outcome and failure reason.

### Runtime Snapshot

The orchestrator exposes a snapshot containing:
- `running` — list of active session rows (with `turn_count`)
- `retrying` — list of retry queue rows
- `copilot_totals` — aggregate input/output/total tokens + seconds_running
- `rate_limits` — latest rate-limit payload from agent events

### Token Accounting

- Prefer absolute thread totals from usage events
- Track deltas relative to last-reported values (avoid double-counting)
- Accumulate aggregates in orchestrator state
- Runtime reported as live aggregate at snapshot time

### HTTP Server (optional extension)

Enabled by CLI `--port` or `server.port` in WORKFLOW.md.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Dashboard (Next.js static export or placeholder) |
| `/api/v1/state` | GET | Full runtime snapshot JSON |
| `/api/v1/<identifier>` | GET | Issue-specific detail (404 if unknown) |
| `/api/v1/refresh` | POST | Trigger immediate poll cycle (202) |

See `docs/design/dashboard-frontend-redesign.md` for dashboard UI architecture.

**Note:** SSE session streaming is designed but not yet implemented. See PR #8 for context.

---

## 11. Failure Model and Recovery

### Failure Classes

1. **Workflow/Config** — missing file, invalid YAML, unsupported tracker, missing credentials
2. **Workspace** — creation failure, hook timeout/failure, invalid path
3. **Agent Session** — startup failure, turn failed/cancelled/timeout, stall, subprocess exit
4. **Tracker** — API transport errors, non-200 status, malformed payloads
5. **Observability** — snapshot timeout, dashboard errors, log sink failure

### Recovery Behavior

| Failure Type | Recovery |
|-------------|----------|
| Dispatch validation failure | Skip dispatch, keep service alive, continue reconciliation |
| Worker failure | Exponential backoff retry |
| Tracker candidate-fetch failure | Skip tick, try next tick |
| Reconciliation state-refresh failure | Keep workers, retry next tick |
| Dashboard/log failure | Do not crash orchestrator |

### Restart Recovery

Scheduler state is intentionally in-memory. After restart:
- No retry timers restored
- No running sessions assumed recoverable
- Recovery by: startup terminal cleanup → fresh polling → re-dispatching eligible work

### Operator Intervention

| Goal | Action |
|------|--------|
| Stop retrying an issue | Move issue to terminal state in tracker |
| Change runtime behavior | Edit WORKFLOW.md (hot-reloaded) |
| Stop all work | Stop the service |
| Reset retry state | Restart the service |

---

## 12. Security and Operational Safety

### Trust Boundary

Each deployment defines its own trust boundary. Implementations MUST document their chosen
approval, sandbox, and operator-confirmation posture.

### Filesystem Safety (mandatory)

- Workspace path MUST remain under configured workspace root
- Agent cwd MUST be the per-issue workspace path
- Workspace directory names MUST use sanitized identifiers

### Secret Handling

- Support `$VAR` indirection in workflow config
- Do not log API tokens or secret env values
- Validate presence of secrets without printing them

### Hook Script Safety

Hooks are arbitrary shell scripts from WORKFLOW.md — fully trusted configuration.
- Run inside workspace directory
- Output truncated in logs
- Timeouts REQUIRED to avoid hanging orchestrator

### Harness Hardening

Running agents against repositories and issue trackers that contain externally-controlled content
carries risk. Implementations SHOULD:

- Tighten SDK approval/sandbox settings rather than running maximally permissive
- Consider external isolation (containers, network restrictions, separate credentials)
- Filter which issues/repos are eligible for dispatch
- Narrow tool access to minimum needed for the workflow
- Document hardening posture explicitly

---

## Appendix: SSH Worker Extension (Future / Not Implemented)

A common extension pattern where Symphony keeps one central orchestrator but executes worker runs
on remote hosts over SSH. This is designed but has no implementation in the current codebase.

Key concepts:
- `worker.ssh_hosts` — candidate SSH destinations
- `worker.max_concurrent_agents_per_host` — per-host cap
- Workspace root interpreted on remote host
- Orchestrator still owns session lifecycle
- Coding agent launched over SSH stdio

See original SPEC.md (git history) for full SSH extension specification if implementing.
