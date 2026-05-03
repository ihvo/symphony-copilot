"""§17.7 — CLI and Host Lifecycle integration tests.

Runs the real ``symphony`` CLI as a subprocess and verifies exit codes,
startup failure output, and clean shutdown.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

PYTHON = sys.executable


# ---------------------------------------------------------------------------
# §17.7 — CLI accepts positional workflow path
# ---------------------------------------------------------------------------


def test_cli_nonexistent_explicit_path_exits_nonzero(tmp_path):
    """CLI exits nonzero for a nonexistent explicit workflow path."""
    result = subprocess.run(
        [PYTHON, "-m", "symphony", str(tmp_path / "missing.md")],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_cli_missing_default_workflow_exits_nonzero(tmp_path):
    """CLI exits nonzero when no ./WORKFLOW.md and no arg given."""
    result = subprocess.run(
        [PYTHON, "-m", "symphony"],
        capture_output=True, text=True, timeout=10,
        cwd=str(tmp_path),
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# §17.7 — Startup failure surfaces cleanly
# ---------------------------------------------------------------------------


def test_cli_surfaces_startup_failure(tmp_path):
    """Startup failure message appears in stderr."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("---\ntracker:\n  kind: unsupported\n---\nP")
    result = subprocess.run(
        [PYTHON, "-m", "symphony", str(wf)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "startup" in result.stderr.lower() or "Unsupported" in result.stderr


def test_cli_surfaces_missing_api_key(tmp_path):
    """Missing API key error appears in stderr."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\n")
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
    result = subprocess.run(
        [PYTHON, "-m", "symphony", str(wf)],
        capture_output=True, text=True, timeout=10,
        env=env,
    )
    assert result.returncode != 0
    assert "api_key" in result.stderr


# ---------------------------------------------------------------------------
# §17.7 — CLI exits with success on normal shutdown
# ---------------------------------------------------------------------------


def test_cli_clean_exit_on_sigint(tmp_path):
    """Service shuts down cleanly on SIGINT with exit code 0."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(
        "---\ntracker:\n  kind: github\n  repo: o/r\n  api_key: tok\n"
        "  endpoint: http://127.0.0.1:1\n"
        "polling:\n  interval_ms: 60000\n"
        "copilot:\n  command: echo noop\n---\nP"
    )
    proc = subprocess.Popen(
        [PYTHON, "-m", "symphony", str(wf)],
        stderr=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    try:
        # Give the service time to start
        time.sleep(1.5)
        assert proc.poll() is None, "process exited prematurely"
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# §17.7 — CLI --port flag
# ---------------------------------------------------------------------------


def test_cli_port_flag_starts_server(tmp_path):
    """--port flag starts the HTTP server alongside the orchestrator."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(
        "---\ntracker:\n  kind: github\n  repo: o/r\n  api_key: tok\n"
        "  endpoint: http://127.0.0.1:1\n"
        "polling:\n  interval_ms: 60000\n"
        "copilot:\n  command: echo noop\n---\nP"
    )
    proc = subprocess.Popen(
        [PYTHON, "-m", "symphony", str(wf), "--port", "0"],
        stderr=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(2)
        assert proc.poll() is None, "process exited prematurely"
        # Check stderr for server_listening log
        proc.send_signal(signal.SIGINT)
        _, stderr = proc.communicate(timeout=5)
        assert "server_listening" in stderr or "http_server_started" in stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
