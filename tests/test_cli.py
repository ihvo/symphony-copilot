"""Tests for CLI (SPEC §17.7)."""

from __future__ import annotations

from symphony.cli import _parse_args


class TestParseArgs:
    def test_default_workflow_path(self):
        args = _parse_args([])
        assert args.workflow_path is None

    def test_explicit_workflow_path(self):
        args = _parse_args(["my/WORKFLOW.md"])
        assert args.workflow_path == "my/WORKFLOW.md"

    def test_port_flag(self):
        args = _parse_args(["--port", "8080"])
        assert args.port == 8080

    def test_port_with_workflow(self):
        args = _parse_args(["wf.md", "--port", "3000"])
        assert args.workflow_path == "wf.md"
        assert args.port == 3000

    def test_log_level(self):
        args = _parse_args(["--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"

    def test_no_port_default(self):
        args = _parse_args([])
        assert args.port is None
