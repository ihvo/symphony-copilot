"""Shared test fixtures."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory path as a string."""
    return str(tmp_path)


@pytest.fixture
def workflow_file(tmp_path):
    """Create a valid WORKFLOW.md file and return its path."""
    content = """---
tracker:
  kind: github
  repo: test-owner/test-repo
  api_key: $SYMPHONY_TEST_TOKEN
polling:
  interval_ms: 5000
workspace:
  root: "{ws_root}"
agent:
  max_concurrent_agents: 5
  max_turns: 10
copilot:
  command: echo test
---
You are working on issue {{{{ issue.identifier }}}}: {{{{ issue.title }}}}

{% if attempt %}This is retry attempt {{{{ attempt }}}}.{% endif %}
""".format(ws_root=str(tmp_path / "workspaces"))

    path = tmp_path / "WORKFLOW.md"
    path.write_text(content)

    # Set the test token
    os.environ["SYMPHONY_TEST_TOKEN"] = "test-token-123"
    yield str(path)
    os.environ.pop("SYMPHONY_TEST_TOKEN", None)
