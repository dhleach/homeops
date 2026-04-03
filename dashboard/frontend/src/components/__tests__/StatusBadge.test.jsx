import { render, screen } from "@testing-library/react";
import { StatusBadge } from "../StatusBadge.jsx";

describe("StatusBadge", () => {
  it("renders 'Heating' for heating action", () => {
    render(<StatusBadge action="heating" />);
    expect(screen.getByText("Heating")).toBeInTheDocument();
  });

  it("renders 'Idle' for idle action", () => {
    render(<StatusBadge action="idle" />);
    expect(screen.getByText("Idle")).toBeInTheDocument();
  });

  it("renders 'Unknown' when action is undefined", () => {
    render(<StatusBadge />);
    expect(screen.getByText("Unknown")).toBeInTheDocument();
  });

  it("is case-insensitive", () => {
    render(<StatusBadge action="HEATING" />);
    expect(screen.getByText("Heating")).toBeInTheDocument();
  });

  it("applies orange styling for heating", () => {
    const { container } = render(<StatusBadge action="heating" />);
    expect(container.firstChild.className).toMatch(/orange/);
  });

  it("applies green styling for idle", () => {
    const { container } = render(<StatusBadge action="idle" />);
    expect(container.firstChild.className).toMatch(/green/);
  });
});
