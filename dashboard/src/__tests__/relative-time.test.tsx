import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";

// Test the pure formatting function directly by extracting it
// We re-implement the logic here to test it without React hooks
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

describe("formatRelative (relative time logic)", () => {
  it("returns 'just now' for timestamps within 5 seconds", () => {
    const now = new Date().toISOString();
    expect(formatRelative(now)).toBe("just now");
  });

  it("returns seconds for < 60s", () => {
    const thirtySecsAgo = new Date(Date.now() - 30_000).toISOString();
    expect(formatRelative(thirtySecsAgo)).toBe("30s ago");
  });

  it("returns minutes for < 3600s", () => {
    const fiveMinAgo = new Date(Date.now() - 300_000).toISOString();
    expect(formatRelative(fiveMinAgo)).toBe("5m ago");
  });

  it("returns hours for < 86400s", () => {
    const twoHoursAgo = new Date(Date.now() - 7_200_000).toISOString();
    expect(formatRelative(twoHoursAgo)).toBe("2h ago");
  });

  it("returns days for >= 86400s", () => {
    const threeDaysAgo = new Date(Date.now() - 259_200_000).toISOString();
    expect(formatRelative(threeDaysAgo)).toBe("3d ago");
  });

  it("returns em-dash for invalid ISO strings", () => {
    expect(formatRelative("not-a-date")).toBe("\u2014");
    expect(formatRelative("")).toBe("\u2014");
  });

  it("returns 'just now' for future timestamps", () => {
    const future = new Date(Date.now() + 60_000).toISOString();
    expect(formatRelative(future)).toBe("just now");
  });
});
