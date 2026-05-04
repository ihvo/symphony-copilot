import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RunningTable } from "@/components/running-table";
import { MOCK_STATE, EMPTY_STATE } from "./fixtures";

vi.mock("@/hooks/use-state-polling", () => ({
  useStatePolling: vi.fn(),
}));

import { useStatePolling } from "@/hooks/use-state-polling";
const mockUseStatePolling = vi.mocked(useStatePolling);

describe("RunningTable", () => {
  it("renders skeleton when loading", () => {
    mockUseStatePolling.mockReturnValue({
      state: undefined,
      isLoading: true,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    const { container } = render(<RunningTable />);
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("renders empty state when no sessions", () => {
    mockUseStatePolling.mockReturnValue({
      state: EMPTY_STATE,
      isLoading: false,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    render(<RunningTable />);
    expect(screen.getByText("No active sessions")).toBeInTheDocument();
  });

  it("renders session rows with data", () => {
    mockUseStatePolling.mockReturnValue({
      state: MOCK_STATE,
      isLoading: false,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    render(<RunningTable />);
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("#5")).toBeInTheDocument();
    expect(screen.getByText("sess-abc")).toBeInTheDocument();
    expect(screen.getByText("turn_completed")).toBeInTheDocument();
    expect(screen.getByText("700")).toBeInTheDocument();
  });
});
