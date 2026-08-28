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

  it("renders 'Cooling' for cooling action", () => {
    render(<StatusBadge action="cooling" />);
    expect(screen.getByText("Cooling")).toBeInTheDocument();
  });

  it("renders 'Unavailable' for unavailable action", () => {
    render(<StatusBadge action="unavailable" />);
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
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

  it("applies blue styling for cooling", () => {
    const { container } = render(<StatusBadge action="cooling" />);
    expect(container.firstChild.className).toMatch(/sky/);
  });
});
