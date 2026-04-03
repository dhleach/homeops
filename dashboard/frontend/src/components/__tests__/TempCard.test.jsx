import { render, screen } from "@testing-library/react";
import { TempCard } from "../TempCard.jsx";

const FLOOR1_DATA = {
  zone: "floor_1",
  current_temp_f: 70,
  setpoint_f: 68,
  hvac_mode: "heat",
  hvac_action: "idle",
  last_updated: "2026-04-02T20:00:00Z",
};

describe("TempCard", () => {
  it("renders zone label", () => {
    render(<TempCard zone="floor_1" data={FLOOR1_DATA} />);
    expect(screen.getByText("Floor 1")).toBeInTheDocument();
  });

  it("renders current temperature", () => {
    render(<TempCard zone="floor_1" data={FLOOR1_DATA} />);
    expect(screen.getByText("70")).toBeInTheDocument();
  });

  it("renders setpoint", () => {
    render(<TempCard zone="floor_1" data={FLOOR1_DATA} />);
    expect(screen.getByText("Set to 68°F")).toBeInTheDocument();
  });

  it("renders status badge", () => {
    render(<TempCard zone="floor_1" data={FLOOR1_DATA} />);
    expect(screen.getByText("Idle")).toBeInTheDocument();
  });

  it("renders dash when temp data is null", () => {
    render(<TempCard zone="floor_1" data={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders floor_2 label correctly", () => {
    render(<TempCard zone="floor_2" data={{ ...FLOOR1_DATA, current_temp_f: 71 }} />);
    expect(screen.getByText("Floor 2")).toBeInTheDocument();
    expect(screen.getByText("71")).toBeInTheDocument();
  });

  it("renders floor_3 label correctly", () => {
    render(<TempCard zone="floor_3" data={{ ...FLOOR1_DATA, current_temp_f: 76 }} />);
    expect(screen.getByText("Floor 3")).toBeInTheDocument();
  });

  it("renders heating badge when hvac_action is heating", () => {
    render(<TempCard zone="floor_1" data={{ ...FLOOR1_DATA, hvac_action: "heating" }} />);
    expect(screen.getByText("Heating")).toBeInTheDocument();
  });

  it("does not render setpoint when data is null", () => {
    render(<TempCard zone="floor_1" data={null} />);
    expect(screen.queryByText(/Set to/)).not.toBeInTheDocument();
  });
});
