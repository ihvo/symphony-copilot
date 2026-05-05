"use client";

import { useEventStream } from "@/hooks/use-event-stream";
import { useStatePolling } from "@/hooks/use-state-polling";
import { EventLog } from "./event-log";
import type { ConnectionState } from "@/lib/stream-types";

const STATE_INDICATORS: Record<ConnectionState, { dot: string; label: string }> = {
  connected: { dot: "bg-accent", label: "Live" },
  connecting: { dot: "bg-warning animate-pulse", label: "Connecting" },
  disconnected: { dot: "bg-zinc-400", label: "Disconnected" },
};

function StreamStatus({ state, eventCount }: { state: ConnectionState; eventCount: number }) {
  const { dot, label } = STATE_INDICATORS[state];
  return (
    <div className="flex items-center gap-2 text-xs text-zinc-500">
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
      <span className="font-mono">{label}</span>
      {eventCount > 0 && (
        <span className="font-mono text-zinc-400">({eventCount})</span>
      )}
    </div>
  );
}

export function StreamPanel({
  identifier,
  onDeselect,
}: {
  identifier?: string;
  onDeselect?: () => void;
}) {
  const { refresh } = useStatePolling();
  const { events, connectionState, clearEvents } = useEventStream(identifier, () => {
    refresh();
  });

  const title = identifier ? `Stream: ${identifier}` : "Event Stream";

  return (
    <div className="bg-surface border border-border rounded-[var(--radius-card)] shadow-[var(--shadow-card)] overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-3">
          <h3 className="text-[0.6875rem] font-semibold uppercase tracking-wider text-zinc-400">
            {title}
          </h3>
          {identifier && onDeselect && (
            <button
              onClick={onDeselect}
              className="text-[0.6875rem] font-medium text-zinc-400 hover:text-zinc-600 transition-colors"
            >
              Show all
            </button>
          )}
          <StreamStatus state={connectionState} eventCount={events.length} />
        </div>
        {events.length > 0 && (
          <button
            onClick={clearEvents}
            className="text-[0.6875rem] font-medium text-zinc-400 hover:text-zinc-600 transition-colors"
          >
            Clear
          </button>
        )}
      </div>
      <EventLog events={events} />
    </div>
  );
}
