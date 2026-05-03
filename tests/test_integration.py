"""Integration tests — run the actual code and verify end-to-end behavior.

Covers SPEC §17.1–17.7 use cases that unit tests can't fully exercise.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import aiohttp
import pytest

from symphony.cli import _parse_args
from symphony.config import ServiceConfig
from symphony.errors import (
    MissingWorkflowFileError,
    SymphonyError,
    WorkflowFrontMatterNotAMapError,
    WorkflowParseError,
)
from symphony.models import Issue, WorkflowDefinition
from symphony.orchestrator import Orchestrator
from symphony.prompt import render_prompt
from symphony.server import SymphonyServer
from symphony.workflow import load_workflow, resolve_workflow_path
from symphony import workspace as ws_mod

PYTHON = sys.executable


# ---------------------------------------------------------------------------
# §17.7 — CLI and Host Lifecycle
# ---------------------------------------------------------------------------

class TestCLILifecycle:
    """Run the real CLI process and verify exit codes / output."""

    def test_cli_errors_on_nonexistent_explicit_path(self, tmp_path):
        """CLI exits nonzero when given a nonexistent explicit workflow path."""
        result = subprocess.run(
            [PYTHON, "-m", "symphony", str(tmp_path / "nonexistent.md")],
            capture_output=True, text=True, timeout=10,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0
        assert "missing_workflow_file" in result.stderr or "startup" in result.stderr.lower()

    def test_cli_errors_on_missing_default_workflow(self, tmp_path):
        """CLI exits nonzero when no ./WORKFLOW.md exists and no path given."""
        result = subprocess.run(
            [PYTHON, "-m", "symphony"],
            capture_output=True, text=True, timeout=10,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0

    def test_cli_errors_on_invalid_config(self, tmp_path):
        """CLI exits nonzero when WORKFLOW.md has invalid config (no tracker)."""
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: unsupported\n---\nPrompt")
        result = subprocess.run(
            [PYTHON, "-m", "symphony", str(wf)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
        assert "startup" in result.stderr.lower() or "Unsupported" in result.stderr

    def test_cli_accepts_positional_workflow(self, tmp_path, monkeypatch):
        """CLI accepts a positional workflow path argument."""
        args = _parse_args([str(tmp_path / "my.md")])
        assert args.workflow_path == str(tmp_path / "my.md")

    def test_cli_uses_default_workflow_when_no_arg(self):
        args = _parse_args([])
        assert args.workflow_path is None

    def test_cli_port_flag(self):
        args = _parse_args(["--port", "9090"])
        assert args.port == 9090


# ---------------------------------------------------------------------------
# §17.1 — Workflow and Config Parsing (integration)
# ---------------------------------------------------------------------------

class TestWorkflowConfigIntegration:
    """End-to-end config resolution through the real pipeline."""

    def test_full_pipeline_defaults(self, tmp_path, monkeypatch):
        """Minimal valid WORKFLOW.md → ServiceConfig with all defaults applied."""
        monkeypatch.setenv("GITHUB_TOKEN", "test_tok")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nDo work.")

        wf = load_workflow(str(wf_path))
        cfg = ServiceConfig(wf, str(tmp_path))

        assert cfg.tracker_kind == "github"
        assert cfg.tracker_api_key == "test_tok"
        assert cfg.tracker_repo == "o/r"
        assert cfg.poll_interval_ms == 30000
        assert cfg.max_concurrent_agents == 10
        assert cfg.max_turns == 20
        assert cfg.copilot_command == "copilot-sdk"
        assert cfg.hook_timeout_ms == 60000
        assert cfg.copilot_turn_timeout_ms == 3600000
        assert cfg.copilot_stall_timeout_ms == 300000
        assert cfg.validate_dispatch() == []

    def test_env_var_api_key_resolution(self, tmp_path, monkeypatch):
        """$VAR indirection in tracker.api_key resolves from environment."""
        monkeypatch.setenv("MY_CUSTOM_TOKEN", "resolved_value")
        wf_path = tmp_path / "WORKFLOW.md"
        wf_path.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n  api_key: $MY_CUSTOM_TOKEN\n---\n")

        wf = load_workflow(str(wf_path))
        cfg = ServiceConfig(wf, str(tmp_path))
        assert cfg.tracker_api_key == "resolved_value"

    def test_tilde_workspace_root(self, tmp_path):
        wf = WorkflowDefinition(config={"workspace": {"root": "~/my_ws"}}, prompt_template="")
        cfg = ServiceConfig(wf, str(tmp_path))
        assert cfg.workspace_root == os.path.expanduser("~/my_ws")

    def test_relative_workspace_root(self, tmp_path):
        wf = WorkflowDefinition(config={"workspace": {"root": "rel_dir"}}, prompt_template="")
        cfg = ServiceConfig(wf, str(tmp_path))
        assert cfg.workspace_root == os.path.abspath(os.path.join(str(tmp_path), "rel_dir"))

    def test_prompt_renders_issue_and_attempt(self):
        template = "Issue {{ issue.identifier }}: {{ issue.title }}{% if attempt %} (retry {{ attempt }}){% endif %}"
        r1 = render_prompt(template, {"identifier": "#5", "title": "Fix it"}, attempt=None)
        assert r1 == "Issue #5: Fix it"
        r2 = render_prompt(template, {"identifier": "#5", "title": "Fix it"}, attempt=3)
        assert r2 == "Issue #5: Fix it (retry 3)"


# ---------------------------------------------------------------------------
# §17.2 — Workspace Manager (integration)
# ---------------------------------------------------------------------------

class TestWorkspaceIntegration:
    """Exercise the real workspace filesystem lifecycle."""

    @pytest.mark.asyncio
    async def test_full_workspace_lifecycle(self, tmp_path):
        """Create → reuse → cleanup lifecycle with hooks."""
        marker_create = tmp_path / "hook_create.ok"
        marker_before = tmp_path / "hook_before.ok"
        marker_after = tmp_path / "hook_after.ok"
        marker_remove = tmp_path / "hook_remove.ok"

        raw = {
            "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
            "workspace": {"root": str(tmp_path / "workspaces")},
            "hooks": {
                "after_create": f"touch {marker_create}",
                "before_run": f"touch {marker_before}",
                "after_run": f"touch {marker_after}",
                "before_remove": f"touch {marker_remove}",
            },
        }
        cfg = ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))

        # 1. Create new workspace
        ws = await ws_mod.create_workspace(cfg, "#42")
        assert ws.created_now is True
        assert os.path.isdir(ws.path)
        assert marker_create.exists(), "after_create hook did not run"

        # 2. Reuse existing workspace
        ws2 = await ws_mod.create_workspace(cfg, "#42")
        assert ws2.created_now is False
        assert ws2.path == ws.path

        # 3. Run before_run hook
        await ws_mod.run_hook("before_run", cfg.hook_before_run, ws.path, cfg.hook_timeout_ms)
        assert marker_before.exists()

        # 4. Run after_run hook
        await ws_mod.run_hook("after_run", cfg.hook_after_run, ws.path, cfg.hook_timeout_ms)
        assert marker_after.exists()

        # 5. Cleanup workspace (runs before_remove, then deletes)
        await ws_mod.cleanup_workspace(cfg, "#42")
        assert marker_remove.exists(), "before_remove hook did not run"
        assert not os.path.exists(ws.path), "workspace directory was not removed"

    @pytest.mark.asyncio
    async def test_workspace_isolation(self, tmp_path):
        """Different issues get different workspace directories."""
        raw = {
            "tracker": {"kind": "github", "repo": "o/r", "api_key": "t"},
            "workspace": {"root": str(tmp_path / "ws")},
        }
        cfg = ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))

        ws1 = await ws_mod.create_workspace(cfg, "#1")
        ws2 = await ws_mod.create_workspace(cfg, "#2")
        assert ws1.path != ws2.path
        assert os.path.isdir(ws1.path)
        assert os.path.isdir(ws2.path)


# ---------------------------------------------------------------------------
# §17.4 — Orchestrator (integration with mock tracker)
# ---------------------------------------------------------------------------

class TestOrchestratorIntegration:
    """Start a real orchestrator and verify dispatch/reconciliation behavior."""

    @pytest.mark.asyncio
    async def test_startup_and_shutdown(self, tmp_path, monkeypatch):
        """Orchestrator starts, validates config, and shuts down cleanly."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nPrompt")

        orch = Orchestrator(str(wf))
        await orch.start()

        assert orch.config is not None
        assert orch.config.tracker_kind == "github"
        assert orch.state.poll_interval_ms == 30000

        await orch.stop()

    @pytest.mark.asyncio
    async def test_startup_fails_with_bad_config(self, tmp_path, monkeypatch):
        """Orchestrator raises on startup with invalid config."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nPrompt")

        orch = Orchestrator(str(wf))
        with pytest.raises(SymphonyError, match="Startup validation failed"):
            await orch.start()

    @pytest.mark.asyncio
    async def test_workflow_dynamic_reload(self, tmp_path, monkeypatch):
        """Workflow changes are detected and applied at runtime."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\npolling:\n  interval_ms: 5000\n---\nV1")

        orch = Orchestrator(str(wf))
        await orch.start()
        assert orch.state.poll_interval_ms == 5000
        assert orch.config.prompt_template == "V1"

        # Update workflow file
        time.sleep(0.1)  # ensure mtime differs
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\npolling:\n  interval_ms: 15000\n---\nV2")
        orch._check_workflow_reload()

        assert orch.state.poll_interval_ms == 15000
        assert orch.config.prompt_template == "V2"

        await orch.stop()

    @pytest.mark.asyncio
    async def test_invalid_reload_keeps_last_good(self, tmp_path, monkeypatch):
        """Invalid workflow reload keeps last-known-good config."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nGood")

        orch = Orchestrator(str(wf))
        await orch.start()
        good_cfg = orch.config
        assert good_cfg.prompt_template == "Good"

        time.sleep(0.1)
        wf.write_text("---\ntracker:\n  kind: jira\n---\nBroken")
        orch._check_workflow_reload()

        # Config unchanged
        assert orch.config is good_cfg
        assert orch.config.prompt_template == "Good"

        await orch.stop()

    @pytest.mark.asyncio
    async def test_snapshot_api(self, tmp_path, monkeypatch):
        """Snapshot returns correct structure."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")

        orch = Orchestrator(str(wf))
        await orch.start()

        snap = orch.get_snapshot()
        assert "generated_at" in snap
        assert snap["counts"]["running"] == 0
        assert snap["counts"]["retrying"] == 0
        assert "copilot_totals" in snap
        assert "input_tokens" in snap["copilot_totals"]
        assert "seconds_running" in snap["copilot_totals"]
        assert snap["rate_limits"] is None

        await orch.stop()


