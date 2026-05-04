import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RetryTable } from "@/components/retry-table";
import { MOCK_STATE, EMPTY_STATE } from "./fixtures";

vi.mock("@/hooks/use-state-polling", () => ({
  useStatePolling: vi.fn(),
}));

import { useStatePolling } from "@/hooks/use-state-polling";
const mockUseStatePolling = vi.mocked(useStatePolling);

describe("RetryTable", () => {
  it("renders empty state when no retries", () => {
    mockUseStatePolling.mockReturnValue({
      state: EMPTY_STATE,
      isLoading: false,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    render(<RetryTable />);
    expect(screen.getByText("No retries queued")).toBeInTheDocument();
  });

  it("renders retry entries", () => {
    mockUseStatePolling.mockReturnValue({
      state: MOCK_STATE,
      isLoading: false,
      isError: false,
      isStale: false,
      refresh: vi.fn(),
    });
    render(<RetryTable />);
    expect(screen.getByText("#10")).toBeInTheDocument();
    expect(screen.getByText("rate_limited")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument(); // attempt
  });
});
