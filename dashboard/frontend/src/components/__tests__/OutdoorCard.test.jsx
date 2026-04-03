import { render, screen } from "@testing-library/react";
import { OutdoorCard } from "../OutdoorCard.jsx";

describe("OutdoorCard", () => {
  it("renders outdoor label", () => {
    render(<OutdoorCard temp={74} />);
    expect(screen.getByText("Outdoor")).toBeInTheDocument();
  });

  it("renders temperature value", () => {
    render(<OutdoorCard temp={74} />);
    expect(screen.getByText("74")).toBeInTheDocument();
  });

  it("renders dash when temp is null", () => {
    render(<OutdoorCard temp={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders dash when temp is undefined", () => {
    render(<OutdoorCard />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("shows last updated when provided", () => {
    const recent = new Date(Date.now() - 60_000).toISOString();
    render(<OutdoorCard temp={74} lastUpdated={recent} />);
    expect(screen.getByText(/Sensor updated/)).toBeInTheDocument();
  });

  it("does not show last updated line when not provided", () => {
    render(<OutdoorCard temp={74} />);
    expect(screen.queryByText(/Sensor updated/)).not.toBeInTheDocument();
  });
});
