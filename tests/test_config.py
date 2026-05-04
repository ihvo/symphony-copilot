"""Tests for config layer (SPEC §6, §17.1)."""

from __future__ import annotations

import os
import tempfile

import pytest

from symphony.config import ServiceConfig
from symphony.models import WorkflowDefinition


def _make_config(raw: dict, workflow_dir: str = "/tmp") -> ServiceConfig:
    wf = WorkflowDefinition(config=raw, prompt_template="test")
    return ServiceConfig(wf, workflow_dir)


class TestDefaults:
    def test_tracker_defaults(self):
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}})
        assert cfg.tracker_endpoint == "https://api.github.com"
        assert cfg.active_states == ["open"]
        assert cfg.terminal_states == ["closed"]

    def test_polling_default(self):
        cfg = _make_config({})
        assert cfg.poll_interval_ms == 30000

    def test_workspace_root_default(self):
        cfg = _make_config({})
        assert cfg.workspace_root == os.path.join(tempfile.gettempdir(), "symphony_workspaces")

    def test_agent_defaults(self):
        cfg = _make_config({})
        assert cfg.max_concurrent_agents == 10
        assert cfg.max_turns == 20
        assert cfg.max_retry_backoff_ms == 300000
        assert cfg.max_concurrent_agents_by_state == {}

    def test_copilot_defaults(self):
        cfg = _make_config({})
        assert cfg.copilot_command == "copilot-sdk"
        assert cfg.copilot_turn_timeout_ms == 3600000
        assert cfg.copilot_read_timeout_ms == 5000
        assert cfg.copilot_stall_timeout_ms == 300000

    def test_hook_timeout_default(self):
        cfg = _make_config({})
        assert cfg.hook_timeout_ms == 60000