# ---------------------------------------------------------------------------
# §13.7 — HTTP Server Extension (integration)
# ---------------------------------------------------------------------------

class TestHTTPServerIntegration:
    """Start a real HTTP server and hit the endpoints."""

    @pytest.mark.asyncio
    async def test_server_lifecycle(self, tmp_path, monkeypatch):
        """Server starts on ephemeral port, serves endpoints, and stops."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")

        orch = Orchestrator(str(wf))
        await orch.start()

        server = SymphonyServer(orch)
        port = await server.start(0)  # ephemeral port
        assert port > 0

        async with aiohttp.ClientSession() as session:
            # GET /api/v1/state
            async with session.get(f"http://127.0.0.1:{port}/api/v1/state") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["counts"]["running"] == 0
                assert "copilot_totals" in data

            # GET / (dashboard)
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                html = await resp.text()
                assert "Symphony Dashboard" in html

            # GET /api/v1/<nonexistent>
            async with session.get(f"http://127.0.0.1:{port}/api/v1/999") as resp:
                assert resp.status == 404
                err = await resp.json()
                assert err["error"]["code"] == "issue_not_found"

            # POST /api/v1/refresh
            async with session.post(f"http://127.0.0.1:{port}/api/v1/refresh") as resp:
                assert resp.status == 202
                data = await resp.json()
                assert data["queued"] is True

            # DELETE on GET-only route → 405
            async with session.delete(f"http://127.0.0.1:{port}/api/v1/state") as resp:
                assert resp.status == 405

        await server.stop()
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.5 — Agent Runner (integration with mock agent subprocess)
# ---------------------------------------------------------------------------

class TestAgentRunnerIntegration:
    """Run the real runner against a mock agent subprocess."""

    @pytest.mark.asyncio
    async def test_single_turn_success(self, tmp_path, monkeypatch):
        """A mock agent that completes one turn successfully."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        ws = tmp_path / "ws" / "_1"
        ws.mkdir(parents=True)

        script = tmp_path / "mock_agent.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'  # init
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'  # thread/create
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"thread-1"}}\'\n'
            'read line\n'  # turn/start
            'echo \'{"jsonrpc":"2.0","id":3,"result":{"turnId":"turn-1"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            'read line\n'  # shutdown
        )
        os.chmod(str(script), 0o755)

        from symphony.runner import run_agent_session
        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                    "copilot": {"command": f"bash {script}", "turn_timeout_ms": 5000, "read_timeout_ms": 2000},
                },
                prompt_template="Do work on {{ issue.identifier }}",
            ),
            str(tmp_path),
        )
        issue = Issue(id="id1", identifier="#1", title="Test", state="open")

        events = []

        # Return closed after first turn so runner doesn't attempt continuation
        async def mock_refresh(issue_id):
            return Issue(id=issue_id, identifier="#1", title="t", state="closed")

        session = await run_agent_session(
            config=cfg,
            workspace_path=str(ws),
            issue=issue,
            prompt="Do work on #1",
            attempt=None,
            on_event=lambda e: events.append(e),
            max_turns=5,
            fetch_issue_state=mock_refresh,
        )

        assert session.thread_id == "thread-1"
        assert session.turn_count == 1
        assert session.session_id == "thread-1-turn-1"
        assert any(e.event == "session_started" for e in events)
        assert any(e.event == "turn_completed" for e in events)

    @pytest.mark.asyncio
    async def test_multi_turn_continuation(self, tmp_path, monkeypatch):
        """Mock agent handles 2 turns, then issue becomes non-active."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        ws = tmp_path / "ws" / "_2"
        ws.mkdir(parents=True)

        script = tmp_path / "multi_agent.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"t1"}}\'\n'
            # Turn 1
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":3,"result":{"turnId":"turn-1"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            # Turn 2
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":4,"result":{"turnId":"turn-2"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\'\n'
            'read line\n'
        )
        os.chmod(str(script), 0o755)

        from symphony.runner import run_agent_session
        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                    "copilot": {"command": f"bash {script}", "turn_timeout_ms": 5000, "read_timeout_ms": 2000},
                },
                prompt_template="",
            ),
            str(tmp_path),
        )
        issue = Issue(id="id1", identifier="#1", title="Test", state="open")

        call_count = 0
        async def mock_refresh(issue_id):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return Issue(id=issue_id, identifier="#1", title="t", state="closed")
            return Issue(id=issue_id, identifier="#1", title="t", state="open")

        session = await run_agent_session(
            config=cfg,
            workspace_path=str(ws),
            issue=issue,
            prompt="work",
            attempt=None,
            max_turns=10,
            fetch_issue_state=mock_refresh,
        )

        assert session.turn_count == 2
        assert session.thread_id == "t1"

    @pytest.mark.asyncio
    async def test_agent_turn_failure(self, tmp_path, monkeypatch):
        """Agent that reports turn failure raises TurnFailedError."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        ws = tmp_path / "ws" / "_3"
        ws.mkdir(parents=True)

        script = tmp_path / "fail_agent.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"t1"}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":3,"result":{"turnId":"turn-1"}}\'\n'
            'echo \'{"jsonrpc":"2.0","method":"turn/failed","params":{"error":"model refused"}}\'\n'
        )
        os.chmod(str(script), 0o755)

        from symphony.errors import TurnFailedError
        from symphony.runner import run_agent_session
        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                    "copilot": {"command": f"bash {script}", "turn_timeout_ms": 5000, "read_timeout_ms": 2000},
                },
                prompt_template="",
            ),
            str(tmp_path),
        )
        issue = Issue(id="id1", identifier="#1", title="Test", state="open")

        with pytest.raises(TurnFailedError):
            await run_agent_session(
                config=cfg, workspace_path=str(ws), issue=issue,
                prompt="work", attempt=None, max_turns=5,
            )

    @pytest.mark.asyncio
    async def test_agent_subprocess_exit(self, tmp_path, monkeypatch):
        """Agent subprocess that exits mid-session raises PortExitError."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        ws = tmp_path / "ws" / "_4"
        ws.mkdir(parents=True)

        script = tmp_path / "exit_agent.sh"
        script.write_text(
            '#!/bin/bash\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\'\n'
            'read line\n'
            'echo \'{"jsonrpc":"2.0","id":2,"result":{"threadId":"t1"}}\'\n'
            'read line\n'
            'exit 0\n'  # exit without completing turn
        )
        os.chmod(str(script), 0o755)

        from symphony.errors import PortExitError
        from symphony.runner import run_agent_session
        cfg = ServiceConfig(
            WorkflowDefinition(
                config={
                    "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                    "copilot": {"command": f"bash {script}", "turn_timeout_ms": 5000, "read_timeout_ms": 2000},
                },
                prompt_template="",
            ),
            str(tmp_path),
        )
        issue = Issue(id="id1", identifier="#1", title="Test", state="open")

        with pytest.raises(PortExitError):
            await run_agent_session(
                config=cfg, workspace_path=str(ws), issue=issue,
                prompt="work", attempt=None, max_turns=5,
            )


# ---------------------------------------------------------------------------
# §17.6 — Observability (integration)
# ---------------------------------------------------------------------------

class TestObservabilityIntegration:
    """Verify structured logging and snapshot correctness."""

    @pytest.mark.asyncio
    async def test_structured_logging_output(self, tmp_path, monkeypatch, capfd):
        """Startup emits structured JSON logs to stderr."""
        from symphony.logging_config import configure_logging
        configure_logging("INFO")

        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nP")

        orch = Orchestrator(str(wf))
        await orch.start()
        await orch.stop()

        captured = capfd.readouterr()
        lines = [l for l in captured.err.strip().split("\n") if l.strip()]
        # Should have at least workflow_loaded and orchestrator_started/stopped
        assert len(lines) >= 2
        for line in lines:
            parsed = json.loads(line)
            assert "ts" in parsed
            assert "level" in parsed
            assert "msg" in parsed
