"use client";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <svg
        className="w-12 h-12 text-zinc-300 mb-4"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M12 8v4m0 4h.01" />
      </svg>
      <h2 className="text-lg font-semibold text-zinc-950 mb-2">Something went wrong</h2>
      <p className="text-sm text-zinc-500 mb-4 max-w-md">{error.message}</p>
      <button
        onClick={reset}
        className="text-sm font-medium text-accent hover:text-accent/80 transition-colors"
      >
        Try again
      </button>
    </div>
  );
}
