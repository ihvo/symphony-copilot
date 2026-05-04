# Agent Execution Architecture

**Status:** Implemented
**Author:** Symphony Team
**Package:** symphony

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 1.0 | 2026-05-04 | Documented current architecture from codebase; reconciles SPEC.md §10 |

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Relationship to SPEC.md](#2-relationship-to-specmd)
3. [Architecture Overview](#3-architecture-overview)
4. [Agent Harness Protocol](#4-agent-harness-protocol)
5. [Harness Implementations](#5-harness-implementations)
6. [Configuration Contract](#6-configuration-contract)
7. [Session Execution Loop](#7-session-execution-loop)
8. [Turn Continuation Strategy](#8-turn-continuation-strategy)
9. [Retry and Termination Semantics](#9-retry-and-termination-semantics)
10. [Error Mapping](#10-error-mapping)
11. [Decision Records](#11-decision-records)

---

## 1. Problem Statement

SPEC.md §10 ("Agent Runner Protocol") describes a `bash -lc <copilot.command>` shell launch model
and is written exclusively around the Copilot SDK. The actual implementation:

- Uses **SDK client libraries** (`CopilotClient`, `ClaudeSDKClient`) that manage subprocess
  lifecycle internally — no shell invocation occurs.
- Defines an **`AgentHarness` Protocol** (`harness.py`) that abstracts agent execution.
- Routes to implementations via a **factory function** (`_create_harness()` in `runner.py`)
  dispatching on `config.agent_harness` ("copilot" | "claude").
- Has a full **`claude` configuration section** with 7 properties, undocumented in SPEC.md §5.3.

This document describes the architecture as implemented and serves as the ground truth for the
agent execution layer.

## 2. Relationship to Other Architecture Documents

This document covers the agent execution layer. The system-level architecture (orchestration,
workspace, tracker, observability, failure model, security) is documented in
`docs/design/system-architecture.md`.

**Sections of system-architecture.md that cover adjacent domains:**
- §5 Orchestrator contract
- §6 Dispatch, eligibility, concurrency
- §7 Workspace manager and hooks
- §8 Issue tracker integration
- §9 Prompt construction
- §13 Logging and observability
- §14 Failure model (with retry clarification below)
- §15 Security and operational safety

**Key topics covered here (not in system-architecture.md):**
- Agent harness protocol and factory pattern
- SDK client model (replaces former `bash -lc` fiction)
- Multi-turn loop mechanics
- `agent`, `copilot`, `claude` configuration contracts
- Error hierarchy for agent-layer failures

**Existing design documents (details not repeated here):**
- `docs/design/claude-sdk-agent-harness.md` — rationale for the Protocol abstraction, options
  evaluation, migration from hard-coded Copilot runner to multi-harness architecture
- `docs/design/dashboard-frontend-redesign.md` — frontend architecture (unrelated to agent
  execution)

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│ Orchestrator (_run_worker)                                      │
│   ├─ Renders prompt from WORKFLOW.md template                   │
│   ├─ Calls run_agent_session(config, workspace, issue, prompt)  │
│   └─ Receives events via on_event callback → _event_queue       │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│ run_agent_session() [runner.py]                                  │
│   ├─ _create_harness(config, ...) → AgentHarness                │
│   ├─ harness.start()                                            │
│   ├─ Multi-turn loop:                                           │
│   │   ├─ harness.run_turn(prompt, turn_number)                  │
│   │   ├─ Refresh issue state from tracker                       │
│   │   └─ Generate continuation prompt or break                  │
│   └─ harness.stop()                                             │
└──────────────────────────────┬──────────────────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                                 ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│ CopilotHarness           │   │ ClaudeHarness                    │
│ [runner.py]              │   │ [claude_runner.py]                │
│                          │   │                                  │
│ CopilotClient(           │   │ ClaudeSDKClient(                 │
│   SubprocessConfig(      │   │   ClaudeAgentOptions(            │
│     cwd=workspace))      │   │     cwd=workspace,              │
│                          │   │     cli_path=...,               │
│ SDK manages subprocess   │   │     model=...,                  │
│ lifecycle internally     │   │     permission_mode=...))       │
└──────────────────────────┘   └──────────────────────────────────┘
```

**Key principle:** Symphony never shells out via `bash -lc`. Both harnesses instantiate SDK client
objects that manage binary discovery, subprocess spawning, and stdio transport internally.

## 4. Agent Harness Protocol

**File:** `symphony/harness.py`

```python
@runtime_checkable
class AgentHarness(Protocol):
    @property
    def session(self) -> LiveSession: ...

    async def start(self) -> None: ...
    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool: ...
    async def stop(self) -> None: ...
```

### Behavioral Invariants

All harness implementations MUST:

1. Call `on_event(AgentEvent)` for: `session_started`, `turn_completed`, `turn_failed`, `notification`
2. Update `session.last_copilot_timestamp` on every SDK event (stall detection signal)
3. Update `session.copilot_input_tokens` / `copilot_output_tokens` on usage events
4. Update `session.turn_count` in `run_turn()` before executing
5. Set `session.thread_id` and `session.session_id` when available (in `start()` for Copilot; on
   first `ResultMessage` for Claude, since the session ID is unknown until the subprocess responds)
6. Raise only `AgentError` subclasses (never raw SDK exceptions)

### Error Contract

| Method | Expected Exceptions |
|--------|-------------------|
| `start()` | `AgentNotFoundError` or `CopilotNotFoundError`, `AgentStartupError`, `PortExitError`, `InvalidWorkspaceCwdError` |
| `run_turn()` | `TurnTimeoutError`, `TurnFailedError`, `TurnCancelledError`, `TurnInputRequiredError`, `PortExitError` |
| `stop()` | None (must not raise) |

Note: `CopilotNotFoundError` (code: `copilot_not_found`) and `PortExitError` (code: `port_exit`)
are Copilot-harness-specific. The Claude harness uses the generic `AgentNotFoundError` (code:
`agent_not_found`) and `AgentStartupError` (code: `agent_startup_failed`). All are subclasses of
`AgentError`.

### Note on `copilot_*` Field Names

`LiveSession` and `OrchestratorState` use `copilot_*` field names (e.g., `copilot_pid`,
`copilot_input_tokens`, `last_copilot_timestamp`). These are historical names retained for
backward compatibility across the observability surface. They are used by **all** harnesses,
not just the Copilot harness. The names predate multi-harness support and are part of the
stable API contract.

## 5. Harness Implementations

### 5.1 Copilot Harness (`runner.py`)

**SDK:** `github-copilot-sdk` (required dependency)

**Startup:**
```python
subprocess_cfg = SubprocessConfig(
    cwd=workspace_path,
    github_token=config.tracker_api_key or None,
    use_logged_in_user=True,
)
client = CopilotClient(subprocess_cfg)
await client.start()
session = await client.create_session(
    on_permission_request=PermissionHandler.approve_all,
    working_directory=workspace_path,
    on_event=self._handle_sdk_event,
)
```

**Turn execution:** `session.send_and_wait(prompt, timeout=turn_timeout_ms/1000)`

**Event delivery:** SDK callback (`_handle_sdk_event`) emits structured `AgentEvent` objects.

**Config section:** `copilot.*` in WORKFLOW.md front matter (see §6.2).

### 5.2 Claude Harness (`claude_runner.py`)

**SDK:** `claude-agent-sdk` (optional dependency, install via `pip install symphony[claude]`)

**Startup:**
```python
options = ClaudeAgentOptions(
    cwd=workspace_path,
    cli_path=config.claude_command or None,
    model=config.claude_model,
    system_prompt=config.claude_system_prompt or None,
    allowed_tools=config.claude_allowed_tools or [],
    permission_mode=config.claude_permission_mode,
)
client = ClaudeSDKClient(options)
```

**Turn execution:**
- Turn 1: `await client.connect(prompt)` (spawns subprocess)
- Turn N>1: `await client.query(prompt, session_id)`
- Streams messages via `async for message in client.receive_messages()`

**Event delivery:** Message-type dispatch in `_handle_claude_message()` maps to `AgentEvent`.

**Config section:** `claude.*` in WORKFLOW.md front matter (see §6.3).

**See:** `docs/design/claude-sdk-agent-harness.md` for full design rationale and options evaluation.

### 5.3 Factory Function

**File:** `runner.py:248-264`

```python
def _create_harness(config, workspace_path, issue, on_event) -> AgentHarness:
    harness_name = config.agent_harness  # "copilot" | "claude"
    if harness_name == "copilot":
        return CopilotHarness(config, workspace_path, issue, on_event=on_event)
    elif harness_name == "claude":
        from symphony.claude_runner import ClaudeHarness
        return ClaudeHarness(config, workspace_path, issue, on_event=on_event)
    else:
        raise ConfigValidationError(f"Unknown agent harness: {harness_name!r}")
```

The Claude import is deferred to avoid hard dependency on `claude-agent-sdk` at import time.

## 6. Configuration Contract

### 6.1 `agent` Section (§5.3.5 addendum)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `harness` | string | `"copilot"` | Which harness to use: `"copilot"` or `"claude"` |
| `max_concurrent_agents` | integer | `10` | Global concurrency cap |
| `max_turns` | positive integer | `20` | Max turns per worker session |
| `max_retry_backoff_ms` | integer | `300000` | Backoff delay ceiling |
| `max_concurrent_agents_by_state` | map | `{}` | Per-state concurrency limits |

The `harness` field is the dispatch key for `_create_harness()`. The `agent_turn_timeout_ms` and
`agent_stall_timeout_ms` properties on `ServiceConfig` delegate to the active harness's config
section (copilot or claude) automatically.

### 6.2 `copilot` Section

| Field | Type | Default | Wired | Description |
|-------|------|---------|-------|-------------|
| `command` | string | `"copilot-sdk"` | ✗ | SDK binary name (stored for future use / test mocking) |
| `approval_policy` | string | impl-defined | ✗ | Copilot SDK `AskForApproval` value (stored, not yet passed to SDK) |
| `thread_sandbox` | string | impl-defined | ✗ | Copilot SDK `SandboxMode` value (stored, not yet passed to SDK) |
| `turn_sandbox_policy` | string | impl-defined | ✗ | Copilot SDK `SandboxPolicy` value (stored, not yet passed to SDK) |
| `turn_timeout_ms` | integer | `3600000` | ✓ | Max turn duration (used in `send_and_wait` timeout) |
| `read_timeout_ms` | integer | `5000` | ✗ | Request/response timeout (stored, not yet wired) |
| `stall_timeout_ms` | integer | `300000` | ✓ | Event inactivity threshold (used by orchestrator reconciliation) |

**Note:** Fields marked ✗ ("not wired") are defined in `ServiceConfig` and persisted in config,
but `CopilotHarness.start()` currently only passes `cwd`, `github_token`, and
`use_logged_in_user` to `SubprocessConfig`. The SDK manages approval policy through the
`PermissionHandler.approve_all` callback instead. These config fields exist for forward
compatibility and test assertions.

### 6.3 `claude` Section

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | string | `"claude"` | Claude CLI path (for subprocess/test mocking) |
| `turn_timeout_ms` | integer | `3600000` | Max turn duration (1 hour) |
| `stall_timeout_ms` | integer | `300000` | Event inactivity threshold (5 min) |
| `system_prompt` | string or null | `null` | Override system prompt for Claude |
| `allowed_tools` | list of strings | `[]` | Tool allowlist passed to Claude SDK |
| `model` | string or null | `null` | Model override (e.g. `"claude-sonnet-4-20250514"`) |
| `permission_mode` | string | `"auto"` | One of: `default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto` |

## 7. Session Execution Loop

**File:** `runner.py:267-325` (`run_agent_session`)

```
run_agent_session(config, workspace_path, issue, prompt, attempt, on_event, max_turns):
  harness = _create_harness(config, workspace_path, issue, on_event)
  await harness.start()

  turn_number = 1
  current_prompt = prompt  # rendered workflow template

  loop:
    await harness.run_turn(current_prompt, turn_number)

    # Mid-session issue state check
    if fetch_issue_state:
      refreshed = await fetch_issue_state(issue.id)
      if refreshed.state not in active_states:
        break  # issue no longer eligible

    if turn_number >= max_turns:
      break

    turn_number += 1
    current_prompt = CONTINUATION_PROMPT  # hardcoded string

  await harness.stop()
  return harness.session
```

## 8. Turn Continuation Strategy

SPEC.md §16.5 references `build_turn_prompt(workflow_template, issue, attempt, turn_number,
max_turns)`. This function does not exist. The actual behavior:

| Turn | Prompt Source |
|------|--------------|
| 1 | Rendered WORKFLOW.md template (Jinja2 with `issue` + `attempt` variables) |
| 2+ | Hardcoded continuation: `"Continue working on the issue. Review your previous progress and continue from where you left off."` |

The continuation prompt is intentionally simple. The agent retains full context from prior turns
via the SDK's thread/session state.

## 9. Retry and Termination Semantics

### Two Retry Paths

Symphony has two distinct retry mechanisms that operate at different layers:

1. **In-session multi-turn** — within a single `run_agent_session()` call, the harness executes
   up to `max_turns` turns on the same live SDK thread. This is a continuation within one process.

2. **Post-session orchestrator retry** — after `run_agent_session()` returns (success or failure),
   the orchestrator schedules a new worker dispatch. This creates a new harness instance and new
   SDK subprocess.

### Success-Path Continuation Retry

After a **successful** worker exit, the orchestrator schedules a continuation retry:

```
delay_ms = 1000  (_CONTINUATION_DELAY_MS)
attempt = 1
```

This ensures the issue is re-checked against the tracker after each session completes. If the
issue is still in an active state, a fresh session starts. If the tracker shows the issue has
moved to a terminal state, the retry callback discovers this and releases the claim.

### Failure-Path Backoff Retry

After a **failed** worker exit, the orchestrator schedules a backoff retry:

```
delay_ms = min(10000 * 2^(attempt - 1), config.max_retry_backoff_ms)
```

Default ceiling: 300,000 ms (5 minutes).

### Retry Attempt Count (uncapped — by design)

There is **no maximum attempt count**. Retry continues indefinitely until one of:

1. **Tracker state transition** — the issue moves to a terminal state (e.g., closed). The
   orchestrator reconciliation loop detects this and releases the claim.
2. **Issue disappears from candidates** — the issue is no longer returned by the tracker query
   (deleted, wrong repo, state changed). The retry callback cannot find it and releases the claim.
3. **Operator intervention** — close the issue, change its state, or restart the service.

This is intentional: Symphony is a daemon that should keep trying until the problem is fixed or
the issue is explicitly resolved. The delay cap prevents thundering-herd behavior while the
tracker state serves as the sole circuit breaker.

### Operational Intervention Path

| Desired Outcome | Operator Action |
|-----------------|-----------------|
| Stop retrying a specific issue | Close the issue (or move to terminal state) in the tracker |
| Stop all retries | Stop the Symphony service |
| Reset retry state | Restart the service (in-memory state is not persisted) |
| Change retry backoff | Edit `agent.max_retry_backoff_ms` in WORKFLOW.md (hot-reloaded) |

## 10. Error Mapping

Both harnesses translate SDK-specific exceptions into the `AgentError` hierarchy (`errors.py`):

| Normalized Error | `.code` | Copilot Trigger | Claude Trigger |
|-----------------|---------|-----------------|----------------|
| `CopilotNotFoundError` | `copilot_not_found` | `FileNotFoundError` on `client.start()` | — |
| `AgentNotFoundError` | `agent_not_found` | — | `CLINotFoundError` from SDK |
| `AgentStartupError` | `agent_startup_failed` | — | Connection/init failure |
| `PortExitError` | `port_exit` | Other `start()` / session-create failures | — |
| `InvalidWorkspaceCwdError` | `invalid_workspace_cwd` | Non-existent workspace dir | Same |
| `TurnTimeoutError` | `turn_timeout` | `TimeoutError` from `send_and_wait` | `asyncio.wait_for` timeout |
| `TurnFailedError` | `turn_failed` | `session.error` event or generic exception | `ResultMessage.is_error` |
| `TurnCancelledError` | `turn_cancelled` | "cancel" in exception string | `asyncio.CancelledError` |
| `TurnInputRequiredError` | `turn_input_required` | "input" + "required" in string | Error containing "input" |

Note: The error hierarchy is not fully normalized across harnesses. `CopilotNotFoundError` and
`PortExitError` are Copilot-specific. The Claude harness uses the more generic `AgentNotFoundError`
and `AgentStartupError`. All are `AgentError` subclasses and carry a stable `.code` string for
structured reporting.

## 11. Decision Records

### ADR-1: SDK Client Libraries Over Shell Invocation

**Context:** SPEC.md §10.1 prescribed `bash -lc <command>`. Both the Copilot SDK and Claude Agent
SDK provide Python client libraries that manage subprocess lifecycle, stdio transport, and binary
discovery internally.

**Decision:** Use SDK client libraries directly (`CopilotClient`, `ClaudeSDKClient`). Do not shell
out.

**Rationale:**
- SDKs handle binary path resolution, version compatibility, and transport framing
- No shell injection surface
- Type-safe configuration via SDK option objects
- Consistent error reporting from SDK layer

**Consequences:** SPEC.md §10.1, §5.3.6 `bash -lc` claim, and §17.5 test assertion are inaccurate.
This document is the authority for agent execution.

### ADR-2: Protocol-Based Harness Abstraction

**Context:** Adding Claude support required a clean abstraction boundary.

**Decision:** Define `AgentHarness` as a `typing.Protocol` with three async methods (`start`,
`run_turn`, `stop`) and one property (`session`).

**Rationale:**
- Structural typing (Protocol) over inheritance avoids coupling
- Factory pattern allows deferred imports for optional dependencies
- Orchestrator code is harness-agnostic — it only calls `run_agent_session()`

**Consequences:** New harness implementations only need to satisfy the protocol. No orchestrator
changes required.

**See:** `docs/design/claude-sdk-agent-harness.md` for full options analysis.

### ADR-3: Retain `copilot_*` Field Names

**Context:** `LiveSession` and observability surfaces use `copilot_*` naming for fields that are
now harness-agnostic.

**Decision:** Keep existing field names. Do not rename.

**Rationale:**
- Breaking the observability API (snapshot JSON, logs) has downstream impact
- The names are arbitrary identifiers at this point — their semantics are "agent session fields"
- Renaming provides no functional benefit

**Consequences:** New contributors may initially assume these fields are Copilot-specific. This
document and code comments clarify they are harness-agnostic.

### ADR-4: Hardcoded Continuation Prompt

**Context:** SPEC.md §16.5 references `build_turn_prompt()` which would compute turn-specific
prompts.

**Decision:** Use a single hardcoded continuation string for turns 2+.

**Rationale:**
- Agent retains full context via SDK thread state
- Complex per-turn prompt engineering adds no value when the agent already has history
- Simpler code, fewer failure modes

**Consequences:** If per-turn prompt customization becomes needed, a `build_turn_prompt()` function
can be introduced without changing the harness protocol (it's above the harness layer).

### ADR-5: Infinite Retry by Design

**Context:** Retry delay is capped at `max_retry_backoff_ms`, but there is no attempt count limit.

**Decision:** Retry indefinitely. Tracker state transitions are the sole termination signal.

**Rationale:**
- Symphony is a daemon — persistent retry is the correct behavior for a long-running service
- Transient failures (network, rate limits) eventually self-heal
- Permanent failures manifest as repeated tracker state checks that eventually resolve (operator
  closes issue, fixes environment, etc.)
- Adding an arbitrary cap creates a "silent give up" failure mode that's harder to debug

**Consequences:** Operators MUST use tracker state (close the issue) to stop retries. This is
documented in §14.4 of SPEC.md and in this document's §9.

---

## Appendix: Future Considerations

### SSE Session Streaming

The HTTP server extension (SPEC.md §13.7, `symphony/server.py`) does not currently implement
server-sent events for real-time session streaming. This is a designed but unspecified extension.
See PR #8 discussion for context.

### SSH Worker Extension

SPEC.md Appendix A describes an SSH remote worker model. This is a **future/not-implemented**
extension — no code exists for it in the current codebase.
