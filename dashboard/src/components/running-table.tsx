"use client";

import { useStatePolling } from "@/hooks/use-state-polling";
import { StatusBadge } from "./status-badge";

function TableSkeleton() {
  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] shadow-[var(--shadow-card)] overflow-hidden mb-8">
      <div className="animate-pulse p-4 space-y-3">
        <div className="h-4 w-full bg-zinc-100 rounded" />
        <div className="h-4 w-5/6 bg-zinc-100 rounded" />
        <div className="h-4 w-4/6 bg-zinc-100 rounded" />
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] shadow-[var(--shadow-card)] overflow-hidden mb-8">
      <div className="text-center py-10 text-zinc-400 text-sm">
        <svg
          className="w-8 h-8 mx-auto mb-3 opacity-40"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <circle cx="12" cy="12" r="10" />
          <path d="M12 8v4m0 4h.01" />
        </svg>
        No active sessions
      </div>
    </div>
  );
}

export function RunningTable() {
  const { state, isLoading } = useStatePolling();

  if (isLoading || !state) return <TableSkeleton />;
  if (state.running.length === 0) return <EmptyState />;

  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] shadow-[var(--shadow-card)] overflow-hidden mb-8">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-zinc-50/50">
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Issue
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                State
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Session
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Turns
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Last Event
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Message
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Started
              </th>
              <th className="text-left text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400 px-4 py-3">
                Tokens
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {state.running.map((session) => (
              <tr
                key={session.session_id}
                className="hover:bg-zinc-50/50 transition-colors"
              >
                <td className="px-4 py-3 font-semibold text-accent">
                  {session.issue_identifier}
                </td>
                <td className="px-4 py-3">
                  <StatusBadge status={session.state} />
                </td>
                <td className="px-4 py-3 font-mono text-xs text-zinc-500 max-w-32 truncate">
                  {session.session_id}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-zinc-500">
                  {session.turn_count}
                </td>
                <td className="px-4 py-3 text-zinc-600 text-xs">
                  {session.last_event}
                </td>
                <td className="px-4 py-3 text-zinc-500 text-xs max-w-48 truncate">
                  {session.last_message}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-zinc-500">
                  {session.started_at}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-zinc-500">
                  {session.tokens.total_tokens.toLocaleString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
