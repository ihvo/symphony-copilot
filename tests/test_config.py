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
