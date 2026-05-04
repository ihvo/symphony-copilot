import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "@/components/status-badge";

describe("StatusBadge", () => {
  it("renders the status text", () => {
    render(<StatusBadge status="running" />);
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("applies accent style for active statuses", () => {
    const { container } = render(<StatusBadge status="running" />);
    const badge = container.querySelector("span")!;
    expect(badge.className).toContain("text-accent");
  });

  it("applies warning style for retry statuses", () => {
    const { container } = render(<StatusBadge status="retrying" />);
    const badge = container.querySelector("span")!;
    expect(badge.className).toContain("text-warning");
  });

  it("applies neutral style for unknown statuses", () => {
    const { container } = render(<StatusBadge status="unknown" />);
    const badge = container.querySelector("span")!;
    expect(badge.className).toContain("text-zinc-600");
  });
});
