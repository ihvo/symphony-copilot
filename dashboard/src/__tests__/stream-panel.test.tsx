import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { StreamPanel } from "@/components/stream-panel";

vi.mock("@/hooks/use-state-polling", () => ({
  useStatePolling: vi.fn(() => ({
    state: undefined,
    isLoading: true,
    isError: false,
    isStale: false,
    refresh: vi.fn(),
  })),
}));

vi.mock("@/hooks/use-event-stream", () => ({
  useEventStream: vi.fn(() => ({
    events: [],
    connectionState: "connected" as const,
    clearEvents: vi.fn(),
    reconnect: vi.fn(),
  })),
}));

import { useEventStream } from "@/hooks/use-event-stream";
const mockUseEventStream = vi.mocked(useEventStream);

describe("StreamPanel", () => {
  it("renders event stream header", () => {
    render(<StreamPanel />);
    expect(screen.getByText("Event Stream")).toBeInTheDocument();
  });

  it("shows Live indicator when connected", () => {
    render(<StreamPanel />);
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("shows Connecting when connecting", () => {
    mockUseEventStream.mockReturnValue({
      events: [],
      connectionState: "connecting",
      clearEvents: vi.fn(),
      reconnect: vi.fn(),
    });
    render(<StreamPanel />);
    expect(screen.getByText("Connecting")).toBeInTheDocument();
  });

  it("shows Disconnected when disconnected", () => {
    mockUseEventStream.mockReturnValue({
      events: [],
      connectionState: "disconnected",
      clearEvents: vi.fn(),
      reconnect: vi.fn(),
    });
    render(<StreamPanel />);
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
  });

  it("shows event count when events exist", () => {
    mockUseEventStream.mockReturnValue({
      events: [
        { id: 1, eventType: "turn_completed", data: { event: "turn_completed", message: "done" }, receivedAt: new Date().toISOString() },
        { id: 2, eventType: "session_ended", data: { event: "session_ended", success: true }, receivedAt: new Date().toISOString() },
      ],
      connectionState: "connected",
      clearEvents: vi.fn(),
      reconnect: vi.fn(),
    });
    render(<StreamPanel />);
    expect(screen.getByText("(2)")).toBeInTheDocument();
  });

  it("shows Clear button when events exist", () => {
    mockUseEventStream.mockReturnValue({
      events: [
        { id: 1, eventType: "turn_completed", data: { event: "turn_completed" }, receivedAt: new Date().toISOString() },
      ],
      connectionState: "connected",
      clearEvents: vi.fn(),
      reconnect: vi.fn(),
    });
    render(<StreamPanel />);
    expect(screen.getByText("Clear")).toBeInTheDocument();
  });

  it("shows empty state when no events", () => {
    mockUseEventStream.mockReturnValue({
      events: [],
      connectionState: "connected",
      clearEvents: vi.fn(),
      reconnect: vi.fn(),
    });
    render(<StreamPanel />);
    expect(screen.getByText("Waiting for events...")).toBeInTheDocument();
  });
});
