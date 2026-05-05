# Dashboard Frontend Redesign

**Status:** Proposed  
**Author:** Ihar / Copilot  
**Package:** symphony

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 0.1 | 2026-05-04 | Initial design |
| 0.2 | 2026-05-04 | Incorporated single-model review feedback (5 HIGH issues resolved) |
| 0.3 | 2026-05-04 | Revised architecture: Next.js static export served by FastAPI |

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Current Architecture](#2-current-architecture)
3. [Requirements](#3-requirements)
4. [Options Evaluation](#4-options-evaluation)
5. [Recommended Approach](#5-recommended-approach)
6. [Component Design](#6-component-design)
7. [Migration Plan](#7-migration-plan)
8. [Test Strategy](#8-test-strategy)
9. [Risk Assessment](#9-risk-assessment)
10. [Decision Records](#10-decision-records)

---

## 1. Problem Statement

The Symphony Dashboard (`GET /`) is the primary human interface for observing agent orchestration state. The current implementation suffers from:

- **Visual quality:** Generic system-ui styling with basic table borders and flexbox stat cards. Looks like a 2015-era admin panel.
- **No interactivity:** Full-page refresh every 10 seconds via `<meta http-equiv="refresh">`. This causes visible flicker, resets scroll position, and provides no transition context for state changes.
- **Accessibility issues:** Emoji in title (`🎵`), low contrast ratios on muted text, no semantic landmarks.
- **Information density:** Running sessions table shows 8 columns with no prioritization—issue identifier and token count receive equal visual weight.
- **No drill-down:** The `/api/v1/{identifier}` endpoint exists but the dashboard doesn't link to per-issue detail views.
- **Mobile unusable:** Fixed table layout overflows on narrow viewports with no responsive strategy.

**Impact of not fixing:** The dashboard is the only way to monitor Symphony in real-time. Poor UX means operators miss important signals (retries backing up, token burn rate spiking, sessions stuck).

---

## 2. Current Architecture

```
┌─────────────────────────────────────────────────────┐
│ symphony/server.py                                  │
├─────────────────────────────────────────────────────┤
│ SymphonyServer                                      │
│   GET /            → _render_dashboard(snapshot)    │
│   GET /api/v1/state → JSON snapshot                 │
│   GET /api/v1/{id}  → JSON issue detail             │
│   POST /api/v1/refresh → trigger poll               │
│   GET /favicon.ico  → inline SVG                    │
├─────────────────────────────────────────────────────┤
│ _render_dashboard()                                 │
│   - 330-line Python f-string                        │
│   - Inline CSS (no external stylesheet)             │
│   - No client-side JavaScript                       │
│   - Auto-refresh: <meta http-equiv="refresh" ...>   │
│   - Data: running[], retrying[], counts, totals     │
└─────────────────────────────────────────────────────┘
```

### Data available via API

| Endpoint | Fields |
|----------|--------|
| `/api/v1/state` | `generated_at`, `counts{running,retrying}`, `running[]{issue_identifier, state, session_id, turn_count, last_event, last_message, started_at, last_event_at, tokens{input,output,total}}`, `retrying[]{issue_identifier, attempt, due_at, error}`, `copilot_totals{input_tokens, output_tokens, total_tokens, seconds_running}`, `rate_limits` |
| `/api/v1/{id}` | `issue_identifier`, `issue_id`, `status`, `workspace{path}`, `running{...}` or `retry{...}`, `last_error` |

### Constraints to preserve


- Dashboard HTML must contain "Symphony Dashboard" (test assertion)
- Must include `rel="icon"` and `data:image/svg+xml,` (test assertion)
- Issue identifiers must appear verbatim in output (test assertion)
- FastAPI response must be `HTMLResponse`

---

## 3. Requirements

### Must-have

| ID | Requirement |
|----|-------------|
| R1 | Premium visual design: Geist-family typography, zinc/slate palette, single accent color, asymmetric grid layout |
| R2 | Responsive layout: graceful collapse to single-column on mobile (`< 768px`) |
| R3 | Smooth data refresh without full-page reload (no scroll reset, no flash) |
| R4 | Visual hierarchy: primary metrics (active sessions, token burn) are immediately scannable |
| R5 | Clean data tables with minimal borders, monospace numbers, and truncated overflow |
| R6 | Accessible: proper semantic HTML, ARIA landmarks, contrast ratios AA compliant |
| R7 | Empty/loading/error states designed with clear visual treatment |
| R8 | Next.js static export served by FastAPI — single deployment unit |
| R9 | All existing API test assertions continue to pass |
| R10 | Stale/failure indication: visible "last updated" timestamp and connection-lost warning |
| R11 | XSS safety: React's default escaping + no `dangerouslySetInnerHTML` for snapshot data |
| R12 | Streaming-ready architecture: API layer supports SSE/WebSocket for future real-time event feeds |

### Nice-to-have

| ID | Requirement |
|----|-------------|
| N1 | Per-issue detail view (click issue identifier → inline expandable panel populated from `/api/v1/{id}`) |
| N2 | Relative time display ("2m ago" instead of ISO timestamp) |
| N3 | Token burn rate indicator (tokens/minute derived from totals + runtime) |
| N4 | Visual state transitions (fade-in new rows, highlight changes) with `prefers-reduced-motion` respect |
| N5 | Dark mode support via `prefers-color-scheme` |
| N6 | Pause polling when tab is hidden (`document.hidden`) to save resources |
| N7 | Live streaming panel: real-time agent event log via SSE |
| N8 | Session timeline visualization (turns, events, token usage over time) |

### Constraints

| ID | Constraint |
|----|------------|
| C1 | Dashboard must be buildable offline and served as static files (no Next.js server runtime in production) |
| C2 | FastAPI remains the single HTTP process — dashboard is mounted as static assets |
| C3 | No emoji anywhere in the dashboard output |
| C4 | API endpoints (`/api/v1/*`) remain unchanged — frontend is a pure consumer |

---

## 4. Options Evaluation

### Option A: Next.js Static Export + FastAPI Static Mount (Recommended)

Build a Next.js App Router application in `dashboard/`. Use `output: 'export'` to produce static HTML/JS/CSS in `dashboard/out/`. FastAPI mounts this directory at `/` and serves the API at `/api/v1/*`.

| Dimension | Assessment |
|-----------|-----------|
| Complexity | Moderate — standard Next.js project with well-known patterns |
| Refresh | Client-side SWR/React Query with automatic revalidation |
| Interactivity | Full React component tree — streaming panels, charts, expandable details |
| Maintenance | Excellent — typed components, hot-reload dev, standard testing tools |
| Performance | Optimized bundle splitting, route-level code splitting, static pre-render |
| Streaming | EventSource/WebSocket clients trivial in React; architecture ready |

**Verdict:** Best balance of developer experience, extensibility (streaming, charts, detail views), and deployment simplicity (single FastAPI process).

### Option B: Vite + React (Lightweight Alternative)

Same static-export approach but with Vite instead of Next.js. No file-system routing, no App Router.

| Dimension | Assessment |
|-----------|-----------|
| Complexity | Low — minimal config, fast builds |
| Routing | Manual (react-router) |
| Future features | Requires more manual wiring for layouts, loading states |
| Bundle size | Slightly smaller (no Next.js runtime) |

**Verdict:** Viable but less ergonomic for a growing dashboard with multiple views. Next.js gives us file-system routing, layouts, loading/error boundaries out of the box.

### Option C: Enhanced Server-Rendered HTML (Previous Approach)

Keep `_render_dashboard()` as a Python f-string with inline vanilla JS.

| Dimension | Assessment |
|-----------|-----------|
| Complexity | Low for initial work, high for streaming/interactivity |
| Streaming | Very hard — would need custom WebSocket client in vanilla JS |
| Maintenance | Poor at scale — JS in Python f-strings, no type checking, no component reuse |
| Testing | No React Testing Library, no component tests |

**Verdict:** Doesn't scale to streaming, detail panels, or charts. Ceiling reached quickly.

### Comparison Matrix

| Criterion | Weight | Option A (Next.js) | Option B (Vite) | Option C (Inline) |
|-----------|--------|-------------------|-----------------|-------------------|
| Visual quality | 20% | 9 | 9 | 7 |
| Streaming support | 20% | 10 | 9 | 3 |
| Developer experience | 20% | 10 | 8 | 4 |
| Simplicity of deploy | 15% | 8 | 9 | 10 |
| Extensibility | 15% | 10 | 7 | 3 |
| Testing | 10% | 9 | 8 | 5 |
| **Weighted Score** | | **9.4** | **8.4** | **5.1** |

**Recommendation: Option A** — Next.js static export provides the best foundation for streaming, detail views, and future dashboard growth while keeping deployment simple (just static files).

---

## 5. Recommended Approach

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser                                                         │
├─────────────────────────────────────────────────────────────────┤
│  Next.js Static Export (React App Router)                        │
│    ├── /              → Dashboard overview (metrics + tables)    │
│    ├── /issues/[id]   → Issue detail view (timeline, tokens)    │
│    └── /stream        → Live event stream panel (future)        │
│                                                                  │
│  Data fetching: SWR with 10s revalidation interval              │
│  Future streaming: EventSource → /api/v1/stream (SSE)           │
└───────────────────────────┬─────────────────────────────────────┘
                            │ fetch / EventSource
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI (symphony/server.py)                                    │
│    /               → StaticFiles(dashboard/out)                  │
│    /api/v1/state   → JSON snapshot                               │
│    /api/v1/{id}    → JSON issue detail                           │
│    /api/v1/refresh → trigger poll                                │
│    /api/v1/stream  → SSE stream (future)                        │
└─────────────────────────────────────────────────────────────────┘
```

### Repository Layout

```
symphony-copilot/
├── symphony/
│   ├── server.py          (mounts dashboard/out as static, serves API)
│   └── ...
├── dashboard/             (Next.js project)
│   ├── package.json
│   ├── next.config.ts     (output: 'export', basePath if needed)
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx       (root layout, fonts, providers)
│   │   │   ├── page.tsx         (dashboard overview)
│   │   │   ├── loading.tsx      (skeleton loader)
│   │   │   ├── error.tsx        (error boundary)
│   │   │   └── issues/
│   │   │       └── [id]/
│   │   │           └── page.tsx (issue detail)
│   │   ├── components/
│   │   │   ├── metrics-grid.tsx
│   │   │   ├── running-table.tsx
│   │   │   ├── retry-table.tsx
│   │   │   ├── status-badge.tsx
│   │   │   └── connection-status.tsx
│   │   ├── hooks/
│   │   │   ├── use-state-polling.ts  (SWR wrapper)
│   │   │   └── use-relative-time.ts
│   │   └── lib/
│   │       ├── api.ts           (typed API client)
│   │       └── types.ts         (API response types)
│   ├── out/                     (build output — gitignored or committed)
│   └── ...
├── pyproject.toml
└── ...
```

### Key Design Decisions

1. **Static export (`output: 'export'`):** No Next.js server at runtime. Build produces pure HTML/JS/CSS files.
2. **FastAPI serves everything:** `StaticFiles` mount at `/` for the dashboard, API routes at `/api/v1/*`.
3. **SWR for data fetching:** Automatic revalidation, stale-while-revalidate, built-in error/loading states.
4. **TypeScript throughout:** API response types derived from the Python models, compile-time safety.
5. **Streaming-ready:** Architecture supports adding SSE endpoints that React components consume via `EventSource`.
6. **Tailwind CSS v4:** Utility-first styling matching the design system (zinc/emerald palette, Geist font).

---

## 6. Component Design

### 6.1 Visual Design System (Tailwind Config)

```typescript
// tailwind.config.ts
import type { Config } from 'tailwindcss'

export default {
  theme: {
    extend: {
      fontFamily: {
        sans: ['Geist', 'system-ui', 'sans-serif'],
        mono: ['Geist Mono', 'JetBrains Mono', 'monospace'],
      },
      colors: {
        surface: '#ffffff',
        border: '#e4e4e7',        // zinc-200
        'border-subtle': '#f4f4f5', // zinc-100
        accent: '#059669',         // emerald-600
        'accent-subtle': '#ecfdf5', // emerald-50
        warning: '#d97706',        // amber-600
        'warning-subtle': '#fffbeb', // amber-50
      },
      boxShadow: {
        'card': '0 1px 2px rgba(0,0,0,0.04)',
        'card-hover': '0 4px 12px -2px rgba(0,0,0,0.06)',
      },
      borderRadius: {
        'card': '12px',
      },
    },
  },
} satisfies Config
```

### 6.2 Root Layout

```tsx
// src/app/layout.tsx
import { Geist, Geist_Mono } from 'next/font/google'

const geist = Geist({ subsets: ['latin'], variable: '--font-sans' })
const geistMono = Geist_Mono({ subsets: ['latin'], variable: '--font-mono' })

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geist.variable} ${geistMono.variable}`}>
      <body className="bg-zinc-50 text-zinc-950 font-sans min-h-[100dvh]">
        <div className="max-w-[1400px] mx-auto px-6 py-10 md:px-10">
          {children}
        </div>
      </body>
    </html>
  )
}
```

### 6.3 Data Fetching (SWR Hook)

```typescript
// src/hooks/use-state-polling.ts
import useSWR from 'swr'
import type { SystemState } from '@/lib/types'

const fetcher = (url: string) => fetch(url).then(r => r.json())

export function useStatePolling() {
  const { data, error, isLoading, mutate } = useSWR<SystemState>(
    '/api/v1/state',
    fetcher,
    {
      refreshInterval: 10_000,
      revalidateOnFocus: true,
      dedupingInterval: 5_000,
      // Pause when tab hidden
      refreshWhenHidden: false,
    }
  )

  return {
    state: data,
    isLoading,
    isError: !!error,
    isStale: !data && !isLoading,
    refresh: mutate,
  }
}
```

### 6.4 API Types (Derived from Python Models)

```typescript
// src/lib/types.ts
export interface TokenUsage {
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

export interface RunningSession {
  issue_id: string
  issue_identifier: string
  state: string
  session_id: string
  turn_count: number
  last_event: string
  last_message: string
  started_at: string
  last_event_at: string
  tokens: TokenUsage
}

export interface RetryEntry {
  issue_id: string
  issue_identifier: string
  attempt: number
  due_at: string
  error: string
}

export interface CopilotTotals {
  input_tokens: number
  output_tokens: number
  total_tokens: number
  seconds_running: number
}

export interface SystemState {
  generated_at: string
  counts: { running: number; retrying: number }
  running: RunningSession[]
  retrying: RetryEntry[]
  copilot_totals: CopilotTotals
  rate_limits: Record<string, unknown> | null
}
```

### 6.5 Metrics Grid Component

```tsx
// src/components/metrics-grid.tsx
'use client'

import { useStatePolling } from '@/hooks/use-state-polling'

export function MetricsGrid() {
  const { state, isLoading } = useStatePolling()

  if (isLoading) return <MetricsSkeleton />

  const runtime = formatRuntime(state.copilot_totals.seconds_running)
  const burnRate = state.copilot_totals.seconds_running > 0
    ? Math.round(state.copilot_totals.total_tokens / (state.copilot_totals.seconds_running / 60))
    : 0

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-[2fr_1fr_1fr_1fr] gap-3 mb-8">
      <MetricCard value={state.counts.running} label="Active Sessions" accent />
      <MetricCard value={state.counts.retrying} label="Retrying" />
      <MetricCard value={state.copilot_totals.total_tokens.toLocaleString()} label="Tokens Used" />
      <MetricCard value={runtime} label="Runtime" />
    </div>
  )
}
```

### 6.6 Running Table Component

```tsx
// src/components/running-table.tsx
'use client'

import { useStatePolling } from '@/hooks/use-state-polling'
import { StatusBadge } from './status-badge'
import { useRelativeTime } from '@/hooks/use-relative-time'

export function RunningTable() {
  const { state, isLoading } = useStatePolling()

  if (isLoading) return <TableSkeleton rows={3} cols={7} />
  if (state.running.length === 0) return <EmptyState icon="circle" message="No active sessions" />

  return (
    <div className="bg-surface border border-border rounded-card shadow-card overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-zinc-50/50">
            <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">Issue</th>
            {/* ... */}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100">
          {state.running.map(session => (
            <tr key={session.session_id} className="hover:bg-zinc-50/50 transition-colors">
              <td className="px-4 py-3 font-semibold text-accent">{session.issue_identifier}</td>
              <td><StatusBadge status={session.state} /></td>
              <td className="font-mono text-xs text-zinc-500">{session.session_id}</td>
              {/* ... */}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

### 6.7 Connection Status Component

```tsx
// src/components/connection-status.tsx
'use client'

import { useStatePolling } from '@/hooks/use-state-polling'

export function ConnectionStatus() {
  const { state, isError, isStale } = useStatePolling()

  if (isError) {
    return (
      <span role="status" aria-live="polite" className="text-xs font-mono text-amber-600">
        Connection lost — retrying...
      </span>
    )
  }

  if (state?.generated_at) {
    return (
      <span className="text-xs font-mono text-zinc-400">
        {formatRelativeTime(state.generated_at)}
      </span>
    )
  }

  return null
}
```

### 6.8 Future: Streaming Event Panel

```tsx
// src/components/event-stream.tsx (Phase 3 — SSE)
'use client'

import { useEffect, useState } from 'react'

interface AgentEvent {
  timestamp: string
  issue_identifier: string
  event_type: string
  message: string
}

export function EventStream() {
  const [events, setEvents] = useState<AgentEvent[]>([])

  useEffect(() => {
    const source = new EventSource('/api/v1/stream')
    source.onmessage = (e) => {
      const event = JSON.parse(e.data) as AgentEvent
      setEvents(prev => [event, ...prev].slice(0, 100)) // keep last 100
    }
    return () => source.close()
  }, [])

  return (
    <div className="font-mono text-xs space-y-1 max-h-96 overflow-y-auto">
      {events.map((e, i) => (
        <div key={i} className="flex gap-3 py-1 border-b border-zinc-100">
          <span className="text-zinc-400 shrink-0">{formatTime(e.timestamp)}</span>
          <span className="text-accent font-semibold">{e.issue_identifier}</span>
          <span className="text-zinc-600 truncate">{e.message}</span>
        </div>
      ))}
    </div>
  )
}
```

### 6.9 Responsive Behavior

| Breakpoint | Layout |
|------------|--------|
| `>= 1400px` | Full layout, all columns visible |
| `768-1399px` | Metrics: 2fr 1fr 1fr 1fr, tables scroll horizontally |
| `< 768px` | Metrics: 2x2 grid, tables scroll in wrapper, padding reduced |

---

## 7. Migration Plan

### Phase 1: CSS Redesign (Complete)

- Replace old inline styles with premium design system
- Implement asymmetric metrics grid, clean tables, proper typography
- Remove emoji from title
- Add responsive breakpoints
- **Gate:** All existing tests pass, dashboard renders correctly

### Phase 2: Next.js Project Setup

- Initialize `dashboard/` with `create-next-app` (App Router, TypeScript, Tailwind)
- Configure `next.config.ts` with `output: 'export'`
- Set up Geist font via `next/font/google`
- Configure Tailwind with the design system tokens
- Add SWR dependency for data fetching
- Build static export to `dashboard/out/`
- Update `server.py` to mount `StaticFiles` at `/` from build output
- When `dashboard/out/` absent, serve a minimal placeholder with build instructions
- **Gate:** `npm run build` succeeds, FastAPI serves static files, API still works

### Phase 3: Core Dashboard Components

- Implement `MetricsGrid`, `RunningTable`, `RetryTable` components
- Implement `ConnectionStatus` component with stale-data indicator
- Add `useStatePolling` hook with SWR revalidation
- Add `useRelativeTime` hook for timestamp formatting
- Add loading skeletons and error boundaries
- Add empty state designs
- **Gate:** Dashboard is fully functional, all data from `/api/v1/state` displayed, responsive

### Phase 4: Detail Views & Interactivity

- Add `/issues/[id]` route consuming `/api/v1/{identifier}`
- Issue timeline view (turns, events, token usage)
- Clickable issue identifiers in tables linking to detail view
- Token burn rate calculation and display
- Dark mode via Tailwind's `dark:` variants
- **Gate:** Navigation works, detail data loads, dark mode toggles cleanly

### Phase 5: Streaming (Future)

- Add SSE endpoint `/api/v1/stream` to FastAPI (emit from orchestrator event loop)
- Implement `EventStream` component with `EventSource`
- Live event log panel with auto-scroll and filters
- Session timeline with real-time updates
- **Gate:** Events appear in real-time, no memory leaks, clean disconnect handling

### Effort Estimate

| Phase | Effort |
|-------|--------|
| Phase 1 | Done |
| Phase 2 | 0.5 days |
| Phase 3 | 1-2 days |
| Phase 4 | 1-2 days |
| Phase 5 | 2-3 days |

---

## 8. Test Strategy

### Existing tests (must continue passing)

- `test_dashboard_endpoint`: Asserts "Symphony Dashboard", "#1", `rel="icon"`, `data:image/svg+xml,` present in HTML
- `test_dashboard_html` (integration): Asserts "Symphony Dashboard" in response from real server
- XSS safety: HTML escaping of all user-supplied data via `_esc()` / `html.escape()`
- Note: These tests validate the fallback server-rendered HTML (used when `dashboard/out/` is absent)

### New tests — Python API layer

| Test | Purpose |
|------|---------|
| `test_static_files_served` | Assert FastAPI serves `dashboard/out/index.html` at `/` when build exists |
| `test_fallback_placeholder` | Assert placeholder HTML with build instructions served when `dashboard/out/` absent |
| `test_api_cors_headers` | Assert proper CORS for local Next.js dev server (`localhost:3000`) |

### New tests — Next.js (Vitest + React Testing Library)

| Test | Purpose |
|------|---------|
| `metrics-grid.test.tsx` | Renders metrics from mocked SWR data, shows loading skeleton |
| `running-table.test.tsx` | Renders running sessions, shows empty state when array is empty |
| `retry-table.test.tsx` | Renders retry entries with formatted timestamps |
| `connection-status.test.tsx` | Shows "connection lost" on error state |
| `use-state-polling.test.ts` | SWR hook revalidates on interval, pauses when hidden |
| `types.test.ts` | Validate TypeScript types match actual API response shapes |
| `accessibility.test.tsx` | Assert ARIA roles, landmarks, and screen reader text |

### E2E (Playwright — Phase 4+)

| Test | Purpose |
|------|---------|
| `dashboard-loads.spec.ts` | Page loads, metrics visible, no console errors |
| `issue-navigation.spec.ts` | Click issue → detail view loads |
| `dark-mode.spec.ts` | Toggle prefers-color-scheme, verify theme switch |

### Test approach

- Python tests: existing `httpx.ASGITransport` pattern
- React tests: Vitest + React Testing Library (fast, no browser needed)
- E2E: Playwright against built static export served locally

---

## 9. Risk Assessment

### Risks of implementing

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Inline JS has a bug causing stale display | Low | Medium | Server-rendered HTML is always correct on initial load; JS errors fail open |
| Google Fonts CDN unavailable | Low | Low | Fallback to `system-ui` in font stack |
| Large snapshot payload slows fetch | Low | Low | Payload is tiny (<5KB even with 10 running sessions) |
| CSS changes break in older browsers | Low | Low | Only CSS3 features with wide support |

### Risks of NOT implementing

| Risk | Likelihood | Impact |
|------|-----------|--------|
| Operators miss critical state changes due to 10s full-page flash | Medium | High |
| Mobile operators cannot use dashboard in incident response | High | Medium |
| Poor visual quality reduces confidence in tool reliability | Medium | Low |

---

## 10. Decision Records

### ADR-1: Next.js with static export

**Context:** The dashboard needs to grow into a full operational interface with streaming, detail views, and interactive panels. Inline HTML in Python f-strings doesn't scale.

**Decision:** Use Next.js App Router with `output: 'export'` producing static HTML/JS/CSS. FastAPI serves the build output via `StaticFiles`.

**Rationale:** Next.js provides file-system routing, layout nesting, loading/error boundaries, `next/font` for optimized font loading, and a mature ecosystem. Static export means no Node.js server at runtime — FastAPI remains the single process.

**Consequences:** Adds a `dashboard/` directory with `package.json` and a build step. Developers need Node.js installed for dashboard work. The build output (`dashboard/out/`) can be committed or built in CI.

### ADR-2: SWR for data fetching

**Context:** The dashboard needs automatic polling with stale-while-revalidate semantics, deduplication, and pause-when-hidden behavior.

**Decision:** Use SWR (by Vercel) for all data fetching from `/api/v1/*`.

**Rationale:** SWR provides all needed behaviors out of the box: `refreshInterval`, `refreshWhenHidden: false`, `dedupingInterval`, error retry, and cache. It's 4KB gzipped and requires no global state setup (unlike Redux/Zustand).

**Consequences:** One npm dependency beyond React/Next.js. Lightweight and well-maintained.

### ADR-3: Emerald accent, not blue/purple

**Context:** Default AI-generated dashboards trend toward blue/purple "tech" palettes.

**Decision:** Zinc neutral base with emerald-600 (`#059669`) as the sole accent.

**Rationale:** Emerald provides clear "active/healthy" signaling for running sessions. Warm amber for warnings. Avoids the generic "AI dashboard" look.

**Consequences:** Consistent with the design-taste-frontend skill directives. Single accent keeps visual noise low.

### ADR-4: Geist font family via next/font

**Context:** Inter is the most common AI-generated font choice. The skill bans it. External font CDNs add network dependencies.

**Decision:** Use Geist (sans) + Geist Mono loaded via `next/font/google` which self-hosts the font files in the static export.

**Rationale:** `next/font` downloads fonts at build time and includes them in the static output. No runtime CDN dependency. Geist is the Vercel system font — geometric, modern, excellent for dashboards.

**Consequences:** Fonts are bundled in the static export. Zero-layout-shift font loading. No external network requests.

### ADR-5: Placeholder when build doesn't exist

**Context:** Not all developers will build the frontend. The Python service should still respond at `/` with something useful rather than a 404.

**Decision:** When `dashboard/out/` doesn't exist, serve a minimal HTML placeholder with a reminder to build the dashboard (`cd dashboard && npm run build`). Remove the legacy `_render_dashboard()` once the Next.js dashboard ships.

**Rationale:** Maintaining two dashboard implementations is a maintenance trap. The placeholder is ~10 lines of HTML, not a second dashboard. It tells the operator exactly what to do.

**Consequences:** `GET /` always returns something. No dual-maintenance burden. Developers must run the build to get the real dashboard.

### ADR-6: Streaming via SSE (not WebSocket)

**Context:** Future streaming of agent events needs a real-time transport.

**Decision:** Use Server-Sent Events (SSE) via a new `/api/v1/stream` endpoint (see `docs/design/session-streaming.md` for full specification).

**Rationale:** SSE is simpler than WebSocket for unidirectional server→client data. Works through proxies and load balancers without special configuration. Native browser `EventSource` API with automatic reconnection. FastAPI supports SSE via `StreamingResponse`.

**Consequences:** Unidirectional only (server→client). If bidirectional is ever needed (e.g., sending commands), WebSocket can be added for that specific endpoint. SSE covers the streaming event log use case perfectly.
