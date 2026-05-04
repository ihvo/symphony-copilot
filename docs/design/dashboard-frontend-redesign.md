# Dashboard Frontend Redesign

**Status:** Proposed  
**Author:** Ihar / Copilot  
**Package:** symphony

## Version History

| Version | Date | Summary |
|---------|------|---------|
| 0.1 | 2026-05-04 | Initial design |
| 0.2 | 2026-05-04 | Incorporated single-model review feedback (5 HIGH issues resolved) |

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

- No build step or frontend tooling (pure Python project, `pyproject.toml` only)
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
| R7 | Empty/loading states designed (not just "No data") |
| R8 | Zero external JS dependencies (inline `<script>` only, ~100 LOC vanilla JS) |
| R9 | All existing test assertions continue to pass |
| R10 | Stale/failure indication: visible "last updated" timestamp and connection-lost warning when fetch fails 3+ times |
| R11 | XSS safety: all dynamic DOM writes use `textContent`/`setAttribute` only — no `innerHTML` for snapshot data |
| R12 | Graceful fallback: `<meta refresh>` is only removed after first successful fetch+patch proves JS is healthy |

### Nice-to-have

| ID | Requirement |
|----|-------------|
| N1 | Per-issue detail view (click issue identifier → inline expandable panel populated from `/api/v1/{id}`) |
| N2 | Relative time display ("2m ago" instead of ISO timestamp) |
| N3 | Token burn rate indicator (tokens/minute derived from totals + runtime) |
| N4 | Visual state transitions (fade-in new rows, highlight changes) with `prefers-reduced-motion` respect |
| N5 | Dark mode support via `prefers-color-scheme` |
| N6 | Pause polling when tab is hidden (`document.hidden`) to save resources |

### Constraints

| ID | Constraint |
|----|------------|
| C1 | No `package.json`, no build tools, no npm/node dependencies |
| C2 | Dashboard must work as a single HTML response (no external static files) |
| C3 | Must degrade gracefully with JS disabled (server-rendered HTML is the baseline) |
| C4 | Inline JS: ~100 LOC vanilla JS (no minification step; keep it readable) |
| C5 | No emoji anywhere in the output |
| C6 | JS and CSS extracted to module-level constants in server.py (not one giant f-string) |

---

## 4. Options Evaluation

### Option A: Enhanced Server-Rendered HTML (Inline CSS only)

Keep `_render_dashboard()` as a Python f-string producing a complete HTML page with improved CSS.

| Dimension | Assessment |
|-----------|-----------|
| Complexity | Very low — CSS-only changes |
| Refresh | Still uses `<meta http-equiv="refresh">` (flicker remains) |
| Interactivity | None — static HTML |
| Maintenance | Easy — single function |
| Performance | Instant render, no JS overhead |

**Verdict:** Addresses R1, R2, R5, R6, R7 but NOT R3 (smooth refresh).

### Option B: Server-Rendered HTML + Inline Fetch Loop (Recommended)

Keep the server-rendered HTML baseline but add a small inline `<script>` that:
1. Fetches `/api/v1/state` on interval
2. Patches DOM in-place (no full reload)
3. Adds relative timestamps and subtle transitions

| Dimension | Assessment |
|-----------|-----------|
| Complexity | Low — ~80 lines of vanilla JS |
| Refresh | Smooth in-place DOM update |
| Interactivity | Clickable issues, relative times, transitions |
| Maintenance | Moderate — JS logic in Python f-string |
| Performance | Minimal — one fetch/10s, no framework |

**Verdict:** Addresses ALL must-haves (R1-R9). JS is progressive enhancement over server-rendered baseline.

### Option C: Full Client-Side SPA (React/Preact via CDN)

Serve a minimal HTML shell that loads React/Preact from CDN and renders entirely client-side.

| Dimension | Assessment |
|-----------|-----------|
| Complexity | High — JSX in a CDN script, complex state management |
| Refresh | Real-time with granular updates |
| Interactivity | Full — routing, animations, charts |
| Maintenance | Hard — untestable from Python, version pinning risk |
| Performance | 50-100KB CDN dependency, FOUC on first load |

