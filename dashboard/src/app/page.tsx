"use client";

import { MetricsGrid } from "@/components/metrics-grid";
import { RunningTable } from "@/components/running-table";
import { RetryTable } from "@/components/retry-table";
import { ConnectionStatus } from "@/components/connection-status";
import { StreamPanel } from "@/components/stream-panel";

export default function DashboardPage() {
  return (
    <>
      <header className="grid grid-cols-[1fr_auto] items-end gap-4 mb-8 pb-6 border-b border-border">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-950">Symphony Dashboard</h1>
        <ConnectionStatus />
      </header>

      <MetricsGrid />

      <section aria-label="Running sessions">
        <div className="flex items-baseline gap-2 mb-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-950">Running</h2>
        </div>
        <RunningTable />
      </section>

      <section aria-label="Retry queue">
        <div className="flex items-baseline gap-2 mb-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-950">
            Retry Queue
          </h2>
        </div>
        <RetryTable />
      </section>

      <section aria-label="Event stream" className="mt-8">
        <div className="flex items-baseline gap-2 mb-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-950">
            Live Events
          </h2>
        </div>
        <StreamPanel />
      </section>
    </>
  );
}
