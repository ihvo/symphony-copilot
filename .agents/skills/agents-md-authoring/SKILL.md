---
name: agents-md-authoring
description: Write effective AGENTS.md / CLAUDE.md instruction files for coding agents. Covers structure, patterns, and failure modes based on measured data.
---

# AGENTS.md Authoring

A good AGENTS.md is a model upgrade. A bad one is worse than none at all.

## Sweet Spot

100–150 lines in the main file, with a handful of focused reference docs. This delivers 10–15% quality improvements across metrics in mid-size modules (~100 core files). Longer than that, gains reverse.

## Patterns That Work

### Progressive Disclosure

Cover common cases at a high level in the main file. Push details into reference files the agent loads on demand.

- Main file: 100–150 lines max
- Reference files: focused, well-scoped, linked from main file
- No more than 10–15 references per AGENTS.md

### Procedural Workflows

Numbered step-by-step workflows are the strongest pattern. They move agents from "unable to complete" to "correct on first try" — correctness +25%, completeness +20% in measured tests.

```
## Deploying a New Integration
1. Create the config in integrations/configs/
2. Add the wiring file in integrations/wiring/
3. Register in the integration registry at lib/registry.ts
4. Add feature flag in flags/integrations.ts
5. Write integration test in tests/integrations/
6. Update the integration docs in docs/integrations/
```

Keep the happy path in the main file. Put branching cases in a reference file.

### Decision Tables

When the codebase has multiple valid approaches, a decision table resolves ambiguity before the agent writes code. This most directly improves convention adherence (+25% best_practices scores).

```
State Management:
- Server is the only data source → React Query
- Multiple code paths mutate state → Zustand
- Optimistic updates mixed with local state → Zustand
```

### Real Codebase Examples

Short snippets (3–10 lines) from actual production code improve reuse and pattern adherence. Keep examples few and non-duplicative — too many and the agent pattern-matches on the wrong thing.

### Pair Every "Don't" with a "Do"

Warning-only docs consistently underperform paired instructions.

- Bad: `Don't instantiate HTTP clients directly.`
- Good: `Don't instantiate HTTP clients directly. Use the shared apiClient from lib/http with the retry middleware.`

The first makes the agent cautious and exploratory. The pair tells it what to do.

### Scope to Modules, Not the Whole Repo

Module-scoped AGENTS.md files outperform large cross-cutting root-level ones. Isolated submodules with focused agent docs produce the best results.

## The Overexploration Trap

The most common failure mode — context rot. Two causes:

**Too much architecture overview.** The agent reads dozens of docs trying to "understand the architecture," loads 100K+ tokens of context, and output quality drops.

**Excessive warnings without alternatives.** 30–50 "don'ts" without matching "dos" cause the agent to verify its solution against every warning — reading migration scripts, checking API versions, exploring auth middleware — even when none of it is relevant.

**Fix:** If your AGENTS.md is good but the module has 500K tokens of surrounding specs, the specs are the problem. Audit the documentation environment, not just the entry point.

## Discovery Rates

Not all docs get found equally:

| Location | Discovery rate |
|---|---|
| AGENTS.md (auto-loaded) | 100% |
| References linked from AGENTS.md | 90%+ |
| Directory-level README.md | 80%+ |
| Nested READMEs (subdirectories) | ~40% |
| Orphan docs in _docs/ or similar | <10% |

If something needs to be seen, it lives in AGENTS.md or is directly referenced from it.

## Problem → Pattern

| Problem | Pattern |
|---|---|
| Agent doesn't reuse existing code | Real codebase examples (3–10 lines) |
| Agent ignores conventions | Decision tables |
| Agent miswires multi-step features | Procedural workflows |
| Agent hits known gotchas | "Don't" paired with "Do" |
| Context rot / quality degradation | Progressive disclosure; audit surrounding docs |

## Checklist

When creating or reviewing an AGENTS.md:

- [ ] Under 150 lines in the main file?
- [ ] Details pushed to scoped reference files?
- [ ] No more than 10–15 references?
- [ ] Every "don't" has a matching "do"?
- [ ] Workflows are numbered steps, not prose?
- [ ] Ambiguous choices resolved with decision tables?
- [ ] Examples from real production code, 3–10 lines each?
- [ ] No architecture overview that could trigger doc exploration?
- [ ] Surrounding documentation audited for sprawl?
