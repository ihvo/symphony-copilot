/** Types for SSE streaming events from /api/v1/stream */

export interface StreamEventData {
  event: string;
  message?: string | null;
  error?: string | null;
  session_id?: string | null;
  turn_id?: string | null;
  usage?: Record<string, number> | null;
  rate_limits?: Record<string, unknown> | null;
  // session_dispatched extras
  attempt?: number | null;
  state?: string | null;
  // session_ended extras
  success?: boolean;
  turns?: number;
  tokens?: { input_tokens: number; output_tokens: number; total_tokens: number };
  // retry_scheduled extras
  delay_ms?: number;
  // session_killed extras
  reason?: string;
}

export interface StreamEvent {
  id: number;
  eventType: string;
  data: StreamEventData;
  receivedAt: string;
}

export type ConnectionState = "connecting" | "connected" | "disconnected";
