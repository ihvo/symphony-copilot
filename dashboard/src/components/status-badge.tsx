"use client";

export function StatusBadge({ status }: { status: string }) {
  const isActive = status === "running" || status === "open" || status === "active";
  const isRetry = status === "retrying" || status === "retry";

  return (
    <span
      className={`inline-block text-[0.6875rem] font-medium px-2 py-0.5 rounded-md lowercase ${
        isActive
          ? "bg-accent-subtle text-accent"
          : isRetry
            ? "bg-warning-subtle text-warning"
            : "bg-zinc-100 text-zinc-600"
      }`}
    >
      {status}
    </span>
  );
}
