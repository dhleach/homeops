import { render, screen } from "@testing-library/react";
import { LiveSummary } from "../LiveSummary.jsx";

describe("LiveSummary", () => {
  it("groups mixed heating, cooling, idle, and unavailable zones", () => {
    render(
      <LiveSummary
        zones={{
          floor_1: { hvac_action: "cooling" },
          floor_2: { hvac_action: "heating" },
          floor_3: { hvac_action: "idle" },
        }}
      />,
    );

    expect(screen.getByText("Cooling")).toBeInTheDocument();
    expect(screen.getByText("Heating")).toBeInTheDocument();
    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByText("Floor 1")).toBeInTheDocument();
    expect(screen.getByText("Floor 2")).toBeInTheDocument();
    expect(screen.getByText("Floor 3")).toBeInTheDocument();
    expect(
      screen.getByText("Cooling reflects inferred thermostat demand, not compressor feedback."),
    ).toBeInTheDocument();
  });

  it("does not call an idle-in-cool zone active cooling", () => {
    render(
      <LiveSummary
        zones={{
          floor_1: { hvac_action: "idle" },
          floor_2: { hvac_action: "idle" },
          floor_3: { hvac_action: "idle" },
        }}
      />,
    );

    expect(screen.queryByText("Cooling")).not.toBeInTheDocument();
    expect(screen.getByText("Current thermostat-derived zone activity")).toBeInTheDocument();
    expect(screen.getByText("Idle")).toBeInTheDocument();
  });

  it("shows unavailable when the snapshot is stale", () => {
    render(
      <LiveSummary
        stale
        zones={{
          floor_1: { hvac_action: "cooling" },
          floor_2: { hvac_action: "heating" },
          floor_3: { hvac_action: "idle" },
        }}
      />,
    );

    expect(screen.getByText("Live HVAC state is unavailable.")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Cooling")).not.toBeInTheDocument();
    expect(screen.queryByText("Heating")).not.toBeInTheDocument();
  });
});
