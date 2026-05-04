"use client";

import { useStatePolling } from "@/hooks/use-state-polling";
import { useRelativeTime } from "@/hooks/use-relative-time";

export function ConnectionStatus() {
  const { state, isError } = useStatePolling();
  const relativeTime = useRelativeTime(state?.generated_at);

  if (isError) {
    return (
      <span
        role="status"
        aria-live="polite"
        className="text-xs font-mono text-warning"
      >
        Connection lost — retrying...
      </span>
    );
  }

  if (relativeTime) {
    return (
      <span className="text-xs font-mono text-zinc-400">{relativeTime}</span>
    );
  }

  return null;
}
