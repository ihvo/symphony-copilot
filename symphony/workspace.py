"""Workspace manager – per-issue workspace lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil

from symphony.config import ServiceConfig
from symphony.errors import (
    HookError,
    HookTimeoutError,
    InvalidWorkspacePathError,
    WorkspaceError,
)
from symphony.models import Workspace

logger = logging.getLogger("symphony.workspace")

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._\-]")


def sanitize_identifier(identifier: str) -> str:
    """Replace characters outside ``[A-Za-z0-9._-]`` with ``_``."""
    return _SAFE_CHARS_RE.sub("_", identifier)


def workspace_path_for(root: str, identifier: str) -> str:
    """Compute the absolute workspace path for an issue identifier."""
    key = sanitize_identifier(identifier)
    path = os.path.normpath(os.path.join(root, key))
    return path


def validate_workspace_path(path: str, root: str) -> None:
    """Ensure *path* is inside *root* (safety invariant 2)."""
    abs_path = os.path.abspath(path)
    abs_root = os.path.abspath(root)
    # Must be a proper child, not equal to root
    if not abs_path.startswith(abs_root + os.sep):
        raise InvalidWorkspacePathError(abs_path, abs_root)


async def run_hook(
    hook_name: str,
    script: str | None,
    cwd: str,
    timeout_ms: int = 60000,
) -> None:
    """Execute a shell hook script in *cwd* with timeout.

    Raises :class:`HookError` or :class:`HookTimeoutError` on failure.
    """
    if not script:
        return

    logger.info("hook_start hook=%s cwd=%s", hook_name, cwd)
    try:
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable="/bin/bash",
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HookTimeoutError(hook_name, timeout_ms)

        if proc.returncode != 0:
            detail = (stderr or stdout or b"").decode(errors="replace")[:500]
            raise HookError(hook_name, f"exit code {proc.returncode}: {detail}")

    except (HookError, HookTimeoutError):
        raise
    except Exception as exc:
        raise HookError(hook_name, str(exc)) from exc


async def create_workspace(
    config: ServiceConfig,
    identifier: str,
) -> Workspace:
    """Create or reuse a workspace directory for *identifier*.

    Returns a :class:`Workspace` with ``created_now=True`` only if the
    directory was created during this call.
    """
    root = config.workspace_root
    key = sanitize_identifier(identifier)
    path = os.path.normpath(os.path.join(root, key))

    validate_workspace_path(path, root)

    created_now = False
    if os.path.exists(path):
        if not os.path.isdir(path):
            # Safety: remove non-directory and recreate
            os.remove(path)
            os.makedirs(path, exist_ok=True)
            created_now = True
    else:
        os.makedirs(path, exist_ok=True)
        created_now = True

    ws = Workspace(path=path, workspace_key=key, created_now=created_now)

    if created_now and config.hook_after_create:
        try:
            await run_hook("after_create", config.hook_after_create, path, config.hook_timeout_ms)
        except Exception:
            # after_create failure is fatal to workspace creation – remove partial workspace
            shutil.rmtree(path, ignore_errors=True)
            raise

    return ws


async def cleanup_workspace(
    config: ServiceConfig,
    identifier: str,
) -> None:
    """Remove workspace directory for *identifier*, running before_remove hook first."""
    key = sanitize_identifier(identifier)
    path = os.path.normpath(os.path.join(config.workspace_root, key))

    validate_workspace_path(path, config.workspace_root)

    if not os.path.isdir(path):
        return

    if config.hook_before_remove:
        try:
            await run_hook("before_remove", config.hook_before_remove, path, config.hook_timeout_ms)
        except Exception:
            logger.warning("before_remove hook failed for %s, proceeding with cleanup", identifier)

    try:
        shutil.rmtree(path)
        logger.info("workspace_removed identifier=%s path=%s", identifier, path)
    except OSError as exc:
        logger.warning("workspace_remove_failed identifier=%s error=%s", identifier, exc)
