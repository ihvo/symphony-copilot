"""§17.1 — Workflow and Config dynamic reload integration tests.

These exercise the real orchestrator's reload path: mutate the
``WORKFLOW.md`` on disk and verify the service picks up the change.
"""

from __future__ import annotations

import time

import pytest

from symphony.errors import SymphonyError
from symphony.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# §17.1 — Workflow changes detected → re-read / re-apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_change_detected_and_reapplied(
    fake_github, make_workflow, tmp_path, monkeypatch
):
    """Changing WORKFLOW.md on disk updates config without restart."""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    wf = make_workflow(endpoint=fake_github.base_url, poll_ms=5000)

    orch = Orchestrator(wf)
    await orch.start()

    try:
        assert orch.state.poll_interval_ms == 5000

        # Rewrite the file with a different poll interval
        time.sleep(0.05)
        with open(wf, "w") as f:
            f.write(
                f'---\ntracker:\n  kind: github\n  endpoint: "{fake_github.base_url}"\n'
                f"  repo: test/repo\n  api_key: tok\npolling:\n  interval_ms: 15000\n"
                f"copilot:\n  command: echo noop\n---\nV2\n"
            )

        orch._check_workflow_reload()
        assert orch.state.poll_interval_ms == 15000
        assert orch.config.prompt_template == "V2"
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.1 — Invalid reload keeps last known good config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_reload_keeps_last_good(fake_github, make_workflow, tmp_path, monkeypatch):
    """Bad YAML in reloaded WORKFLOW.md does not break the service."""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    wf = make_workflow(endpoint=fake_github.base_url, poll_ms=5000)

    orch = Orchestrator(wf)
    await orch.start()

    try:
        good_config = orch.config
        assert good_config.poll_interval_ms == 5000

        # Write invalid YAML
        time.sleep(0.05)
        with open(wf, "w") as f:
            f.write("---\ntracker:\n  kind: jira\n---\nBroken\n")

        orch._check_workflow_reload()

        # Config should be unchanged
        assert orch.config is good_config
        assert orch.config.tracker_kind == "github"
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.1 — Deleted WORKFLOW.md keeps last known good config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_workflow_keeps_last_good(fake_github, make_workflow, tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    wf = make_workflow(endpoint=fake_github.base_url)

    orch = Orchestrator(wf)
    await orch.start()

    try:
        good = orch.config
        os.unlink(wf)
        orch._check_workflow_reload()
        assert orch.config is good
    finally:
        await orch.stop()


# ---------------------------------------------------------------------------
# §17.1 — Startup fails on missing WORKFLOW.md
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_fails_on_missing_workflow(tmp_path):
    orch = Orchestrator(str(tmp_path / "missing.md"))
    with pytest.raises(SymphonyError, match="Startup validation failed"):
        await orch.start()


# ---------------------------------------------------------------------------
# §17.1 — Startup fails on unsupported tracker kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_fails_on_unsupported_tracker(tmp_path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(
        "---\ntracker:\n  kind: jira\n  repo: x\n  api_key: t\ncopilot:\n  command: x\n---\n"
    )
    orch = Orchestrator(str(wf))
    with pytest.raises(SymphonyError):
        await orch.start()


# ---------------------------------------------------------------------------
# §17.1 — Prompt renders issue + attempt through the full pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_renders_through_full_pipeline(fake_github, make_workflow, tmp_path):
    """Verify prompt rendering does not crash for a real issue."""
    from symphony.config import ServiceConfig
    from symphony.prompt import render_prompt
    from symphony.workflow import load_workflow

    wf_path = make_workflow(
        endpoint=fake_github.base_url,
        prompt="Issue {{ issue.identifier }}: {{ issue.title }}{% if attempt %} retry {{ attempt }}{% endif %}",
    )
    wf = load_workflow(wf_path)
    cfg = ServiceConfig(wf, str(tmp_path))

    fake_github.add_issue(1, title="Bug fix", state="open")
    from symphony.tracker import GitHubTrackerClient

    client = GitHubTrackerClient(fake_github.base_url, "tok", "test/repo")
    try:
        issues = await client.fetch_candidate_issues()
    finally:
        await client.close()

    issue = issues[0]
    rendered = render_prompt(cfg.prompt_template, issue.to_template_dict(), attempt=None)
    assert "#1" in rendered
    assert "Bug fix" in rendered

    rendered2 = render_prompt(cfg.prompt_template, issue.to_template_dict(), attempt=3)
    assert "retry 3" in rendered2