**Verdict:** Overkill. Violates C1 spirit (keeping it simple Python-only). CDN dependency introduces availability risk for an operational tool.

### Comparison Matrix

| Criterion | Weight | Option A | Option B | Option C |
|-----------|--------|----------|----------|----------|
| Visual quality | 25% | 9 | 9 | 9 |
| Smooth refresh | 20% | 2 | 9 | 10 |
| Simplicity | 20% | 10 | 8 | 3 |
| Zero dependencies | 15% | 10 | 10 | 4 |
| Interactivity | 10% | 1 | 7 | 10 |
| Testability | 10% | 10 | 9 | 5 |
| **Weighted Score** | | **7.3** | **8.8** | **6.7** |

**Recommendation: Option B** — best balance of premium UX and operational simplicity.

---

## 5. Recommended Approach

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser                                                     │
├─────────────────────────────────────────────────────────────┤
│  Server-rendered HTML (initial paint)                        │
│    ├── Semantic structure + CSS (inline <style>)             │
│    ├── Data attributes on dynamic elements                  │
│    └── Inline <script> (progressive enhancement)            │
│         ├── fetch(/api/v1/state) every 10s                  │
│         ├── DOM diffing (update only changed cells)          │
│         ├── Relative time formatting                         │
│         └── CSS transition triggers on update               │
└─────────────────────────────────────────────────────────────┘
         │                        ▲
         │ fetch (JSON)           │ HTMLResponse (initial)
         ▼                        │
┌─────────────────────────────────────────────────────────────┐
│  FastAPI (symphony/server.py)                               │
│    GET /           → _render_dashboard(snapshot) [HTML]      │
│    GET /api/v1/state → JSON snapshot [for JS refresh]       │
└─────────────────────────────────────────────────────────────┘
```

### Key Design Decisions

1. **Progressive enhancement:** Page works fully without JS. Script upgrades the experience by replacing `<meta refresh>` with fetch-based updates.
2. **DOM patching over innerHTML:** Update individual `<td>` values via `data-field` attributes to preserve scroll position and enable CSS transitions.
3. **No virtual DOM, no framework:** Vanilla JS with `querySelectorAll` + direct attribute mutation.
4. **Server-rendered is the source of truth:** The initial HTML render is complete and correct. JS only adds polish.

---

## 6. Component Design

### 6.1 Visual Design System

```
Typography:
  - Headlines: Outfit 600, tracking-tight
  - Body: Outfit 400, 0.8125rem
  - Data/Mono: JetBrains Mono 400, 0.75rem

Colors:
  --bg:             #fafafa     (page background)
  --surface:        #ffffff     (cards/tables)
  --border:         #e4e4e7     (zinc-200)
  --text-primary:   #18181b     (zinc-950, NOT pure black)
  --text-secondary: #71717a     (zinc-500)
  --text-muted:     #a1a1aa     (zinc-400)
  --accent:         #059669     (emerald-600)
  --accent-subtle:  #ecfdf5     (emerald-50)
  --warning:        #d97706     (amber-600)
  --warning-subtle: #fffbeb     (amber-50)

Layout:
  - Max-width: 1400px, centered
  - Metrics grid: 2fr 1fr 1fr 1fr (asymmetric)
  - Section padding: 3rem 2.5rem (desktop), 1.5rem 1rem (mobile)
  - Border-radius: 12px (cards), 6px (badges)

Shadows:
  - sm: 0 1px 2px rgba(0,0,0,0.04)
  - md: 0 4px 12px -2px rgba(0,0,0,0.06)
  (Tinted to background hue, not pure black)
