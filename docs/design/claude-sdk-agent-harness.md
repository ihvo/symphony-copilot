# Claude SDK Agent Harness Support

**Status:** Proposed
**Author:** Symphony Team
**Package:** symphony

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 1.0 | 2026-05-04 | Initial design |
| 1.1 | 2026-05-04 | Incorporated design review feedback: fixed timeout abstraction, error mapping, config validation, and command parity |

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

Symphony currently hard-codes the Copilot SDK as its only agent harness. The runner module (`symphony/runner.py`) directly imports and instantiates `CopilotClient` / `CopilotSession` from the `github-copilot-sdk` package. This creates three problems:

1. **Vendor lock-in**: Teams wanting to use Claude Code (Anthropic's agentic SDK) cannot use Symphony without forking.
2. **No extensibility path**: Adding any alternative agent runtime requires modifying the runner, orchestrator, and config modules—all tightly coupled to Copilot SDK concepts.
3. **Naming leakage**: Config fields (`copilot_command`, `copilot_turn_timeout_ms`, `copilot_stall_timeout_ms`), model fields (`copilot_pid`, `copilot_input_tokens`), and LiveSession attributes all use "copilot" naming, making abstraction difficult without breaking the public contract.

### Evidence

- `runner.py:16-17` — hard imports: `from copilot import CopilotClient, SubprocessConfig`
- `config.py` — six `copilot_*` properties with Copilot-specific semantics
- `models.py` — `LiveSession` fields named `copilot_*` (8 fields)
- `orchestrator.py` — references `copilot_totals`, `copilot_rate_limits` in state
- `pyproject.toml:12` — `github-copilot-sdk>=0.3.0` is a hard dependency

### Impact of Not Fixing

- Symphony cannot serve teams using Claude Code SDK for their agent runtime.
- The Claude Agent SDK (`claude-agent-sdk` on PyPI) offers comparable capabilities (subprocess-based, async streaming, tool use, MCP support) but is structurally incompatible with the current runner.
- As the agentic ecosystem diversifies, Symphony becomes progressively harder to adopt.

## 2. Current Architecture

### Runner Module (`symphony/runner.py`)

```
┌─────────────────────────────────────────────────────┐
│ run_agent_session() — public entry point             │
│  ├─ Creates CopilotAgentSession                     │
│  ├─ Calls session.start()                           │
│  ├─ Multi-turn loop: session.run_turn(prompt, N)    │
│  └─ session.stop() in finally                       │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ CopilotAgentSession                                  │
│  Fields: _config, _workspace, _issue, _on_event     │
│  Methods:                                            │
│    start()     → CopilotClient + create_session     │
│    run_turn()  → sdk_session.send_and_wait()        │
│    stop()      → disconnect + client.stop()         │
│    _emit()     → AgentEvent to orchestrator         │
│    _handle_sdk_event() → SDK events → _emit()       │
└─────────────────────────────────────────────────────┘
```

### Orchestrator Integration

```
Orchestrator._run_worker()
  → run_agent_session(config, workspace, issue, prompt, ...)
      ↓ on_event callback
  → _event_queue.put_nowait(AgentEvent)
      ↓
  → _handle_agent_event() mutates RunningEntry
```

### Config Coupling

The `copilot` section in WORKFLOW.md drives:
- `copilot_command` → binary to launch
- `copilot_turn_timeout_ms` → per-turn timeout
- `copilot_stall_timeout_ms` → stall detection
- `copilot_approval_policy` → permission handling
- `copilot_read_timeout_ms` → JSONRPC read timeout

### Dependency Graph

```
orchestrator.py ──imports──→ runner.run_agent_session()
runner.py ──imports──→ copilot (CopilotClient, SubprocessConfig)
runner.py ──imports──→ config.ServiceConfig (copilot_* properties)
runner.py ──imports──→ models (AgentEvent, Issue, LiveSession)
config.py ──reads──→ WORKFLOW.md copilot section
models.py ──defines──→ LiveSession (copilot_* fields)
```

## 3. Requirements

### Must-Have

- **R1**: Support Claude Agent SDK (`claude-agent-sdk`) as an alternative agent harness alongside the existing Copilot SDK.
- **R2**: Agent harness is selectable via WORKFLOW.md configuration (a single `agent.harness` field or equivalent).
- **R3**: Both harnesses share the same `AgentEvent` protocol to the orchestrator — no orchestrator changes required for event handling.
- **R4**: The `run_agent_session()` public interface remains unchanged — the orchestrator dispatches identically regardless of harness.
- **R5**: Each harness manages its own subprocess lifecycle, error mapping, and event translation.
- **R6**: Existing Copilot SDK behavior is fully preserved (zero regressions).
- **R7**: Claude SDK harness supports multi-turn sessions with the same turn loop semantics.
- **R8**: Configuration validation rejects unknown harness values at startup.

### Nice-to-Have

- **N1**: Abstract base class or Protocol that new harnesses can implement for future extensibility (e.g., OpenAI Codex, custom agents).
- **N2**: Harness-specific config sections coexist — `copilot.*` and `claude.*` can both be present, and only the active harness's section is read.
- **N3**: LiveSession field naming becomes harness-agnostic (rename `copilot_*` → `agent_*`) with backward-compatible aliases.

### Constraints

- **C1**: The existing WORKFLOW.md format must remain backward-compatible. A workflow file without an explicit `agent.harness` field defaults to `copilot`.
- **C2**: `pyproject.toml` must not make `claude-agent-sdk` a hard dependency — it should be an optional extra.
- **C3**: The orchestrator module must not import any harness-specific code.
- **C4**: Test infrastructure must support both harnesses without duplicating the entire test suite.

## 4. Options Evaluation

### Option A: Strategy Pattern with Protocol

**Approach**: Define a `AgentHarness` Protocol (abstract interface). Implement `CopilotHarness` and `ClaudeHarness` as concrete classes. A factory function selects the harness at runtime based on config.

```python
class AgentHarness(Protocol):
    async def start(self) -> None: ...
    async def run_turn(self, prompt: str, turn_number: int) -> bool: ...
    async def stop(self) -> None: ...
    @property
    def session(self) -> LiveSession: ...
```

**Pros**: Clean separation, easy to add more harnesses, testable via mocks.
**Cons**: Requires refactoring `CopilotAgentSession` into the Protocol shape (minor).

### Option B: Subclass Hierarchy

**Approach**: Create `BaseAgentSession` ABC, have `CopilotAgentSession` and `ClaudeAgentSession` inherit from it.

**Pros**: Familiar OOP pattern, shared logic in base class.
**Cons**: Inheritance coupling, harder to test in isolation, shared mutable state in base.

### Option C: Plugin/Entry-Point System

**Approach**: Each harness is a separate package with a `setuptools` entry point. Symphony discovers harnesses at runtime via `importlib.metadata`.

**Pros**: Maximum decoupling, third parties can add harnesses.
**Cons**: Over-engineered for 2 harnesses, complex debugging, harder to test.

### Comparison Matrix

| Criterion | A: Protocol | B: Subclass | C: Plugin |
|-----------|:-----------:|:-----------:|:---------:|
| Simplicity | ★★★★ | ★★★ | ★★ |
| Testability | ★★★★★ | ★★★ | ★★★ |
| Extensibility | ★★★★ | ★★★ | ★★★★★ |
| No unnecessary abstraction | ★★★★ | ★★★ | ★★ |
| Backward compat effort | ★★★★ | ★★★ | ★★★★ |

### Recommendation: **Option A — Strategy Pattern with Protocol**

It's the simplest approach that satisfies all Must-Have requirements without over-engineering. The Protocol provides structural typing (duck-typing with IDE support) and keeps the door open for future harnesses without requiring them to install as separate packages.

## 5. Recommended Approach

### 5.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      Orchestrator                             │
│  _run_worker() calls run_agent_session()                    │
│  (unchanged — still receives AgentEvent via on_event)       │
└─────────────────────────────┬───────────────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │ run_agent_session()│  ← unchanged public API
                    │  (runner.py)       │
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │  _create_harness() │  ← factory, reads config.agent_harness
                    └────┬─────────┬────┘
                         │         │
            ┌────────────▼──┐  ┌──▼────────────┐
            │ CopilotHarness│  │ ClaudeHarness  │
            │ (runner.py)   │  │ (claude_runner │
            │               │  │  .py)          │
            └───────────────┘  └───────────────┘
                    │                   │
            ┌───────▼───────┐  ┌───────▼───────┐
            │ github-copilot│  │ claude-agent   │
            │ -sdk          │  │ -sdk           │
            └───────────────┘  └───────────────┘
```

### 5.2 New Module: `symphony/harness.py` — Protocol Definition

```python
"""Agent harness protocol — the contract all harnesses implement."""

from __future__ import annotations
from typing import Protocol, runtime_checkable
from symphony.models import LiveSession


@runtime_checkable
class AgentHarness(Protocol):
    """Protocol for agent session harnesses.

    Implementors MUST also satisfy these behavioral invariants:
    - Call on_event(AgentEvent) for: session_started, turn_completed, turn_failed, notification
    - Update session.last_copilot_timestamp on every SDK event (for stall detection)
    - Update session.copilot_input_tokens / copilot_output_tokens on usage events
    - Update session.turn_count in run_turn() before executing
    - Set session.thread_id and session.session_id in start()
    - Raise only AgentError subclasses (never raw SDK exceptions)
    """

    @property
    def session(self) -> LiveSession:
        """Return the current session state."""
        ...

    async def start(self) -> None:
        """Initialize and launch the agent subprocess.

        Raises:
            AgentNotFoundError: Binary/SDK not available.
            AgentStartupError: Subprocess failed to start.
            InvalidWorkspaceCwdError: Workspace path invalid.
        """
        ...

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Execute one agent turn. Returns True on success.

        Raises:
            TurnTimeoutError: Turn exceeded timeout.
            TurnFailedError: Turn failed for any reason.
            TurnCancelledError: Turn was cancelled.
            TurnInputRequiredError: Agent requires user input.
        """
        ...

    async def stop(self) -> None:
        """Shut down the agent session and subprocess. Must not raise."""
        ...
```

### 5.3 Refactored Runner (`symphony/runner.py`)

The existing `CopilotAgentSession` becomes the `CopilotHarness` (rename for clarity, keeping identical logic). The `run_agent_session()` function gains a factory step:

```python
def _create_harness(
    config: ServiceConfig,
    workspace_path: str,
    issue: Issue,
    on_event: Callable[[AgentEvent], None] | None = None,
) -> AgentHarness:
    """Factory: select harness based on config.agent_harness."""
    harness_name = config.agent_harness  # "copilot" or "claude"

    if harness_name == "copilot":
        return CopilotHarness(config, workspace_path, issue, on_event=on_event)
    elif harness_name == "claude":
        from symphony.claude_runner import ClaudeHarness
        return ClaudeHarness(config, workspace_path, issue, on_event=on_event)
    else:
        from symphony.errors import ConfigValidationError
        raise ConfigValidationError(f"Unknown agent harness: {harness_name!r}")
```

The `run_agent_session()` function changes only its session creation line:

```python
async def run_agent_session(...) -> LiveSession:
    session = _create_harness(config, workspace_path, issue, on_event=on_event)
    # ... rest unchanged ...
```

### 5.4 New Module: `symphony/claude_runner.py`

```python
"""Claude Agent SDK harness implementation."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

from symphony.config import ServiceConfig
from symphony.errors import (
    AgentNotFoundError,
    InvalidWorkspaceCwdError,
    AgentStartupError,
    TurnCancelledError,
    TurnFailedError,
    TurnInputRequiredError,
    TurnTimeoutError,
)
from symphony.models import AgentEvent, Issue, LiveSession

logger = logging.getLogger("symphony.claude_runner")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ClaudeHarness:
    """Manages a live Claude Agent SDK session with multi-turn support.

    Uses the claude-agent-sdk Python package which spawns a Claude CLI
    subprocess and communicates via structured JSON streams.
    """

    def __init__(
        self,
        config: ServiceConfig,
        workspace_path: str,
        issue: Issue,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._config = config
        self._workspace = os.path.abspath(workspace_path)
        self._issue = issue
        self._on_event = on_event
        self._session = LiveSession()
        self._started = False
        self._client: Any = None  # ClaudeSDKClient

    @property
    def session(self) -> LiveSession:
        return self._session

    def _emit(self, event_name: str, **kwargs: Any) -> None:
        """Emit an AgentEvent — identical pattern to CopilotHarness."""
        evt = AgentEvent(
            event=event_name,
            issue_id=self._issue.id,
            timestamp=_now_utc(),
            copilot_pid=self._session.copilot_pid,
            session_id=self._session.session_id or None,
            **kwargs,
        )
        self._session.last_copilot_event = event_name
        self._session.last_copilot_timestamp = evt.timestamp
        if kwargs.get("message"):
            self._session.last_copilot_message = kwargs["message"]
        if self._on_event:
            self._on_event(evt)

    async def start(self) -> None:
        """Launch Claude CLI subprocess and establish session."""
        try:
            from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        except ImportError as exc:
            raise AgentNotFoundError(
                "claude-agent-sdk",
                "Package not installed. Install with: pip install symphony[claude]",
            ) from exc

        if not os.path.isdir(self._workspace):
            raise InvalidWorkspaceCwdError(self._workspace, "does not exist")

        logger.info(
            "agent_launch harness=claude cwd=%s issue=%s",
            self._workspace, self._issue.identifier,
        )

        options = ClaudeAgentOptions(
            working_directory=self._workspace,
            command=self._config.claude_command,
            model=self._config.claude_model,
            system_prompt=self._config.claude_system_prompt or "",
            allowed_tools=self._config.claude_allowed_tools,
            permission_mode=self._config.claude_permission_mode,
        )

        try:
            self._client = ClaudeSDKClient(options)
            await self._client.connect()
        except FileNotFoundError as exc:
            raise AgentNotFoundError("claude", str(exc)) from exc
        except Exception as exc:
            raise AgentStartupError("claude", str(exc)) from exc

        self._session.thread_id = getattr(self._client, "session_id", "") or ""
        self._session.session_id = self._session.thread_id
        self._session.copilot_pid = str(getattr(self._client, "pid", "") or "")
        self._started = True
        self._emit("session_started", message="Claude session started")

    async def run_turn(self, prompt: str, turn_number: int = 1) -> bool:
        """Execute one turn via Claude SDK streaming query."""
        if not self._started or not self._client:
            raise AgentStartupError("claude", "Session not started")

        self._session.turn_count += 1
        self._session.turn_id = f"turn-{turn_number}"
        self._session.session_id = f"{self._session.thread_id}-{self._session.turn_id}"

        turn_timeout = self._config.claude_turn_timeout_ms / 1000.0

        try:
            async for message in self._client.send(prompt, timeout=turn_timeout):
                self._handle_claude_message(message)
        except asyncio.TimeoutError:
            raise TurnTimeoutError()
        except asyncio.CancelledError:
            raise TurnCancelledError()
        except Exception as exc:
            err_str = str(exc).lower()
            if "input" in err_str and "required" in err_str:
                raise TurnInputRequiredError()
            raise TurnFailedError(str(exc))

        self._emit("turn_completed", message=f"Turn {turn_number} completed")
        return True

    def _handle_claude_message(self, message: Any) -> None:
        """Translate Claude SDK streaming messages into Symphony events."""
        msg_type = getattr(message, "type", "")
        self._session.last_copilot_timestamp = _now_utc()

        if msg_type == "usage":
            inp = getattr(message, "input_tokens", 0) or 0
            out = getattr(message, "output_tokens", 0) or 0
            total = inp + out
            self._session.copilot_input_tokens = int(inp)
            self._session.copilot_output_tokens = int(out)
            self._session.copilot_total_tokens = int(total)
            self._emit(
                "notification",
                usage={"input_tokens": int(inp), "output_tokens": int(out), "total_tokens": total},
            )
        elif msg_type == "assistant_message":
            content = str(getattr(message, "content", ""))[:200]
            self._session.last_copilot_message = content
            self._emit("notification", message=content)
        elif msg_type == "error":
            msg = str(getattr(message, "message", message))
            self._emit("turn_failed", error=msg, message=msg)
        elif msg_type == "tool_use":
            self._emit("notification", message="Tool use in progress")

    async def stop(self) -> None:
        """Disconnect Claude SDK client."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._started = False
```

### 5.5 Configuration Changes

#### WORKFLOW.md — New `agent.harness` field

```yaml
agent:
  harness: claude  # or "copilot" (default)
  max_concurrent_agents: 3
  max_turns: 20

claude:
  command: "claude"       # CLI binary (like copilot.command — for test mocking)
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
  system_prompt: ""       # Optional override
  allowed_tools: []       # Claude SDK tool allowlist
  model: "claude-sonnet-4-20250514"  # Model selection
  permission_mode: "auto" # "auto" (approve all) | "manual" | "deny"
```

#### `config.py` — New properties

**Generic dispatching properties** (resolve based on active harness):

```python
@property
def agent_harness(self) -> str:
    """Which agent harness to use: 'copilot' or 'claude'."""
    raw = self._raw.get("agent", {})
    val = raw.get("harness", "copilot") if isinstance(raw, dict) else "copilot"
    return str(val).lower().strip()

@property
def agent_turn_timeout_ms(self) -> int:
    """Turn timeout for the active harness (dispatches to copilot/claude section)."""
    if self.agent_harness == "claude":
        return self.claude_turn_timeout_ms
    return self.copilot_turn_timeout_ms

@property
def agent_stall_timeout_ms(self) -> int:
    """Stall timeout for the active harness (dispatches to copilot/claude section)."""
    if self.agent_harness == "claude":
        return self.claude_stall_timeout_ms
    return self.copilot_stall_timeout_ms
```

**Claude-specific properties** (follow existing `_get()` validation pattern):

```python
@property
def claude_command(self) -> str:
    """Claude CLI command (for subprocess launch / test mocking)."""
    sec = self._raw.get("claude", {})
    if not isinstance(sec, dict):
        return "claude"
    return str(sec.get("command", "claude")).strip() or "claude"

@property
def claude_turn_timeout_ms(self) -> int:
    sec = self._raw.get("claude", {})
    if not isinstance(sec, dict):
        return 3600000
    try:
        val = int(sec.get("turn_timeout_ms", 3600000))
        return val if val > 0 else 3600000
    except (TypeError, ValueError):
        return 3600000

@property
def claude_stall_timeout_ms(self) -> int:
    sec = self._raw.get("claude", {})
    if not isinstance(sec, dict):
        return 300000
    try:
        val = int(sec.get("stall_timeout_ms", 300000))
        return val  # 0 or negative = disabled (same semantics as copilot)
    except (TypeError, ValueError):
        return 300000

@property
def claude_system_prompt(self) -> str | None:
    sec = self._raw.get("claude", {})
    if not isinstance(sec, dict):
        return None
    val = sec.get("system_prompt")
    return str(val) if val is not None else None

@property
def claude_allowed_tools(self) -> list[str]:
    sec = self._raw.get("claude", {})
    if not isinstance(sec, dict):
        return []
    tools = sec.get("allowed_tools")
    if isinstance(tools, list):
        return [str(t) for t in tools]
    return []

@property
def claude_model(self) -> str | None:
    sec = self._raw.get("claude", {})
    if not isinstance(sec, dict):
        return None
    val = sec.get("model")
    return str(val) if val else None

@property
def claude_permission_mode(self) -> str:
    sec = self._raw.get("claude", {})
    if not isinstance(sec, dict):
        return "auto"
    val = str(sec.get("permission_mode", "auto")).lower().strip()
    return val if val in ("auto", "manual", "deny") else "auto"
```

#### Validation

`validate_dispatch()` gains:
```python
harness = self.agent_harness
if harness not in ("copilot", "claude"):
    errors.append(f"agent.harness must be 'copilot' or 'claude', got {harness!r}")

if harness == "claude":
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        errors.append("agent.harness is 'claude' but claude-agent-sdk is not installed")

    # Validate claude-specific fields
    sec = self._raw.get("claude", {})
    if isinstance(sec, dict):
        tt = sec.get("turn_timeout_ms")
        if tt is not None:
            try:
                if int(tt) <= 0:
                    errors.append("claude.turn_timeout_ms must be positive")
            except (TypeError, ValueError):
                errors.append("claude.turn_timeout_ms must be an integer")
```

#### Orchestrator stall detection update

The orchestrator's `_reconcile_stalls()` currently reads `self._config.copilot_stall_timeout_ms`. This changes to `self._config.agent_stall_timeout_ms` — a one-line change that dispatches to the correct harness-specific value.

### 5.6 Error Hierarchy Changes (`symphony/errors.py`)

New harness-agnostic errors that replace Copilot-specific errors for the factory layer:

```python
class AgentNotFoundError(AgentError):
    """The agent binary/SDK was not found."""
    code = "agent_not_found"

    def __init__(self, harness: str, detail: str = "") -> None:
        self.harness = harness
        super().__init__(f"{harness} agent not found: {detail}")


class AgentStartupError(AgentError):
    """The agent subprocess failed to start or connect."""
    code = "agent_startup_failed"

    def __init__(self, harness: str, detail: str = "") -> None:
        self.harness = harness
        super().__init__(f"{harness} agent startup failed: {detail}")
```

The existing `CopilotNotFoundError` and `PortExitError` remain for backward compatibility but `CopilotHarness` can migrate to the new types in a follow-up.

**Retry semantics**: The orchestrator treats `AgentNotFoundError` as non-retryable (permanent config issue). `AgentStartupError` is retryable (transient subprocess failure).

### 5.7 Dependency Changes (`pyproject.toml`)

```toml
[project]
dependencies = [
    "httpx>=0.27",
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "pyyaml>=6.0,<7",
    "jinja2>=3.1,<4",
    "github-copilot-sdk>=0.3.0",
]

[project.optional-dependencies]
claude = ["claude-agent-sdk>=1.0"]
all = ["claude-agent-sdk>=1.0"]
```

Install with Claude support: `pip install symphony[claude]` or `uv sync --extra claude`.

### 5.8 LiveSession Field Naming (N3 — Nice-to-Have)

For this initial implementation, we keep the existing `copilot_*` field names in `LiveSession` to avoid breaking changes. Both harnesses write to the same fields:

- `copilot_pid` → stores the subprocess PID regardless of harness
- `copilot_input_tokens` → stores input tokens from either SDK
- `copilot_total_tokens` → stores total tokens from either SDK

A future PR can rename these to `agent_pid`, `agent_input_tokens`, etc. with deprecation aliases.

### 5.9 Stall Detection

The orchestrator's stall detection (`_reconcile_stalls()`) uses `last_copilot_timestamp` on `LiveSession`. Both harnesses update this field during event processing, so stall detection works identically for both harnesses — no orchestrator changes needed.

## 6. Migration Plan

### Phase 1: Protocol + Factory (no new harness yet)

1. Create `symphony/harness.py` with `AgentHarness` Protocol.
2. Rename `CopilotAgentSession` → `CopilotHarness` (keep import alias for compatibility).
3. Add `_create_harness()` factory to `runner.py`.
4. Add `agent_harness` property to `ServiceConfig` (defaults to `"copilot"`).
5. Verify all existing tests pass unchanged.

**Verification gate**: `uv run pytest` — all 206 tests green, no behavior change.

### Phase 2: Claude Harness Implementation

1. Create `symphony/claude_runner.py` with `ClaudeHarness`.
2. Add `claude_*` config properties to `config.py`.
3. Add `claude-agent-sdk` as optional dependency in `pyproject.toml`.
4. Implement full `start()` / `run_turn()` / `stop()` lifecycle.
5. Map Claude SDK events to `AgentEvent` emissions.

**Verification gate**: Unit tests for `ClaudeHarness` with mocked SDK pass.

### Phase 3: Integration Testing

1. Create `tests/integration/mock_claude_agent.py` — standalone script simulating Claude CLI subprocess.
2. Add integration tests for Claude harness: startup, multi-turn, timeout, failure modes.
3. Add config validation tests for `agent.harness` field.

**Verification gate**: Full test suite passes with both harness configurations.

### Phase 4: Documentation + Validation

1. Update `AGENTS.md` with Claude harness instructions.
2. Add example WORKFLOW.md for Claude configuration.
3. Update SPEC.md section 5.3.6 to document harness selection.
4. Add validation for harness-specific config sections.

**Estimated effort**: ~5 engineering days total across all phases.

## 7. Test Strategy

### Unit Tests

| Test | File | What it verifies |
|------|------|------------------|
| `test_harness_factory_copilot` | `test_runner.py` | Factory returns CopilotHarness for `harness: copilot` |
| `test_harness_factory_claude` | `test_runner.py` | Factory returns ClaudeHarness for `harness: claude` |
| `test_harness_factory_invalid` | `test_runner.py` | Factory raises ConfigValidationError for unknown harness |
| `test_claude_session_lifecycle` | `test_claude_runner.py` | start → run_turn → stop with mocked SDK |
| `test_claude_turn_timeout` | `test_claude_runner.py` | asyncio.TimeoutError → TurnTimeoutError |
| `test_claude_turn_failure` | `test_claude_runner.py` | SDK exception → TurnFailedError |
| `test_claude_event_emission` | `test_claude_runner.py` | Claude messages → AgentEvent via on_event |
| `test_config_agent_harness_default` | `test_config.py` | Missing field defaults to "copilot" |
| `test_config_agent_harness_claude` | `test_config.py` | Explicit "claude" parsed correctly |
| `test_config_validation_unknown_harness` | `test_config.py` | Validation error for bad harness |
| `test_config_claude_properties` | `test_config.py` | claude.* properties parsed correctly |

### Integration Tests

| Test | File | What it verifies |
|------|------|------------------|
| `test_claude_harness_multi_turn` | `test_s17_5_agent.py` | Full multi-turn session with mock Claude subprocess |
| `test_claude_harness_stall_timeout` | `test_s17_5_agent.py` | Stall detection works with Claude harness |
| `test_orchestrator_dispatches_claude` | `test_s17_4_orchestrator.py` | Orchestrator uses correct harness per config |

### Mock Claude Agent

A `tests/integration/mock_claude_agent.py` script that simulates the Claude CLI subprocess behavior — accepts prompts via stdin, streams JSON responses via stdout. Mirrors `mock_agent.py` structure with behaviors: `success`, `fail`, `hang`, `input_required`.

## 8. Risk Assessment

### Risks of Implementing

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Claude SDK API changes before stable | Medium | Medium | Pin version, use adapter layer in ClaudeHarness |
| Token counting differs between SDKs | Low | Low | Normalize in each harness's event handler |
| Claude CLI not available in all environments | Medium | Low | Clear error message + validation at startup |
| Increased maintenance surface (2 harnesses) | Medium | Medium | Protocol ensures consistent interface; shared test patterns |

### Risks of NOT Implementing

| Risk | Likelihood | Impact |
|------|-----------|--------|
| Teams blocked from adopting Symphony with Claude | High | High |
| Competitive disadvantage vs. multi-agent orchestrators | Medium | Medium |
| Technical debt compounds as more harness-specific code accumulates | High | Medium |

## 9. Decision Records

### Review Incorporation Summary

Design review (single-model, GPT-5.4) identified 4 blocking issues, all resolved:
- **Timeout abstraction**: Added generic `agent_turn_timeout_ms` / `agent_stall_timeout_ms` dispatching properties + one-line orchestrator change
- **Command/runtime parity**: Added `claude.command` field for test mocking + `permission_mode` for approval semantics
- **Error mapping**: Introduced `AgentNotFoundError` / `AgentStartupError` (harness-agnostic), classified retry semantics
- **Config validation pattern**: Rewrote all `claude_*` getters with proper type guards, `isinstance` checks, and fail-fast validation

Non-blocking feedback acknowledged:
- Protocol behavioral invariants now documented in docstring
- Integration test plan updated to note SDK adapter seam testing

### ADR-1: Strategy Pattern over Inheritance

**Context**: Need to support multiple agent harnesses with different SDK dependencies.
**Decision**: Use a Python `Protocol` (structural typing) rather than ABC inheritance.
**Rationale**: Protocols enable duck typing without import-time coupling. Each harness can be in its own module with lazy imports, avoiding import errors when an SDK isn't installed.
**Consequences**: Slightly less discoverable than ABC (no `@abstractmethod` enforcement), but `@runtime_checkable` provides isinstance checks where needed.

### ADR-2: Optional Dependency for Claude SDK

**Context**: Not all users need Claude support; requiring it adds install complexity.
**Decision**: `claude-agent-sdk` is an optional extra (`pip install symphony[claude]`).
**Rationale**: Follows Python ecosystem conventions. Users get clear ImportError if they configure `harness: claude` without the extra installed.
**Consequences**: Validation must check for SDK availability at config time, not import time.

### ADR-3: Shared LiveSession Model

**Context**: Could create separate session models per harness or reuse one.
**Decision**: Reuse `LiveSession` with existing `copilot_*` field names for now.
**Rationale**: Avoids breaking the HTTP server API, dashboard, and orchestrator state. Both SDKs produce the same semantic data (PIDs, tokens, timestamps). Rename to `agent_*` in a future non-breaking PR.
**Consequences**: Field names are slightly misleading when using Claude harness, but functionally correct.

### ADR-4: Backward-Compatible Default

**Context**: Existing WORKFLOW.md files don't specify a harness.
**Decision**: Default `agent.harness` to `"copilot"` when not specified.
**Rationale**: Zero-change upgrade path for existing users. SPEC compliance maintained.
**Consequences**: New users must explicitly opt into Claude by adding `harness: claude` to their agent config.

### ADR-5: Factory in runner.py, Not Orchestrator

**Context**: Where should harness selection live?
**Decision**: In `runner.py`'s `_create_harness()` factory, called by `run_agent_session()`.
**Rationale**: Keeps the orchestrator completely harness-agnostic (satisfies C3). The orchestrator only knows about `run_agent_session()` and `AgentEvent`.
**Consequences**: Runner module grows slightly, but maintains single responsibility for agent session lifecycle.
