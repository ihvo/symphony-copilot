---
tracker:
  kind: github
  repo: ihvo/symphony-copilot
  api_key: $GH_TOKEN
  active_states:
    - open
  terminal_states:
    - closed
polling:
  interval_ms: 30000
workspace:
  root: /tmp/symphony_workspaces
hooks:
  after_create: |
    git clone https://github.com/ihvo/symphony-copilot.git .
    uv sync
  before_run: |
    git checkout main
    git pull --ff-only
    uv sync
agent:
  max_concurrent_agents: 3
  max_turns: 20
copilot:
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
server:
  port: 8111
---
You are an autonomous coding agent working on GitHub issue {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

{% if issue.labels %}Labels: {{ issue.labels | join(', ') }}{% endif %}
{% if attempt %}This is continuation attempt {{ attempt }}. Check the issue comments for your previous progress notes and continue from where you left off.{% endif %}

## Progress reporting

Post a comment on this GitHub issue at each major checkpoint using the `gh` CLI:

```
gh issue comment {{ issue.identifier }} --repo ihvo/symphony-copilot --body "<your update>"
```

You MUST comment at these checkpoints:
1. **Starting** — what you understand the task to be and your planned approach.
2. **Implementation done** — summary of changes made, files touched.
3. **Tests passing** — confirmation that existing and new tests pass.
4. **PR opened** — link to the pull request.

If you hit a blocker or need human input, comment immediately explaining what's wrong and what you need.

Keep comments concise. Use markdown. Include code snippets only when they clarify the update.