```

### 6.2 Page Layout

```
┌──────────────────────────────────────────────────────────┐
│ [Symphony Dashboard]                    [timestamp mono] │  ← header
├──────────────────────────────────────────────────────────┤
│ ┌─────────────────┐ ┌────────┐ ┌────────┐ ┌──────────┐ │
│ │ 3               │ │ 1      │ │ 12,450 │ │ 4m       │ │  ← metrics
│ │ Active Sessions │ │ Retry  │ │ Tokens │ │ Runtime  │ │     (2fr 1fr 1fr 1fr)
│ └─────────────────┘ └────────┘ └────────┘ └──────────┘ │
├──────────────────────────────────────────────────────────┤
│ RUNNING  3                                               │  ← section header
│ ┌────────────────────────────────────────────────────┐   │
│ │ Issue │ State │ Session │ Turns │ Event │ ...      │   │  ← table (divide-y)
│ │ #14   │ open  │ abc123  │   5   │ turn  │ ...      │   │
│ │ #21   │ open  │ def456  │   2   │ start │ ...      │   │
│ └────────────────────────────────────────────────────┘   │
├──────────────────────────────────────────────────────────┤
│ RETRY QUEUE  1                                           │  ← section header
│ ┌────────────────────────────────────────────────────┐   │
│ │ Issue │ Attempt │ Due At │ Error                   │   │
│ │ #7    │    2    │ 30s    │ timeout after 60s       │   │
│ └────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### 6.3 Inline Script Architecture

```javascript
// ~100 lines, extracted to DASHBOARD_SCRIPT constant in server.py
(function() {
  // 1. State
  let controller = null;       // AbortController for in-flight request
  let lastGenerated = null;    // monotonic freshness check
  let failures = 0;
  const INTERVAL_MS = 10_000;
  const MAX_FAILURES = 3;
  const metaRefresh = document.querySelector('meta[http-equiv="refresh"]');
  const statusEl = document.getElementById('refresh-status');

  // 2. Fetch with serialization (only one in-flight at a time)
  async function refresh() {
    if (document.hidden) return;  // pause when tab hidden
    if (controller) controller.abort();  // cancel stale request
    controller = new AbortController();

    try {
      const res = await fetch('/api/v1/state', { signal: controller.signal });
      if (!res.ok) throw new Error(res.status);
      const state = await res.json();

      // Monotonic freshness: drop stale responses
      if (lastGenerated && state.generated_at <= lastGenerated) return;
      lastGenerated = state.generated_at;

      patch(state);
      failures = 0;

      // Remove meta-refresh only AFTER first successful patch
      if (metaRefresh) { metaRefresh.remove(); }
      if (statusEl) statusEl.textContent = '';
    } catch (e) {
      if (e.name === 'AbortError') return;
      failures++;
      if (failures >= MAX_FAILURES && statusEl) {
        statusEl.textContent = 'Connection lost — retrying...';
      }
    } finally {
      controller = null;
    }
  }

  // 3. DOM patching — textContent only, NEVER innerHTML
  function patch(state) {
    // Update metric values via data-metric attributes
    document.querySelectorAll('[data-metric]').forEach(el => {
      const key = el.dataset.metric;
      const val = resolveMetric(key, state);
      if (el.textContent !== val) {
        el.textContent = val;
        el.classList.add('updated');
        setTimeout(() => el.classList.remove('updated'), 600);
      }
    });

    // Reconcile table rows (keyed by issue_identifier)
    reconcileTable('running-body', state.running, renderRunningRow);
    reconcileTable('retry-body', state.retrying, renderRetryRow);
  }

  // 4. Row reconciliation — keyed by issue_identifier
  function reconcileTable(tbodyId, items, renderFn) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    const existing = new Map();
    tbody.querySelectorAll('tr[data-key]').forEach(tr => {
      existing.set(tr.dataset.key, tr);
    });

    // Rebuild in server order
    const fragment = document.createDocumentFragment();
    if (items.length === 0) {
      // Show empty state
      const emptyRow = tbody.querySelector('.empty-row') || createEmptyRow(tbodyId);
      fragment.appendChild(emptyRow);
    } else {
      items.forEach(item => {
        const key = item.issue_identifier;
        const row = existing.get(key) || renderFn(item);
        updateRow(row, item);
        fragment.appendChild(row);
      });
    }
    tbody.replaceChildren(fragment);
  }

  // 5. Relative time helper
  function relTime(iso) {
    if (!iso) return '';
    const delta = (Date.now() - new Date(iso)) / 1000;
    if (delta < 60) return Math.floor(delta) + 's ago';
    if (delta < 3600) return Math.floor(delta/60) + 'm ago';
    return (delta/3600).toFixed(1) + 'h ago';
  }

  // 6. Start loop + visibility listener
  setInterval(refresh, INTERVAL_MS);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
})();
```

