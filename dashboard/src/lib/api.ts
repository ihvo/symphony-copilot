import type { SystemState } from "./types";

export async function fetchState(): Promise<SystemState> {
  const res = await fetch("/api/v1/state");
  if (!res.ok) {
    throw new Error(`API error: ${res.status}`);
  }
  return res.json();
}
