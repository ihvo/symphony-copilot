import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { EventLog } from "@/components/event-log";
import type { StreamEvent } from "@/lib/stream-types";

function makeEvent(overrides: Partial<StreamEvent> = {}): StreamEvent {
  return {
    id: 1,
    eventType: "turn_completed",
    data: { event: "turn_completed", message: "Turn 1 completed" },
    receivedAt: "2025-01-01T00:00:30.000Z",
    ...overrides,
  };
}

describe("EventLog", () => {
  it("renders empty state when no events", () => {
    render(<EventLog events={[]} />);
    expect(screen.getByText("Waiting for events...")).toBeInTheDocument();
  });

  it("renders event rows", () => {
    const events = [
      makeEvent({ id: 2, eventType: "session_started", data: { event: "session_started", session_id: "sess-1" } }),
      makeEvent({ id: 1, eventType: "session_dispatched", data: { event: "session_dispatched", attempt: null } }),
    ];
    render(<EventLog events={events} />);
    expect(screen.getByText("started")).toBeInTheDocument();
    expect(screen.getByText("dispatched")).toBeInTheDocument();
    expect(screen.getByText("sess-1")).toBeInTheDocument();
    expect(screen.getByText("initial")).toBeInTheDocument();
  });

  it("renders session_ended with success", () => {
    const events = [
      makeEvent({ id: 1, eventType: "session_ended", data: { event: "session_ended", success: true, turns: 5 } }),
    ];
    render(<EventLog events={events} />);
    expect(screen.getByText("ended")).toBeInTheDocument();
    expect(screen.getByText("ok (5t)")).toBeInTheDocument();
  });

  it("renders session_ended with failure", () => {
    const events = [
      makeEvent({ id: 1, eventType: "session_ended", data: { event: "session_ended", success: false, error: "rate_limited" } }),
    ];
    render(<EventLog events={events} />);
    expect(screen.getByText("fail: rate_limited")).toBeInTheDocument();
  });

  it("renders retry_scheduled with delay", () => {
    const events = [
      makeEvent({ id: 1, eventType: "retry_scheduled", data: { event: "retry_scheduled", delay_ms: 5000 } }),
    ];
    render(<EventLog events={events} />);
    expect(screen.getByText("retry")).toBeInTheDocument();
    expect(screen.getByText("in 5s")).toBeInTheDocument();
  });

  it("renders session_killed with reason", () => {
    const events = [
      makeEvent({ id: 1, eventType: "session_killed", data: { event: "session_killed", reason: "terminal_state" } }),
    ];
    render(<EventLog events={events} />);
    expect(screen.getByText("killed")).toBeInTheDocument();
    expect(screen.getByText("terminal_state")).toBeInTheDocument();
  });
});
