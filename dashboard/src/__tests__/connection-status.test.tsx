import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConnectionStatus } from "@/components/connection-status";

vi.mock("@/hooks/use-state-polling", () => ({
  useStatePolling: vi.fn(),
}));
vi.mock("@/hooks/use-relative-time", () => ({
  useRelativeTime: vi.fn(),
}));

import { useStatePolling } from "@/hooks/use-state-polling";
import { useRelativeTime } from "@/hooks/use-relative-time";
const mockUseStatePolling = vi.mocked(useStatePolling);
const mockUseRelativeTime = vi.mocked(useRelativeTime);

describe("ConnectionStatus", () => {
  it("shows connection lost on error", () => {
    mockUseStatePolling.mockReturnValue({
      state: undefined,
      isLoading: false,
      isError: true,
      isStale: false,
      refresh: vi.fn(),
    });
    mockUseRelativeTime.mockReturnValue("");
    render(<ConnectionStatus />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Connection lost"
    );
  });

  it("shows relative time when connected", () => {
    mockUseStatePolling.mockReturnValue({
      state: { generated_at: "2025-01-01T00:00:00Z" } as never,
      isLoading: false,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    mockUseRelativeTime.mockReturnValue("15s ago");
    render(<ConnectionStatus />);
    expect(screen.getByText("15s ago")).toBeInTheDocument();
  });

  it("returns null when no data yet", () => {
    mockUseStatePolling.mockReturnValue({
      state: undefined,
      isLoading: true,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    mockUseRelativeTime.mockReturnValue("");
    const { container } = render(<ConnectionStatus />);
    expect(container.innerHTML).toBe("");
  });
});