class TestEnvResolution:
    def test_api_key_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r", "api_key": "$MY_TOKEN"}})
        assert cfg.tracker_api_key == "secret123"

    def test_api_key_empty_env(self, monkeypatch):
        monkeypatch.setenv("EMPTY_VAR", "")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r", "api_key": "$EMPTY_VAR"}})
        # Falls back to GITHUB_TOKEN
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert cfg.tracker_api_key == ""

    def test_api_key_github_token_fallback(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh_fallback")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}})
        assert cfg.tracker_api_key == "gh_fallback"

    def test_api_key_literal(self):
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r", "api_key": "ghp_literal"}})
        assert cfg.tracker_api_key == "ghp_literal"


class TestWorkspaceRoot:
    def test_tilde_expansion(self):
        cfg = _make_config({"workspace": {"root": "~/my_workspaces"}})
        assert cfg.workspace_root == os.path.expanduser("~/my_workspaces")

    def test_relative_path(self, tmp_path):
        cfg = _make_config({"workspace": {"root": "rel_ws"}}, workflow_dir=str(tmp_path))
        expected = os.path.join(str(tmp_path), "rel_ws")
        assert cfg.workspace_root == os.path.abspath(expected)

    def test_env_var_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WS_DIR", str(tmp_path / "env_ws"))
        cfg = _make_config({"workspace": {"root": "$WS_DIR"}})
        assert cfg.workspace_root == str(tmp_path / "env_ws")


class TestPerStateConcurrency:
    def test_normalizes_state_names(self):
        cfg = _make_config({"agent": {"max_concurrent_agents_by_state": {"Todo": 1, "In Progress": 2}}})
        result = cfg.max_concurrent_agents_by_state
        assert result["todo"] == 1
        assert result["in progress"] == 2

    def test_ignores_invalid_values(self):
        cfg = _make_config({"agent": {"max_concurrent_agents_by_state": {"a": "bad", "b": -1, "c": 0, "d": 3}}})
        result = cfg.max_concurrent_agents_by_state
        assert "a" not in result
        assert "b" not in result
        assert "c" not in result
        assert result["d"] == 3


class TestCopilotCommand:
    def test_preserved_as_shell_string(self):
        cfg = _make_config({"copilot": {"command": "npx copilot-sdk --mode app-server"}})
        assert cfg.copilot_command == "npx copilot-sdk --mode app-server"


class TestValidation:
    def test_valid_config_no_errors(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}})
        assert cfg.validate_dispatch() == []

    def test_missing_tracker_kind(self):
        cfg = _make_config({})
        errors = cfg.validate_dispatch()
        assert any("tracker.kind" in e for e in errors)

    def test_unsupported_tracker_kind(self):
        cfg = _make_config({"tracker": {"kind": "jira"}})
        errors = cfg.validate_dispatch()
        assert any("Unsupported" in e for e in errors)

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}})
        errors = cfg.validate_dispatch()
        assert any("api_key" in e for e in errors)

    def test_missing_repo(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        cfg = _make_config({"tracker": {"kind": "github"}})
        errors = cfg.validate_dispatch()
        assert any("tracker.repo" in e for e in errors)

    def test_empty_copilot_command_still_valid(self, monkeypatch):
        """copilot.command is no longer validated (SDK manages subprocess)."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}, "copilot": {"command": ""}})
        errors = cfg.validate_dispatch()
        assert errors == []

    def test_unknown_harness_rejected(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}, "agent": {"harness": "openai"}})
        errors = cfg.validate_dispatch()
        assert any("agent.harness" in e for e in errors)

    def test_claude_harness_missing_sdk(self, monkeypatch):
        import builtins

        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}, "agent": {"harness": "claude"}})

        original_import = builtins.__import__

        def _block_claude(name, *args, **kwargs):
            if name == "claude_agent_sdk":
                raise ImportError("mocked missing")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_claude)
        errors = cfg.validate_dispatch()
        assert any("claude-agent-sdk" in e for e in errors)


class TestAgentHarnessConfig:
    def test_default_harness_is_copilot(self):
        cfg = _make_config({})
        assert cfg.agent_harness == "copilot"

    def test_explicit_copilot(self):
        cfg = _make_config({"agent": {"harness": "copilot"}})
        assert cfg.agent_harness == "copilot"

    def test_explicit_claude(self):
        cfg = _make_config({"agent": {"harness": "claude"}})
        assert cfg.agent_harness == "claude"

    def test_case_insensitive(self):
        cfg = _make_config({"agent": {"harness": "Claude"}})
        assert cfg.agent_harness == "claude"

    def test_empty_harness_defaults_to_copilot(self):
        cfg = _make_config({"agent": {"harness": ""}})
        assert cfg.agent_harness == "copilot"

    def test_whitespace_harness_defaults_to_copilot(self):
        cfg = _make_config({"agent": {"harness": "  "}})
        assert cfg.agent_harness == "copilot"

    def test_invalid_harness_fails_validation(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        cfg = _make_config({"tracker": {"kind": "github", "repo": "o/r"}, "agent": {"harness": "unknown"}})
        errors = cfg.validate_dispatch()
        assert any("must be" in e for e in errors)

    def test_agent_turn_timeout_dispatches_copilot(self):
        cfg = _make_config({"agent": {"harness": "copilot"}, "copilot": {"turn_timeout_ms": 9000}})
        assert cfg.agent_turn_timeout_ms == 9000

    def test_agent_turn_timeout_dispatches_claude(self):
        cfg = _make_config({"agent": {"harness": "claude"}, "claude": {"turn_timeout_ms": 7000}})
        assert cfg.agent_turn_timeout_ms == 7000

    def test_agent_stall_timeout_dispatches_copilot(self):
        cfg = _make_config({"agent": {"harness": "copilot"}, "copilot": {"stall_timeout_ms": 120000}})
        assert cfg.agent_stall_timeout_ms == 120000

    def test_agent_stall_timeout_dispatches_claude(self):
        cfg = _make_config({"agent": {"harness": "claude"}, "claude": {"stall_timeout_ms": 180000}})
        assert cfg.agent_stall_timeout_ms == 180000


class TestClaudeConfigProperties:
    def test_claude_command_default(self):
        cfg = _make_config({})
        assert cfg.claude_command == "claude"

    def test_claude_command_custom(self):
        cfg = _make_config({"claude": {"command": "/usr/local/bin/claude-code"}})
        assert cfg.claude_command == "/usr/local/bin/claude-code"

    def test_claude_turn_timeout_default(self):
        cfg = _make_config({})
        assert cfg.claude_turn_timeout_ms == 3600000

    def test_claude_turn_timeout_custom(self):
        cfg = _make_config({"claude": {"turn_timeout_ms": 120000}})
        assert cfg.claude_turn_timeout_ms == 120000

    def test_claude_turn_timeout_invalid_returns_default(self):
        cfg = _make_config({"claude": {"turn_timeout_ms": "bad"}})
        assert cfg.claude_turn_timeout_ms == 3600000

    def test_claude_turn_timeout_zero_returns_default(self):
        cfg = _make_config({"claude": {"turn_timeout_ms": 0}})
        assert cfg.claude_turn_timeout_ms == 3600000

    def test_claude_stall_timeout_default(self):
        cfg = _make_config({})
        assert cfg.claude_stall_timeout_ms == 300000

    def test_claude_stall_timeout_custom(self):
        cfg = _make_config({"claude": {"stall_timeout_ms": 600000}})
        assert cfg.claude_stall_timeout_ms == 600000

    def test_claude_system_prompt_default(self):
        cfg = _make_config({})
        assert cfg.claude_system_prompt is None

    def test_claude_system_prompt_set(self):
        cfg = _make_config({"claude": {"system_prompt": "You are a coder."}})
        assert cfg.claude_system_prompt == "You are a coder."

    def test_claude_allowed_tools_default(self):
        cfg = _make_config({})
        assert cfg.claude_allowed_tools == []

    def test_claude_allowed_tools_list(self):
        cfg = _make_config({"claude": {"allowed_tools": ["bash", "editor", "mcp"]}})
        assert cfg.claude_allowed_tools == ["bash", "editor", "mcp"]

    def test_claude_model_default(self):
        cfg = _make_config({})
        assert cfg.claude_model is None

    def test_claude_model_set(self):
        cfg = _make_config({"claude": {"model": "claude-sonnet-4-20250514"}})
        assert cfg.claude_model == "claude-sonnet-4-20250514"

    def test_claude_permission_mode_default(self):
        cfg = _make_config({})
        assert cfg.claude_permission_mode == "auto"

    def test_claude_permission_mode_bypass(self):
        cfg = _make_config({"claude": {"permission_mode": "bypassPermissions"}})
        assert cfg.claude_permission_mode == "bypassPermissions"

    def test_claude_permission_mode_accept_edits(self):
        cfg = _make_config({"claude": {"permission_mode": "acceptEdits"}})
        assert cfg.claude_permission_mode == "acceptEdits"

    def test_claude_permission_mode_invalid_fallback(self):
        cfg = _make_config({"claude": {"permission_mode": "yolo"}})
        assert cfg.claude_permission_mode == "auto"

    def test_claude_section_not_dict(self):
        """When claude section is not a dict, defaults apply."""
        cfg = _make_config({"claude": "invalid"})
        assert cfg.claude_command == "claude"
        assert cfg.claude_turn_timeout_ms == 3600000
        assert cfg.claude_model is None
        assert cfg.claude_allowed_tools == []
