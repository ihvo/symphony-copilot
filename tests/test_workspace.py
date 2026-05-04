"""Tests for workspace manager (SPEC §9, §17.2)."""

from __future__ import annotations

import os

import pytest

from symphony.config import ServiceConfig
from symphony.errors import HookError, HookTimeoutError, InvalidWorkspacePathError
from symphony.models import WorkflowDefinition
from symphony.workspace import (
    cleanup_workspace,
    create_workspace,
    run_hook,
    sanitize_identifier,
    validate_workspace_path,
    workspace_path_for,
)


def _cfg(tmp_path, hooks=None) -> ServiceConfig:
    raw = {
        "tracker": {"kind": "github", "repo": "o/r", "api_key": "tok"},
        "workspace": {"root": str(tmp_path / "workspaces")},
        "hooks": hooks or {},
    }
    return ServiceConfig(WorkflowDefinition(config=raw, prompt_template=""), str(tmp_path))


class TestSanitizeIdentifier:
    def test_simple(self):
        assert sanitize_identifier("#123") == "_123"

    def test_complex(self):
        assert sanitize_identifier("MT-649") == "MT-649"

    def test_spaces_and_special(self):
        assert sanitize_identifier("issue #1/2") == "issue__1_2"

    def test_already_safe(self):
        assert sanitize_identifier("hello-world_1.0") == "hello-world_1.0"

    def test_empty(self):
        assert sanitize_identifier("") == ""


class TestWorkspacePath:
    def test_deterministic(self):
        p1 = workspace_path_for("/root", "#123")
        p2 = workspace_path_for("/root", "#123")
        assert p1 == p2

    def test_different_identifiers(self):
        p1 = workspace_path_for("/root", "#1")
        p2 = workspace_path_for("/root", "#2")
        assert p1 != p2


class TestValidateWorkspacePath:
    def test_valid_path(self, tmp_path):
        validate_workspace_path(
            str(tmp_path / "workspaces" / "issue1"),
            str(tmp_path / "workspaces"),
        )

    def test_outside_root_raises(self, tmp_path):
        with pytest.raises(InvalidWorkspacePathError):
            validate_workspace_path(
                str(tmp_path / "other" / "issue1"),
                str(tmp_path / "workspaces"),
            )

    def test_equal_to_root_raises(self, tmp_path):
        root = str(tmp_path / "workspaces")
        with pytest.raises(InvalidWorkspacePathError):
            validate_workspace_path(root, root)

    def test_traversal_attack(self, tmp_path):
        with pytest.raises(InvalidWorkspacePathError):
            validate_workspace_path(
                str(tmp_path / "workspaces" / ".." / "etc" / "passwd"),
                str(tmp_path / "workspaces"),
            )


class TestCreateWorkspace:
    @pytest.mark.asyncio
    async def test_creates_new_directory(self, tmp_path):
        cfg = _cfg(tmp_path)
        ws = await create_workspace(cfg, "#42")
        assert ws.created_now is True
        assert os.path.isdir(ws.path)
        assert ws.workspace_key == "_42"

    @pytest.mark.asyncio
    async def test_reuses_existing(self, tmp_path):
        cfg = _cfg(tmp_path)
        ws1 = await create_workspace(cfg, "#42")
        ws2 = await create_workspace(cfg, "#42")
        assert ws2.created_now is False
        assert ws1.path == ws2.path

    @pytest.mark.asyncio
    async def test_replaces_non_directory(self, tmp_path):
        cfg = _cfg(tmp_path)
        root = cfg.workspace_root
        os.makedirs(root, exist_ok=True)
        # Create a file where directory should be
        file_path = os.path.join(root, "_99")
        with open(file_path, "w") as f:
            f.write("not a dir")
        ws = await create_workspace(cfg, "#99")
        assert ws.created_now is True
        assert os.path.isdir(ws.path)


class TestHooks:
    @pytest.mark.asyncio
    async def test_after_create_runs_on_new(self, tmp_path):
        marker = tmp_path / "created.marker"
        cfg = _cfg(tmp_path, hooks={"after_create": f"touch {marker}"})
        ws = await create_workspace(cfg, "#10")
        assert ws.created_now is True
        assert marker.exists()

    @pytest.mark.asyncio
    async def test_after_create_not_on_existing(self, tmp_path):
        marker = tmp_path / "created.marker"
        cfg = _cfg(tmp_path, hooks={"after_create": f"touch {marker}"})
        await create_workspace(cfg, "#10")
        marker.unlink()
        await create_workspace(cfg, "#10")
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_after_create_failure_removes_workspace(self, tmp_path):
        cfg = _cfg(tmp_path, hooks={"after_create": "exit 1"})
        with pytest.raises(HookError):
            await create_workspace(cfg, "#fail")
        ws_path = workspace_path_for(cfg.workspace_root, "#fail")
        assert not os.path.exists(ws_path)

    @pytest.mark.asyncio
    async def test_before_run_hook(self, tmp_path):
        marker = tmp_path / "before.marker"
        await run_hook("before_run", f"touch {marker}", str(tmp_path))
        assert marker.exists()

    @pytest.mark.asyncio
    async def test_before_run_failure(self, tmp_path):
        with pytest.raises(HookError):
            await run_hook("before_run", "exit 42", str(tmp_path))

    @pytest.mark.asyncio
    async def test_hook_timeout(self, tmp_path):
        with pytest.raises(HookTimeoutError):
            await run_hook("test_hook", "sleep 60", str(tmp_path), timeout_ms=100)

    @pytest.mark.asyncio
    async def test_after_run_hook_runs(self, tmp_path):
        marker = tmp_path / "after.marker"
        await run_hook("after_run", f"touch {marker}", str(tmp_path))
        assert marker.exists()


class TestCleanupWorkspace:
    @pytest.mark.asyncio
    async def test_cleanup_removes_directory(self, tmp_path):
        cfg = _cfg(tmp_path)
        ws = await create_workspace(cfg, "#del")
        assert os.path.isdir(ws.path)
        await cleanup_workspace(cfg, "#del")
        assert not os.path.exists(ws.path)

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_is_noop(self, tmp_path):
        cfg = _cfg(tmp_path)
        await cleanup_workspace(cfg, "#ghost")  # should not raise

    @pytest.mark.asyncio
    async def test_before_remove_hook(self, tmp_path):
        marker = tmp_path / "removing.marker"
        cfg = _cfg(tmp_path, hooks={"before_remove": f"touch {marker}"})
        await create_workspace(cfg, "#rm")
        await cleanup_workspace(cfg, "#rm")
        assert marker.exists()


class TestSafetyInvariants:
    @pytest.mark.asyncio
    async def test_workspace_stays_under_root(self, tmp_path):
        cfg = _cfg(tmp_path)
        ws = await create_workspace(cfg, "normal-123")
        assert ws.path.startswith(cfg.workspace_root)

    def test_agent_cwd_matches_workspace(self, tmp_path):
        """Verify the path we'd use as agent cwd matches the workspace path."""
        root = str(tmp_path / "workspaces")
        path = workspace_path_for(root, "#5")
        validate_workspace_path(path, root)

    @pytest.mark.asyncio
    async def test_cleanup_rejects_traversal(self, tmp_path):
        """cleanup_workspace must reject identifiers that escape the root (§9.5)."""
        cfg = _cfg(tmp_path)
        os.makedirs(cfg.workspace_root, exist_ok=True)
        with pytest.raises(InvalidWorkspacePathError):
            await cleanup_workspace(cfg, "..")
