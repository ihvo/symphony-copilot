"use client";

import { useStatePolling } from "@/hooks/use-state-polling";

function TableSkeleton() {
  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] shadow-[var(--shadow-card)] overflow-hidden">
      <div className="animate-pulse p-4 space-y-3">
        <div className="h-4 w-full bg-zinc-100 rounded" />
        <div className="h-4 w-3/4 bg-zinc-100 rounded" />
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] shadow-[var(--shadow-card)] overflow-hidden">
      <div className="text-center py-10 text-zinc-400 text-sm">
        <svg
          className="w-8 h-8 mx-auto mb-3 opacity-40"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <path d="M9 12l2 2 4-4" />
          <circle cx="12" cy="12" r="10" />
        </svg>
        No retries queued
      </div>
    </div>
  );
}

export function RetryTable() {
  const { state, isLoading } = useStatePolling();

  if (isLoading || !state) return <TableSkeleton />;
  if (state.retrying.length === 0) return <EmptyState />;

  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] shadow-[var(--shadow-card)] overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-zinc-50/50">
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Issue
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Attempt
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Due At
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Error
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {state.retrying.map((entry) => (
              <tr key={entry.issue_identifier} className="hover:bg-zinc-50/50 transition-colors">
                <td className="px-4 py-3 font-semibold text-accent">{entry.issue_identifier}</td>
                <td className="px-4 py-3 font-mono text-xs text-zinc-500">{entry.attempt}</td>
                <td className="px-4 py-3 font-mono text-xs text-zinc-500">{entry.due_at}</td>
                <td className="px-4 py-3 text-zinc-500 text-xs max-w-64 truncate">{entry.error}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
