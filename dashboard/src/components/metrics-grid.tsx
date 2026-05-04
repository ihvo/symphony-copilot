"use client";

import { useStatePolling } from "@/hooks/use-state-polling";

function MetricsSkeleton() {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-[2fr_1fr_1fr_1fr] gap-3 mb-8">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="bg-surface border border-border rounded-[var(--radius-card)] p-5 shadow-[var(--shadow-card)] animate-pulse"
        >
          <div className="h-8 w-16 bg-zinc-100 rounded mb-2" />
          <div className="h-3 w-24 bg-zinc-100 rounded" />
        </div>
      ))}
    </div>
  );
}

function formatRuntime(seconds: number): string {
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds)}s`;
}

function MetricCard({
  value,
  label,
  accent,
}: {
  value: string | number;
  label: string;
  accent?: boolean;
}) {
  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] px-5 py-4 shadow-[var(--shadow-card)] hover:shadow-[var(--shadow-card-hover)] transition-shadow">
      <div
        className={`text-3xl font-bold tracking-tight font-mono ${
          accent ? "text-accent" : "text-zinc-950"
        }`}
      >
        {value}
      </div>
      <div className="text-xs font-medium text-zinc-500 uppercase tracking-wider mt-1">
        {label}
      </div>
    </div>
  );
}

export function MetricsGrid() {
  const { state, isLoading } = useStatePolling();

  if (isLoading || !state) return <MetricsSkeleton />;

  const runtime = formatRuntime(state.copilot_totals.seconds_running);

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-[2fr_1fr_1fr_1fr] gap-3 mb-8">
      <MetricCard value={state.counts.running} label="Active Sessions" accent />
      <MetricCard value={state.counts.retrying} label="Retrying" />
      <MetricCard
        value={state.copilot_totals.total_tokens.toLocaleString()}
        label="Tokens Used"
      />
      <MetricCard value={runtime} label="Runtime" />
    </div>
  );
}
