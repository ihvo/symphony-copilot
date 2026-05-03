"""Tests for workflow loader (SPEC §5, §17.1)."""

from __future__ import annotations

import os

import pytest

from symphony.errors import (
    MissingWorkflowFileError,
    WorkflowFrontMatterNotAMapError,
    WorkflowParseError,
)
from symphony.workflow import load_workflow, resolve_workflow_path


class TestResolveWorkflowPath:
    def test_explicit_path(self, tmp_path):
        p = str(tmp_path / "my_workflow.md")
        assert resolve_workflow_path(p) == os.path.abspath(p)

    def test_default_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert resolve_workflow_path() == os.path.join(str(tmp_path), "WORKFLOW.md")


class TestLoadWorkflow:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(MissingWorkflowFileError):
            load_workflow(str(tmp_path / "nonexistent.md"))

    def test_empty_file(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("")
        wf = load_workflow(str(p))
        assert wf.config == {}
        assert wf.prompt_template == ""

    def test_no_front_matter(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("Just a prompt.\nWith multiple lines.")
        wf = load_workflow(str(p))
        assert wf.config == {}
        assert wf.prompt_template == "Just a prompt.\nWith multiple lines."

    def test_front_matter_and_body(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("---\ntracker:\n  kind: github\n  repo: o/r\n---\nDo the work.")
        wf = load_workflow(str(p))
        assert wf.config["tracker"]["kind"] == "github"
        assert wf.config["tracker"]["repo"] == "o/r"
        assert wf.prompt_template == "Do the work."

    def test_empty_front_matter(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("---\n---\nPrompt body here.")
        wf = load_workflow(str(p))
        assert wf.config == {}
        assert wf.prompt_template == "Prompt body here."

    def test_invalid_yaml(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("---\n: bad: yaml: [unclosed\n---\nbody")
        with pytest.raises(WorkflowParseError):
            load_workflow(str(p))

    def test_front_matter_not_a_map(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("---\n- item1\n- item2\n---\nbody")
        with pytest.raises(WorkflowFrontMatterNotAMapError):
            load_workflow(str(p))

    def test_front_matter_scalar(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("---\njust a string\n---\nbody")
        with pytest.raises(WorkflowFrontMatterNotAMapError):
            load_workflow(str(p))

    def test_prompt_body_trimmed(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text("---\nkey: val\n---\n\n  Prompt  \n\n")
        wf = load_workflow(str(p))
        assert wf.prompt_template == "Prompt"

    def test_complex_front_matter(self, tmp_path):
        p = tmp_path / "WORKFLOW.md"
        p.write_text(
            "---\n"
            "tracker:\n"
            "  kind: github\n"
            "  repo: org/repo\n"
            "  active_states:\n"
            "    - open\n"
            "    - In Progress\n"
            "agent:\n"
            "  max_concurrent_agents: 3\n"
            "  max_concurrent_agents_by_state:\n"
            "    todo: 1\n"
            "    in progress: 2\n"
            "hooks:\n"
            "  after_create: echo created\n"
            "---\n"
            "Work on {{ issue.title }}\n"
        )
        wf = load_workflow(str(p))
        assert wf.config["tracker"]["active_states"] == ["open", "In Progress"]
        assert wf.config["agent"]["max_concurrent_agents"] == 3
        assert wf.config["hooks"]["after_create"] == "echo created"
