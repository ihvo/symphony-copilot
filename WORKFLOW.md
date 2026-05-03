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
agent:
  max_concurrent_agents: 3
  max_turns: 20
copilot:
  turn_timeout_ms: 3600000
  stall_timeout_ms: 300000
server:
  port: 8111
---
You are working on GitHub issue {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

{% if issue.labels %}Labels: {{ issue.labels | join(', ') }}{% endif %}
{% if attempt %}This is continuation attempt {{ attempt }}. Review prior progress and continue.{% endif %}
