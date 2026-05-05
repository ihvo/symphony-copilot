"use client";

import type { StreamEvent } from "@/lib/stream-types";

const EVENT_COLORS: Record<string, string> = {
  session_dispatched: "text-accent",
  session_started: "text-accent",
  turn_completed: "text-zinc-600",
  turn_failed: "text-warning",
  session_ended: "text-zinc-500",
  session_killed: "text-warning",
  retry_scheduled: "text-warning",
  notification: "text-zinc-500",
  gap: "text-red-500",
  overflow: "text-red-500",
};

const EVENT_LABELS: Record<string, string> = {
  session_dispatched: "dispatched",
  session_started: "started",
  turn_completed: "turn",
  turn_failed: "turn fail",
  session_ended: "ended",
  session_killed: "killed",
  retry_scheduled: "retry",
  notification: "info",
  gap: "gap",
  overflow: "overflow",
};

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}

function eventMessage(event: StreamEvent): string {
  const d = event.data;
  switch (event.eventType) {
    case "session_dispatched":
      return d.attempt != null ? `attempt ${d.attempt}` : "initial";
    case "session_started":
      return d.session_id ? `${d.session_id}` : "";
    case "turn_completed":
    case "turn_failed":
      return d.message || "";
    case "session_ended":
      return d.success ? `ok (${d.turns ?? "?"}t)` : `fail: ${d.error || "unknown"}`;
    case "session_killed":
      return d.reason || "";
    case "retry_scheduled":
      return d.delay_ms != null ? `in ${(d.delay_ms / 1000).toFixed(0)}s` : "";
    case "notification":
      return d.message || "";
    default:
      return d.message || d.error || "";
  }
}

function EventRow({ event }: { event: StreamEvent }) {
  const color = EVENT_COLORS[event.eventType] || "text-zinc-500";
  const label = EVENT_LABELS[event.eventType] || event.eventType;
  const msg = eventMessage(event);

  return (
    <div className="flex items-baseline gap-3 py-1.5 px-4 hover:bg-zinc-50/50 transition-colors text-xs">
      <span className="font-mono text-zinc-400 w-16 shrink-0 tabular-nums">
        {formatTime(event.receivedAt)}
      </span>
      <span className={`font-semibold w-16 shrink-0 ${color}`}>{label}</span>
      <span className="font-mono text-zinc-500 truncate">{msg}</span>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="text-center py-8 text-zinc-400 text-sm">
      <svg
        className="w-6 h-6 mx-auto mb-2 opacity-40"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      >
        <path d="M13 10V3L4 14h7v7l9-11h-7z" />
      </svg>
      Waiting for events...
    </div>
  );
}

export function EventLog({
  events,
  maxHeight = "24rem",
}: {
  events: StreamEvent[];
  maxHeight?: string;
}) {
  if (events.length === 0) return <EmptyState />;

  return (
    <div className="overflow-y-auto" style={{ maxHeight }}>
      {events.map((event) => (
        <EventRow key={event.id || event.receivedAt} event={event} />
      ))}
    </div>
  );
}
