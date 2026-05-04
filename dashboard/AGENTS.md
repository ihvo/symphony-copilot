# Dashboard AGENTS.md

Next.js 15 static-export frontend for the Symphony orchestrator dashboard. Served by FastAPI from `dashboard/out/`.

## Quick Reference

```bash
npm install          # install dependencies
npm run dev          # dev server on :3000 (proxies API to :8080)
npm run build        # static export → out/
npm test             # 22 Vitest + RTL component tests
```

## Stack

- **Framework:** Next.js 15 (App Router, `output: 'export'`)
- **React:** 19.1
- **Data fetching:** SWR 2.3 (10s polling, pause-when-hidden)
- **Styling:** Tailwind CSS v4 (`@theme` in globals.css)
- **Fonts:** Geist Sans + Geist Mono via `next/font/google`
- **Testing:** Vitest + React Testing Library
- **TypeScript:** strict mode

## Project Layout

```
src/
├── app/
│   ├── globals.css       → Tailwind @theme tokens (zinc/emerald palette)
│   ├── layout.tsx        → Root layout (fonts, <main> landmark)
│   ├── page.tsx          → Dashboard page (composes all components)
│   ├── loading.tsx       → App Router loading skeleton
│   └── error.tsx         → Error boundary
├── components/
│   ├── metrics-grid.tsx  → 4-col asymmetric metrics (2fr/1fr/1fr/1fr)
│   ├── running-table.tsx → Active sessions table
│   ├── retry-table.tsx   → Retry queue table
│   ├── status-badge.tsx  → Colored badge variants
│   └── connection-status.tsx → Live relative time + stale warning
├── hooks/
│   ├── use-state-polling.ts  → SWR wrapper for /api/v1/state
│   └── use-relative-time.ts  → Relative time display (auto-updating)
├── lib/
│   ├── types.ts          → TypeScript API response types
│   └── api.ts            → Typed fetch wrapper
└── __tests__/
    ├── setup.ts          → RTL jest-dom matchers + cleanup
    ├── fixtures.ts       → MOCK_STATE / EMPTY_STATE test data
    └── *.test.tsx        → Component and hook tests
```

## Design System

| Token | Value |
|-------|-------|
| Background | zinc-950 |
| Surface | zinc-900 |
| Border | zinc-800 |
| Text primary | zinc-50 |
| Text secondary | zinc-400 |
| Accent | emerald-600 |
| Font sans | Geist Sans |
| Font mono | Geist Mono |

No emoji. No Inter. No purple/blue. No pure black (#000000).

## Conventions

### Components

- One component per file in `src/components/`
- Named exports (not default exports)
- Props typed inline or with a `Props` suffix type
- All components are client components (`"use client"` directive)
- Responsive breakpoint: `md:` (768px) for grid transitions

### Hooks

- Custom hooks in `src/hooks/`
- Prefix with `use`
- All tests mock `useStatePolling` — components receive data via this hook

### Testing

- Tests in `src/__tests__/` (colocated test directory)
- Every component has a test file
- Mock `useStatePolling` via `vi.mock("@/hooks/use-state-polling")`
- Use `MOCK_STATE` and `EMPTY_STATE` from `fixtures.ts`
- Run: `npm test` (watch) or `npm test -- --run` (CI)

### Styling

- Tailwind utility classes only (no CSS modules, no styled-components)
- Design tokens defined in `globals.css` under `@theme`
- Tabular data uses `font-mono` for alignment
- Status colors: emerald (active), amber (retry), zinc (default)

## API Contract

The dashboard fetches from a single endpoint:

```
GET /api/v1/state → {
  generated_at: string (ISO),
  counts: { active, retrying, completed, total },
  running: [{ issue_id, issue_identifier, started_at, ... }],
  retrying: [{ issue_id, issue_identifier, next_attempt_at, ... }],
  copilot_totals: { total_tokens, prompt_tokens, completion_tokens },
  rate_limits: { ... }
}
```

Types are defined in `src/lib/types.ts`. Keep in sync with `symphony/server.py` `_build_state_payload()`.

## Critical Invariants

- **Don't** import from `symphony/` Python code. The dashboard is a standalone static app.
- **Don't** use `getServerSideProps` or server actions — static export only.
- **Don't** add runtime environment variables — all config is build-time or hardcoded API path.
- **Don't** use `next/image` with remote URLs — static export doesn't support image optimization.
- **Do** keep `output: 'export'` in `next.config.ts` — FastAPI serves the built files.
- **Do** guard relative-time formatting against NaN and negative values.
- **Do** run `npm test -- --run` before committing dashboard changes.
