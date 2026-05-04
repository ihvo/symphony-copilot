"""Typed configuration layer for Symphony.

Resolves workflow front-matter into typed runtime values with defaults,
``$VAR`` expansion, ``~`` expansion, path normalization, and validation.
"""

from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from symphony.errors import ConfigValidationError
from symphony.models import WorkflowDefinition

_ENV_VAR_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


def _resolve_env(value: str) -> str:
    """If *value* is ``$VAR_NAME``, resolve from the environment."""
    m = _ENV_VAR_RE.match(value)
    if m:
        return os.environ.get(m.group(1), "")
    return value


def _expand_path(value: str, base_dir: str) -> str:
    """Expand ``~`` and resolve relative paths against *base_dir*."""
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return os.path.abspath(value)


def _get(cfg: dict, *keys: str, default: Any = None) -> Any:
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


class ServiceConfig:
    """Typed, validated runtime configuration derived from a workflow."""

    def __init__(self, workflow: WorkflowDefinition, workflow_dir: str) -> None:
        self._raw = workflow.config
        self._workflow_dir = workflow_dir
        self.prompt_template = workflow.prompt_template

    # --- tracker ---

    @property
    def tracker_kind(self) -> str:
        return str(_get(self._raw, "tracker", "kind", default="") or "")

    @property
    def tracker_endpoint(self) -> str:
        default = "https://api.github.com" if self.tracker_kind == "github" else ""
        return str(_get(self._raw, "tracker", "endpoint", default=default) or default)

    @property
    def tracker_api_key(self) -> str:
        raw = str(_get(self._raw, "tracker", "api_key", default="") or "")
        if raw:
            resolved = _resolve_env(raw)
            if resolved:
                return resolved
        # Canonical fallback for github
        if self.tracker_kind == "github":
            return os.environ.get("GITHUB_TOKEN", "")
        return ""

    @property
    def tracker_repo(self) -> str:
        return str(_get(self._raw, "tracker", "repo", default="") or "")

    @property
    def active_states(self) -> list[str]:
        val = _get(self._raw, "tracker", "active_states")
        if isinstance(val, list):
            return [str(s).lower() for s in val]
        return ["open"]

    @property
    def terminal_states(self) -> list[str]:
        val = _get(self._raw, "tracker", "terminal_states")
        if isinstance(val, list):
            return [str(s).lower() for s in val]
        return ["closed"]

    # --- polling ---

    @property
    def poll_interval_ms(self) -> int:
        val = _get(self._raw, "polling", "interval_ms", default=30000)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 30000

    # --- workspace ---

    @property
    def workspace_root(self) -> str:
        raw = str(
            _get(self._raw, "workspace", "root", default="")
            or ""
        )
        if not raw:
            return os.path.join(tempfile.gettempdir(), "symphony_workspaces")
        resolved = _resolve_env(raw)
        if not resolved:
            return os.path.join(tempfile.gettempdir(), "symphony_workspaces")
        return _expand_path(resolved, self._workflow_dir)

    # --- hooks ---

    @property
    def hook_after_create(self) -> str | None:
        return _get(self._raw, "hooks", "after_create")

    @property
    def hook_before_run(self) -> str | None:
        return _get(self._raw, "hooks", "before_run")

    @property
    def hook_after_run(self) -> str | None:
        return _get(self._raw, "hooks", "after_run")

    @property
    def hook_before_remove(self) -> str | None:
        return _get(self._raw, "hooks", "before_remove")

    @property
    def hook_timeout_ms(self) -> int:
        val = _get(self._raw, "hooks", "timeout_ms", default=60000)
        try:
            v = int(val)
            if v <= 0:
                raise ConfigValidationError("hooks.timeout_ms must be positive")
            return v
        except (TypeError, ValueError):
            raise ConfigValidationError(f"hooks.timeout_ms invalid: {val!r}")

    # --- agent ---

    @property
    def agent_harness(self) -> str:
        """Which agent harness to use: 'copilot' or 'claude'."""
        raw = self._raw.get("agent", {})
        val = raw.get("harness", "copilot") if isinstance(raw, dict) else "copilot"
        result = str(val).lower().strip()
        return result if result else "copilot"

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

    @property
    def max_concurrent_agents(self) -> int:
        val = _get(self._raw, "agent", "max_concurrent_agents", default=10)
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return 10

    @property
    def max_turns(self) -> int:
        val = _get(self._raw, "agent", "max_turns", default=20)
        try:
            v = int(val)
            if v <= 0:
                raise ConfigValidationError("agent.max_turns must be a positive integer")
            return v
        except (TypeError, ValueError):
            raise ConfigValidationError(f"agent.max_turns invalid: {val!r}")

    @property
    def max_retry_backoff_ms(self) -> int:
        val = _get(self._raw, "agent", "max_retry_backoff_ms", default=300000)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 300000

    @property
    def max_concurrent_agents_by_state(self) -> dict[str, int]:
        raw = _get(self._raw, "agent", "max_concurrent_agents_by_state", default={})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for k, v in raw.items():
            try:
                iv = int(v)
                if iv > 0:
                    result[str(k).lower()] = iv
            except (TypeError, ValueError):
                continue
        return result

    # --- copilot ---

    @property
    def copilot_command(self) -> str:
        val = _get(self._raw, "copilot", "command")
        if val is None:
            return "copilot-sdk"
        return str(val)

    @property
    def copilot_approval_policy(self) -> str | None:
        return _get(self._raw, "copilot", "approval_policy")

    @property
    def copilot_thread_sandbox(self) -> str | None:
        return _get(self._raw, "copilot", "thread_sandbox")

    @property
    def copilot_turn_sandbox_policy(self) -> str | None:
        return _get(self._raw, "copilot", "turn_sandbox_policy")

    @property
    def copilot_turn_timeout_ms(self) -> int:
        val = _get(self._raw, "copilot", "turn_timeout_ms", default=3600000)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 3600000

    @property
    def copilot_read_timeout_ms(self) -> int:
        val = _get(self._raw, "copilot", "read_timeout_ms", default=5000)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 5000

    @property
    def copilot_stall_timeout_ms(self) -> int:
        val = _get(self._raw, "copilot", "stall_timeout_ms", default=300000)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 300000

    # --- claude ---

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
            return int(sec.get("stall_timeout_ms", 300000))
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
        """Valid: default, acceptEdits, plan, bypassPermissions, dontAsk, auto."""
        sec = self._raw.get("claude", {})
        if not isinstance(sec, dict):
            return "auto"
        val = str(sec.get("permission_mode", "auto")).strip()
        valid = ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto")
        return val if val in valid else "auto"

    # --- server (extension) ---

    @property
    def server_port(self) -> int | None:
        val = _get(self._raw, "server", "port")
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    # --- validation ---

    def validate_dispatch(self) -> list[str]:
        """Run dispatch preflight validation. Returns list of error messages (empty = ok)."""
        errors: list[str] = []
        if not self.tracker_kind:
            errors.append("tracker.kind is required")
        elif self.tracker_kind != "github":
            errors.append(f"Unsupported tracker.kind: {self.tracker_kind!r}")
        if not self.tracker_api_key:
            errors.append("tracker.api_key is missing after $VAR resolution")
        if self.tracker_kind == "github" and not self.tracker_repo:
            errors.append("tracker.repo is required when tracker.kind is 'github'")

        # Validate agent harness
        harness = self.agent_harness
        if harness not in ("copilot", "claude"):
            errors.append(f"agent.harness must be 'copilot' or 'claude', got {harness!r}")

        if harness == "claude":
            try:
                import claude_agent_sdk  # noqa: F401
            except ImportError:
                errors.append(
                    "agent.harness is 'claude' but claude-agent-sdk is not installed"
                )

        return errors
