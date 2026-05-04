"""Tests for dev mode components (MockTracker, DevHarness, CLI sidecar)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from symphony.dev import (
    MockTracker,
    cleanup_pid_file,
    dev_workspace_root,
    generate_instance_id,
    mount_dev_routes,
    validate_instance_id,
    write_pid_file,
    write_port_file,
)
from symphony.errors import InstanceAlreadyRunningError


class TestMockTracker:
    def test_add_issue_auto_number(self):
        tracker = MockTracker()
        issue = tracker.add_issue(title="First")
        assert issue["number"] == 1
        assert issue["title"] == "First"
        assert issue["state"] == "open"
        issue2 = tracker.add_issue(title="Second")
        assert issue2["number"] == 2

    def test_add_issue_explicit_number(self):
        tracker = MockTracker()
        issue = tracker.add_issue(number=42, title="Explicit")
        assert issue["number"] == 42
        # Next auto-number should be 43
        issue2 = tracker.add_issue(title="Auto")
        assert issue2["number"] == 43

    def test_set_state(self):
        tracker = MockTracker()
        tracker.add_issue(number=1)
        assert tracker.set_state(1, "closed") is True
        assert tracker.issues[1]["state"] == "closed"

    def test_set_state_missing(self):
        tracker = MockTracker()
        assert tracker.set_state(999, "closed") is False

    def test_remove_issue(self):
        tracker = MockTracker()
        tracker.add_issue(number=1)
        assert tracker.remove_issue(1) is True
        assert tracker.remove_issue(1) is False
        assert 1 not in tracker.issues

    def test_seed(self):
        tracker = MockTracker()
        created = tracker.seed(5)
        assert len(created) == 5
        assert all(i["state"] == "open" for i in created)
        # Should have varied labels
        labels = [i["labels"][0]["name"] for i in created]
        assert any("priority/" in lbl for lbl in labels)

    def test_list_issues_filter(self):
        tracker = MockTracker()
        tracker.add_issue(number=1, state="open")
        tracker.add_issue(number=2, state="closed")
        tracker.add_issue(number=3, state="open")

        open_issues = tracker.list_issues(state="open")
        assert len(open_issues) == 2
        assert all(i["state"] == "open" for i in open_issues)

        closed_issues = tracker.list_issues(state="closed")
        assert len(closed_issues) == 1

    def test_list_issues_all(self):
        tracker = MockTracker()
        tracker.add_issue(number=1, state="open")
        tracker.add_issue(number=2, state="closed")
        all_issues = tracker.list_issues()
        assert len(all_issues) == 2

    def test_get_issue(self):
        tracker = MockTracker()
        tracker.add_issue(number=7, title="Lucky seven")
        assert tracker.get_issue(7)["title"] == "Lucky seven"
        assert tracker.get_issue(99) is None

    def test_inject_and_clear_errors(self):
        tracker = MockTracker()
        tracker.inject_error("list", 500, "boom")
        assert "list" in tracker.errors
        tracker.clear_errors()
        assert len(tracker.errors) == 0

    def test_add_issue_with_labels(self):
        tracker = MockTracker()
        issue = tracker.add_issue(title="Labeled", labels=["bug", "p1"])
        assert len(issue["labels"]) == 2
        assert issue["labels"][0]["name"] == "bug"


class TestDevRoutes:
    @pytest.fixture
    def app_client(self):
        app = FastAPI()
        tracker = MockTracker()
        mount_dev_routes(app, tracker)
        transport = ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        return client, tracker

    @pytest.mark.asyncio
    async def test_github_list_issues(self, app_client):
        client, tracker = app_client
        tracker.add_issue(number=1, title="Issue 1", state="open")
        tracker.add_issue(number=2, title="Issue 2", state="open")
        resp = await client.get("/_dev/github/repos/dev/local/issues?state=open")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_github_list_issues_pagination(self, app_client):
        client, tracker = app_client
        for i in range(5):
            tracker.add_issue(title=f"Issue {i + 1}", state="open")
        resp = await client.get("/_dev/github/repos/dev/local/issues?state=open&per_page=2&page=1")
        assert len(resp.json()) == 2
        resp2 = await client.get("/_dev/github/repos/dev/local/issues?state=open&per_page=2&page=3")
        assert len(resp2.json()) == 1

    @pytest.mark.asyncio
    async def test_github_get_issue(self, app_client):
        client, tracker = app_client
        tracker.add_issue(number=5, title="Five")
        resp = await client.get("/_dev/github/repos/dev/local/issues/5")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Five"

    @pytest.mark.asyncio
    async def test_github_get_issue_not_found(self, app_client):
        client, tracker = app_client
        resp = await client.get("/_dev/github/repos/dev/local/issues/999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_github_error_injection(self, app_client):
        client, tracker = app_client
        tracker.add_issue(number=1)
        tracker.inject_error("list", 503, "overloaded")
        resp = await client.get("/_dev/github/repos/dev/local/issues?state=open")
        assert resp.status_code == 503
        assert resp.json()["message"] == "overloaded"

    @pytest.mark.asyncio
    async def test_control_create_issue(self, app_client):
        client, tracker = app_client
        resp = await client.post("/dev/issues", json={
            "title": "New Issue",
            "state": "open",
            "labels": ["bug"],
        })
        assert resp.status_code == 201
        assert resp.json()["title"] == "New Issue"
        assert len(tracker.issues) == 1

    @pytest.mark.asyncio
    async def test_control_update_issue(self, app_client):
        client, tracker = app_client
        tracker.add_issue(number=1, title="Old", state="open")
        resp = await client.patch("/dev/issues/1", json={"state": "closed", "title": "Updated"})
        assert resp.status_code == 200
        assert tracker.issues[1]["state"] == "closed"
        assert tracker.issues[1]["title"] == "Updated"

    @pytest.mark.asyncio
    async def test_control_delete_issue(self, app_client):
        client, tracker = app_client
        tracker.add_issue(number=1)
        resp = await client.delete("/dev/issues/1")
        assert resp.status_code == 200
        assert 1 not in tracker.issues

    @pytest.mark.asyncio
    async def test_control_seed(self, app_client):
        client, tracker = app_client
        resp = await client.post("/dev/issues/seed", json={"count": 3})
        assert resp.status_code == 200
        assert resp.json()["created"] == 3
        assert len(tracker.issues) == 3

    @pytest.mark.asyncio
    async def test_control_inject_clear_errors(self, app_client):
        client, tracker = app_client
        resp = await client.post("/dev/errors", json={"key": "list", "status": 500})
        assert resp.status_code == 200
        assert "list" in tracker.errors

        resp = await client.delete("/dev/errors")
        assert resp.status_code == 200
        assert len(tracker.errors) == 0


class TestDevUtilities:
    def test_generate_instance_id(self):
        id1 = generate_instance_id()
        id2 = generate_instance_id()
        assert len(id1) == 8
        assert id1 != id2

    def test_dev_workspace_root(self):
        root = dev_workspace_root("/tmp/workspaces", "alice")
        assert root == "/tmp/workspaces/_dev_alice"

    def test_instance_id_validation_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid instance ID"):
            validate_instance_id("../../etc")

    def test_instance_id_validation_rejects_slashes(self):
        with pytest.raises(ValueError, match="Invalid instance ID"):
            dev_workspace_root("/tmp", "foo/bar")

    def test_instance_id_validation_accepts_valid(self):
        assert validate_instance_id("my-instance_01") == "my-instance_01"

    def test_write_port_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_port_file(tmpdir, 8234)
            port_file = os.path.join(tmpdir, ".symphony-dev.port")
            assert os.path.exists(port_file)
            with open(port_file) as f:
                assert f.read() == "8234"

    def test_write_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_pid_file(tmpdir)
            pid_file = os.path.join(tmpdir, ".symphony-dev.pid")
            assert os.path.exists(pid_file)
            with open(pid_file) as f:
                assert int(f.read()) == os.getpid()

    def test_write_pid_file_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_pid_file(tmpdir)
            # Should raise because current process is alive
            with pytest.raises(InstanceAlreadyRunningError):
                write_pid_file(tmpdir)

    def test_write_pid_file_stale_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = os.path.join(tmpdir, ".symphony-dev.pid")
            # Write a PID that doesn't exist
            with open(pid_file, "w") as f:
                f.write("99999999")
            # Should succeed because PID is dead
            write_pid_file(tmpdir)

    def test_cleanup_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_pid_file(tmpdir)
            pid_file = os.path.join(tmpdir, ".symphony-dev.pid")
            assert os.path.exists(pid_file)
            cleanup_pid_file(tmpdir)
            assert not os.path.exists(pid_file)


class TestDevHarness:
    @pytest.mark.asyncio
    async def test_dev_harness_basic_session(self):
        """DevHarness can start, run turns, and stop."""
        from symphony.config import ServiceConfig
        from symphony.dev_harness import DevHarness
        from symphony.models import Issue, WorkflowDefinition

        wf = WorkflowDefinition(
            config={
                "tracker": {"kind": "github", "repo": "dev/local", "api_key": "dev-token"},
                "dev": {"agent_behavior": "success", "agent_turns": 2, "agent_delay_ms": 0},
                "copilot": {"turn_timeout_ms": 10000},
            },
            prompt_template="Fix issue {{ issue.title }}",
        )
        cfg = ServiceConfig(wf, "/tmp")

        issue = Issue(
            id="1000",
            identifier="#1",
            title="Test issue",
            state="open",
        )

        events = []

        def on_event(evt):
            events.append(evt)

        with tempfile.TemporaryDirectory() as workspace:
            harness = DevHarness(cfg, workspace, issue, on_event=on_event)
            await harness.start()

            assert harness.session.thread_id == "mock-thread-1"
            assert harness._started is True

            # Run first turn
            result = await harness.run_turn("Fix the bug", turn_number=1)
            assert result is True
            assert harness.session.turn_count == 1

            # Run second turn
            result = await harness.run_turn("Continue", turn_number=2)
            assert result is True
            assert harness.session.turn_count == 2

            await harness.stop()
            assert harness._started is False

        # Should have events
        assert any(e.event == "session_started" for e in events)
        assert sum(1 for e in events if e.event == "turn_completed") == 2

    @pytest.mark.asyncio
    async def test_dev_harness_failure_behavior(self):
        """DevHarness raises TurnFailedError on 'fail' behavior."""
        from symphony.config import ServiceConfig
        from symphony.dev_harness import DevHarness
        from symphony.errors import TurnFailedError
        from symphony.models import Issue, WorkflowDefinition

        wf = WorkflowDefinition(
            config={
                "tracker": {"kind": "github", "repo": "dev/local", "api_key": "dev-token"},
                "dev": {"agent_behavior": "fail", "agent_turns": 1, "agent_delay_ms": 0},
                "copilot": {"turn_timeout_ms": 10000},
            },
            prompt_template="Fix {{ issue.title }}",
        )
        cfg = ServiceConfig(wf, "/tmp")
        issue = Issue(id="2000", identifier="#2", title="Fail test", state="open")

        with tempfile.TemporaryDirectory() as workspace:
            harness = DevHarness(cfg, workspace, issue)
            await harness.start()
            with pytest.raises(TurnFailedError):
                await harness.run_turn("Do work", turn_number=1)
            await harness.stop()

    @pytest.mark.asyncio
    async def test_run_dev_agent_session(self):
        """Full run_dev_agent_session lifecycle."""
        from symphony.config import ServiceConfig
        from symphony.dev_harness import run_dev_agent_session
        from symphony.models import Issue, WorkflowDefinition

        wf = WorkflowDefinition(
            config={
                "tracker": {"kind": "github", "repo": "dev/local", "api_key": "dev-token"},
                "dev": {"agent_behavior": "success", "agent_turns": 2, "agent_delay_ms": 0},
                "copilot": {"turn_timeout_ms": 10000},
            },
            prompt_template="Fix {{ issue.title }}",
        )
        cfg = ServiceConfig(wf, "/tmp")
        issue = Issue(id="3000", identifier="#3", title="Session test", state="open")

        with tempfile.TemporaryDirectory() as workspace:
            session = await run_dev_agent_session(
                config=cfg,
                workspace_path=workspace,
                issue=issue,
                prompt="Fix the issue",
                attempt=None,
                max_turns=2,
            )
            assert session.turn_count == 2
            assert session.thread_id == "mock-thread-1"


class TestDevConfig:
    def test_dev_config_defaults(self):
        from symphony.config import ServiceConfig
        from symphony.models import WorkflowDefinition

        wf = WorkflowDefinition(
            config={"tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"}},
            prompt_template="",
        )
        cfg = ServiceConfig(wf, "/tmp")
        assert cfg.dev_agent_behavior == "success"
        assert cfg.dev_agent_turns == 3
        assert cfg.dev_agent_delay_ms == 2000
        assert cfg.dev_poll_interval_ms == 5000

    def test_dev_config_custom(self):
        from symphony.config import ServiceConfig
        from symphony.models import WorkflowDefinition

        wf = WorkflowDefinition(
            config={
                "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
                "dev": {
                    "agent_behavior": "multi-turn",
                    "agent_turns": 5,
                    "agent_delay_ms": 500,
                    "poll_interval_ms": 2000,
                },
            },
            prompt_template="",
        )
        cfg = ServiceConfig(wf, "/tmp")
        assert cfg.dev_agent_behavior == "multi-turn"
        assert cfg.dev_agent_turns == 5
        assert cfg.dev_agent_delay_ms == 500
        assert cfg.dev_poll_interval_ms == 2000

    def test_validate_dispatch_dev_mode_skips_api_key(self):
        from symphony.config import ServiceConfig
        from symphony.models import WorkflowDefinition

        wf = WorkflowDefinition(
            config={"tracker": {"kind": "github", "repo": "dev/local"}},
            prompt_template="",
        )
        cfg = ServiceConfig(wf, "/tmp")
        # Normal mode: fails due to missing api_key
        errors = cfg.validate_dispatch(dev_mode=False)
        assert any("api_key" in e for e in errors)
        # Dev mode: passes
        errors = cfg.validate_dispatch(dev_mode=True)
        assert not errors


class TestCLI:
    def test_parse_dev_flag(self):
        from symphony.cli import _parse_args

        args = _parse_args(["workflow.md", "--dev"])
        assert args.dev is True
        assert args.workflow_path == "workflow.md"

    def test_parse_instance_flag(self):
        from symphony.cli import _parse_args

        args = _parse_args(["--dev", "--instance", "alice"])
        assert args.instance == "alice"

    def test_parse_dev_seed_flag(self):
        from symphony.cli import _parse_args

        args = _parse_args(["--dev", "--dev-seed", "10"])
        assert args.dev_seed == 10

    def test_parse_no_dev_defaults(self):
        from symphony.cli import _parse_args

        args = _parse_args([])
        assert args.dev is False
        assert args.instance is None
        assert args.dev_seed == 0