**Key safety properties:**
- `AbortController` serializes requests — no overlapping fetches
- `generated_at` comparison drops stale responses that arrive out-of-order
- Meta-refresh only removed after proven JS success
- All DOM writes use `textContent` / `setAttribute` — no `innerHTML` for data
- Rows keyed by `issue_identifier` for stable reconciliation
- Polling pauses when tab is hidden

### 6.4 Accessibility for Live Updates

```html
<!-- Status region announces connection state -->
<span id="refresh-status" role="status" aria-live="polite"></span>

<!-- Tables use aria-live="polite" for screen reader updates -->
<tbody id="running-body" aria-live="polite" aria-relevant="additions removals">
```

- `aria-live="polite"` on status and table bodies
- CSS transitions respect `prefers-reduced-motion: reduce` (disable animations)
- Semantic `<header>`, `<main>` landmarks in page structure

### 6.5 Empty States

```html
<!-- Running table empty -->
<div class="empty">
  <svg class="empty-icon">...</svg>  <!-- simple circle-info icon -->
  No active sessions
</div>

<!-- Retry table empty -->
<div class="empty">
  <svg class="empty-icon">...</svg>  <!-- checkmark-circle icon -->
  No retries queued
</div>
```

Inline SVG icons (no external icon library). Clean, centered, muted.

### 6.6 Code Organization in server.py

To avoid one giant f-string blob, the implementation splits concerns:

```python
# Module-level constants
FAVICON_SVG = "..."          # existing
DASHBOARD_CSS = "..."        # extracted ~120 lines
DASHBOARD_SCRIPT = "..."     # extracted ~100 lines

# Helper functions
def _render_metric(value, label, css_class=""): ...
def _render_running_row(entry): ...
def _render_retry_row(entry): ...

# Main renderer composes the parts
def _render_dashboard(snapshot): ...
```

This keeps each piece reviewable and testable in isolation.

### 6.7 Responsive Behavior

| Breakpoint | Layout |
|------------|--------|
| `>= 1400px` | Full layout, all columns visible |
| `768-1399px` | Metrics: 2fr 1fr 1fr 1fr, tables scroll horizontally |
| `< 768px` | Metrics: 2x2 grid, tables scroll in wrapper, padding reduced |

---

## 7. Migration Plan

### Phase 1: CSS Redesign (Complete)

- Replace old inline styles with new design system
- Implement asymmetric metrics grid, clean tables, proper typography
- Remove emoji from title
- Add responsive breakpoints
- **Gate:** All existing tests pass, dashboard renders correctly

### Phase 2: Inline Script (Progressive Enhancement)

- Add `<script>` block at end of body
- Implement fetch loop replacing meta-refresh
- Add `data-field` attributes to dynamic elements
- Implement DOM patching logic
- Add relative time formatting
- **Gate:** Dashboard works identically with JS disabled. With JS, updates are smooth.

### Phase 3: Polish (Nice-to-haves)

- Add issue identifier links to detail view
- Add token burn rate calculation
- Add CSS transitions for value changes (subtle pulse on update)
- Add dark mode via `prefers-color-scheme`
- Add connection status indicator
- **Gate:** No regressions, polish only adds, never removes.

### Effort Estimate

| Phase | Effort |
|-------|--------|
| Phase 1 | 1 day (already done) |
| Phase 2 | 1 day |
| Phase 3 | 1-2 days |

---

## 8. Test Strategy

### Existing tests (must continue passing)

- `test_dashboard_endpoint`: Asserts "Symphony Dashboard", "#1", `rel="icon"`, `data:image/svg+xml,` present in HTML
- `test_dashboard_html` (integration): Asserts "Symphony Dashboard" in response from real server
- XSS safety: HTML escaping of all user-supplied data via `_esc()` / `html.escape()`

