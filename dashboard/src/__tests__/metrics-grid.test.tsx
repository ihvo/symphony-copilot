import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricsGrid } from "@/components/metrics-grid";
import { MOCK_STATE, EMPTY_STATE } from "./fixtures";

vi.mock("@/hooks/use-state-polling", () => ({
  useStatePolling: vi.fn(),
}));

import { useStatePolling } from "@/hooks/use-state-polling";
const mockUseStatePolling = vi.mocked(useStatePolling);

describe("MetricsGrid", () => {
  it("renders skeleton when loading", () => {
    mockUseStatePolling.mockReturnValue({
      state: undefined,
      isLoading: true,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    const { container } = render(<MetricsGrid />);
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it("renders metrics from state data", () => {
    mockUseStatePolling.mockReturnValue({
      state: MOCK_STATE,
      isLoading: false,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    render(<MetricsGrid />);
    expect(screen.getByText("2")).toBeInTheDocument(); // running count
    expect(screen.getByText("Active Sessions")).toBeInTheDocument();
    expect(screen.getByText("850")).toBeInTheDocument(); // total tokens
    expect(screen.getByText("2m")).toBeInTheDocument(); // 120s = 2m
  });

  it("renders zero state correctly", () => {
    mockUseStatePolling.mockReturnValue({
      state: EMPTY_STATE,
      isLoading: false,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    render(<MetricsGrid />);
    expect(screen.getByText("Active Sessions")).toBeInTheDocument();
    expect(screen.getByText("0s")).toBeInTheDocument(); // 0 seconds runtime
    // Multiple cards show "0" — verify all 4 cards render
    const cards = screen.getAllByText("0");
    expect(cards.length).toBe(3); // running=0, retrying=0, tokens=0
  });
});
