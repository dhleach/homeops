import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../../App.jsx";
import { FleetDeployView } from "../FleetDeployView.jsx";

function fleetSnapshot() {
  const environments = ["test", "stage", "prod"];
  const colors = ["blue", "green", "orange", "purple"];
  const shapes = ["circle", "square", "triangle", "hexagon"];
  const targets = environments.flatMap((environment) => (
    [1, 2, 3, 4].map((index) => {
      const profileIndex = index - 1;
      const profile = { color: colors[profileIndex], shape: shapes[profileIndex] };
      return {
        target_id: `${environment}-vehicle-${String(index).padStart(2, "0")}`,
        label: `${environment.toUpperCase()}-${String(index).padStart(2, "0")}`,
        environment,
        simulated: true,
        desired: profile,
        desired_digest: `${environment}-desired-${index}`,
        observed: profile,
        observed_digest: `${environment}-desired-${index}`,
        status: "ready",
        active_deployment_id: null,
        last_error: null,
        updated_at: "2026-09-25T23:00:00Z",
      };
    })
  ));

  return { simulated: true, target_kind: "simulated", targets };
}

describe("FleetDeployView", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => fleetSnapshot(),
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/");
  });

  it("renders all twelve simulated targets grouped by environment", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    expect(await screen.findAllByTestId("fleet-target-card")).toHaveLength(12);
    expect(screen.getByRole("heading", { name: "test" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "stage" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "prod" })).toBeInTheDocument();
    expect(screen.getAllByText("Simulated target")).toHaveLength(12);
    expect(screen.getByText("test-vehicle-01")).toBeInTheDocument();
    expect(screen.getAllByText("blue circle")).toHaveLength(6);
    expect(screen.getAllByText("Desired = observed")).toHaveLength(12);
  });

  it("reads the public fleet endpoint and keeps desired versus observed drift visible", async () => {
    const snapshot = fleetSnapshot();
    snapshot.targets[0].observed = { color: "purple", shape: "hexagon" };
    snapshot.targets[0].observed_digest = "observed-drift";
    snapshot.targets[0].status = "pending";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => snapshot,
    }));

    render(<FleetDeployView apiUrl="https://api.homeops.now/" />);

    await screen.findByText("Desired / observed drift");
    expect(fetch).toHaveBeenCalledWith(
      "https://api.homeops.now/deploy/api/fleet",
      expect.objectContaining({
        cache: "no-store",
        headers: { Accept: "application/json" },
      }),
    );
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getAllByText("sha256:test-desired").length).toBeGreaterThan(0);
    expect(screen.getByText("sha256:observed-dri")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Observed purple hexagon")[0]).toHaveClass("fleet-shape--hexagon");
  });

  it("loads directly through the /deploy route without rendering the HVAC dashboard", async () => {
    window.history.replaceState({}, "", "/deploy");

    render(<App />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "Fleet Deploy Lab" })).toBeInTheDocument());
    expect(screen.queryByText("What's the temperature right now?")).not.toBeInTheDocument();
  });
});
