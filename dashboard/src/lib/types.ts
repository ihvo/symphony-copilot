export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface RunningSession {
  issue_id: string;
  issue_identifier: string;
  state: string;
  session_id: string;
  turn_count: number;
  last_event: string;
  last_message: string;
  started_at: string;
  last_event_at: string;
  tokens: TokenUsage;
}

export interface RetryEntry {
  issue_id: string;
  issue_identifier: string;
  attempt: number;
  due_at: string;
  error: string;
}

export interface CopilotTotals {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  seconds_running: number;
}

export interface SystemState {
  generated_at: string;
  counts: { running: number; retrying: number };
  running: RunningSession[];
  retrying: RetryEntry[];
  copilot_totals: CopilotTotals;
  rate_limits: Record<string, unknown> | null;
}
