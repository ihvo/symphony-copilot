"use client";

import type { SystemState } from "@/lib/types";

export const MOCK_STATE: SystemState = {
  generated_at: new Date().toISOString(),
  counts: { running: 2, retrying: 1 },
  running: [
    {
      issue_id: "id1",
      issue_identifier: "#1",
      state: "open",
      session_id: "sess-abc",
      turn_count: 3,
      last_event: "turn_completed",
      last_message: "done",
      started_at: "2025-01-01T00:00:00Z",
      last_event_at: "2025-01-01T00:01:00Z",
      tokens: { input_tokens: 400, output_tokens: 300, total_tokens: 700 },
    },
    {
      issue_id: "id2",
      issue_identifier: "#5",
      state: "running",
      session_id: "sess-xyz",
      turn_count: 1,
      last_event: "turn_started",
      last_message: "working",
      started_at: "2025-01-01T00:02:00Z",
      last_event_at: "2025-01-01T00:02:30Z",
      tokens: { input_tokens: 100, output_tokens: 50, total_tokens: 150 },
    },
  ],
  retrying: [
    {
      issue_id: "id3",
      issue_identifier: "#10",
      attempt: 2,
      due_at: "2025-01-01T01:00:00Z",
      error: "rate_limited",
    },
  ],
  copilot_totals: {
    input_tokens: 500,
    output_tokens: 350,
    total_tokens: 850,
    seconds_running: 120,
  },
  rate_limits: null,
};

export const EMPTY_STATE: SystemState = {
  generated_at: new Date().toISOString(),
  counts: { running: 0, retrying: 0 },
  running: [],
  retrying: [],
  copilot_totals: {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    seconds_running: 0,
  },
  rate_limits: null,
};