### New tests to add

| Test | Purpose |
|------|---------|
| `test_dashboard_no_emoji` | Assert no emoji codepoints (U+1F000-U+1FFFF) in HTML output |
| `test_dashboard_responsive_meta` | Assert `viewport` meta tag present |
| `test_dashboard_accessible_landmarks` | Assert `<header>`, `<main>` or ARIA landmark roles present |
| `test_dashboard_metric_formatting` | Assert large numbers use comma separators |
| `test_dashboard_empty_state_text` | Assert friendly empty state messages appear with 0 running |
| `test_dashboard_xss_safety` | Assert HTML-special chars in issue data are escaped in output |
| `test_dashboard_script_present` | Assert inline `<script>` block exists (Phase 2) |
| `test_dashboard_data_attributes` | Assert dynamic elements have `data-metric` / `data-key` (Phase 2) |
| `test_dashboard_aria_live` | Assert `aria-live` on status region and table bodies (Phase 2) |
| `test_dashboard_meta_refresh_present` | Assert meta-refresh is in initial HTML (JS removes it after first success) |

### Test approach

All tests use the existing `httpx.ASGITransport` pattern from `test_server.py` — no browser testing needed since the dashboard is progressive enhancement over server-rendered HTML.

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

### ADR-1: No frontend build tooling

**Context:** Symphony is a pure Python service. Adding npm/node would complicate the development workflow and CI.

**Decision:** All frontend code (HTML, CSS, JS) is inlined in the Python server module.

**Rationale:** The dashboard is ~500 lines of markup/style/script. This doesn't warrant a build step. Inline code is version-controlled alongside the API it renders.

**Consequences:** No TypeScript, no JSX, no CSS preprocessing. Acceptable for this scope.

### ADR-2: Progressive enhancement over SPA

**Context:** We could serve a React app from CDN for richer interactivity.

**Decision:** Server-rendered HTML is the baseline. Inline vanilla JS enhances it.

**Rationale:** An operational monitoring tool must work reliably. CDN dependencies, JS bundle failures, and framework hydration issues are unacceptable for a tool you check during incidents.

**Consequences:** Limited interactivity ceiling. If we ever need charts or complex state, we'd revisit.

### ADR-3: Emerald accent, not blue/purple

**Context:** Default AI-generated dashboards trend toward blue/purple "tech" palettes.

**Decision:** Zinc neutral base with emerald-600 (`#059669`) as the sole accent.

**Rationale:** Emerald provides clear "active/healthy" signaling for running sessions. Warm amber for warnings. Avoids the generic "AI dashboard" look.

**Consequences:** Consistent with the design-taste-frontend skill directives. Single accent keeps visual noise low.

### ADR-4: Outfit + JetBrains Mono typography (optional polish)

**Context:** Inter is the most common AI-generated font choice. The skill bans it.

**Decision:** Outfit (headlines/body) + JetBrains Mono (data/timestamps) loaded from Google Fonts CDN. System-ui is the immediate fallback.

**Rationale:** Outfit has personality (geometric, modern) without sacrificing readability. JetBrains Mono is a high-quality monospace designed for code/data.

**Consequences:** Requires Google Fonts CDN link. If CDN is unavailable, `system-ui` renders immediately — no layout shift, no FOIT. The fonts are purely aesthetic; the dashboard is fully functional without them. This is acceptable because unlike JS framework CDN failures (which break functionality), font CDN failures only degrade to system fonts.

### ADR-5: Graceful degradation over fail-fast

**Context:** The script removes meta-refresh to provide smooth updates. If JS breaks, the page could become permanently stale.

**Decision:** Meta-refresh is only removed after the first successful fetch+patch cycle. If 3+ consecutive fetches fail, a visible "connection lost" indicator appears.

**Rationale:** An operational dashboard must never silently go stale. The worst outcome is an operator believing everything is fine because the dashboard looks normal but hasn't updated in 5 minutes.

**Consequences:** Slightly more complex JS logic. Worth it for operational safety.
