"use client";

import { useEffect, useState } from "react";

const INTERVALS: [number, string][] = [
  [60, "s"],
  [3600, "m"],
  [86400, "h"],
  [604800, "d"],
];

function formatRelative(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(diff)) return "\u2014";
  if (diff < 0) return "just now";
  if (diff < 5) return "just now";
  for (let i = 0; i < INTERVALS.length; i++) {
    const [threshold, unit] = INTERVALS[i];
    if (diff < threshold) {
      const prev = INTERVALS[i - 1];
      if (prev) {
        return `${Math.floor(diff / prev[0])}${unit} ago`;
      }
      return `${Math.floor(diff)}s ago`;
    }
  }
  return `${Math.floor(diff / 86400)}d ago`;
}

export function useRelativeTime(iso: string | undefined): string {
  const [display, setDisplay] = useState(() => (iso ? formatRelative(iso) : ""));

  useEffect(() => {
    if (!iso) return;
    setDisplay(formatRelative(iso));
    const id = setInterval(() => setDisplay(formatRelative(iso)), 10_000);
    return () => clearInterval(id);
  }, [iso]);

  return display;
}
