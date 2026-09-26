import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

  return {
    simulated: true,
    target_kind: "simulated",
    capabilities: [
      { implementation: "python", strategy: "all_at_once", failure_mode: "abort" },
      { implementation: "ansible", strategy: "all_at_once", failure_mode: "abort" },
    ],
    targets,
  };
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
    window.sessionStorage.removeItem("homeops.activeFleetDeploymentId");
    window.sessionStorage.removeItem("homeops.fleetDeployAttemptId");
    window.history.replaceState({}, "", "/");
  });

  it("renders all twelve simulated targets grouped by environment", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    expect(await screen.findAllByTestId("fleet-target-card")).toHaveLength(12);
    expect(screen.getByRole("heading", { name: "test" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "stage" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "prod" })).toBeInTheDocument();
    expect(screen.getAllByText("Simulated target")).toHaveLength(12);
    expect(screen.getAllByText("test-vehicle-01").length).toBeGreaterThan(0);
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

  it("describes the live deployment action and target-aware control", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    const preview = screen.getByTestId("deployment-spec-preview");
    const attemptId = screen.getByTestId("deployment-attempt-id").textContent;
    expect(attemptId).toMatch(/^demo-[a-z0-9-]+$/);
    expect(preview).toHaveTextContent(`"deployment_id": "${attemptId}"`);
    expect(preview).toHaveTextContent('"environment": "test"');
    expect(preview).toHaveTextContent('"implementation": "python"');
    expect(preview).toHaveTextContent('"strategy": "all_at_once"');
    expect(preview).toHaveTextContent('"failure_mode": "abort"');
    expect(screen.getByRole("heading", { name: "Configure a deployment" })).toBeInTheDocument();
    expect(screen.getByText("DeploymentSpec JSON")).toBeInTheDocument();
    expect(screen.getByTestId("deployment-capability-note")).toHaveTextContent("Rolling, canary, and rollback");
    expect(screen.getByText("Public simulated control plane")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy to 4 test vehicles" })).not.toBeDisabled();
    expect(screen.getByText("Simulated fleet")).toBeInTheDocument();
  });

  it("keeps configuration, the selected fleet, and the run context in a responsive layout", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    const layout = screen.getByTestId("fleet-deploy-layout");
    expect(layout).toContainElement(screen.getByTestId("deployment-config-column"));
    expect(layout).toContainElement(screen.getByTestId("fleet-state-column"));

    const testEnvironment = screen.getByTestId("fleet-environment-test");
    expect(within(testEnvironment).getAllByTestId("fleet-target-card")).toHaveLength(4);
    const specDisclosure = screen.getByTestId("deployment-spec-disclosure");
    expect(specDisclosure).toHaveProperty("open", false);
    expect(screen.getByText("DeploymentSpec JSON")).toBeInTheDocument();
    fireEvent.click(screen.getByText("DeploymentSpec JSON"));
    expect(specDisclosure).toHaveProperty("open", true);
  });

  it("submits only the canonical constrained payload without a browser credential", async () => {
    const snapshot = fleetSnapshot();
    const fetchMock = vi.fn((url, options) => {
      if (url.endsWith("/deployments/submit")) {
        const request = JSON.parse(options.body);
        return Promise.resolve({
          ok: true,
          json: async () => ({
            deployment_id: request.deployment_id,
            dispatch_status: "dispatched",
            workflow_url: "https://github.com/dhleach/homeops/actions/runs/456",
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => snapshot });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    fireEvent.click(screen.getByTestId("deploy-submit"));

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toBe("https://api.homeops.now/deploy/api/deployments/submit");
    expect(options.method).toBe("POST");
    expect(options.headers).toEqual({
      Accept: "application/json",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(options.body)).toEqual(expect.objectContaining({
      deployment_id: expect.stringMatching(/^demo-[a-z0-9-]+$/),
      environment: "test",
      implementation: "python",
      strategy: "all_at_once",
      failure_mode: "abort",
    }));
    expect(options.body).not.toContain("Authorization");
    expect(await screen.findByRole("status")).toHaveTextContent("accepted");
    const submittedId = JSON.parse(options.body).deployment_id;
    expect(submittedId).toMatch(/^demo-[a-z0-9-]+$/);
    expect(window.sessionStorage.getItem("homeops.activeFleetDeploymentId")).toBe(submittedId);
    fireEvent.click(screen.getByTestId("new-deployment"));
    await waitFor(() => {
      expect(screen.getByTestId("deployment-attempt-id").textContent).not.toBe(submittedId);
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    });
    expect(window.sessionStorage.getItem("homeops.activeFleetDeploymentId")).toBeNull();
  });

  it("keeps a generated attempt ID stable across a retry and isolates fresh visitors", async () => {
    const snapshot = fleetSnapshot();
    let submissionAttempts = 0;
    const fetchMock = vi.fn((url, options) => {
      if (url.endsWith("/deployments/submit")) {
        submissionAttempts += 1;
        if (submissionAttempts === 1) {
          return Promise.resolve({
            ok: false,
            status: 504,
            json: async () => ({ detail: "Gateway timeout" }),
          });
        }
        const request = JSON.parse(options.body);
        return Promise.resolve({
          ok: true,
          json: async () => ({ deployment_id: request.deployment_id, dispatch_status: "dispatched" }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => snapshot });
    });
    vi.stubGlobal("fetch", fetchMock);

    const first = render(<FleetDeployView apiUrl="https://api.homeops.now" />);
    await screen.findAllByTestId("fleet-target-card");
    const firstId = screen.getByTestId("deployment-attempt-id").textContent;
    fireEvent.click(screen.getByTestId("deploy-submit"));
    expect(await screen.findByTestId("retry-guidance")).toHaveTextContent("retained");
    expect(screen.getByRole("button", { name: "Retry same attempt" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Retry same attempt" }));
    await screen.findByRole("status");

    const submitCalls = fetchMock.mock.calls.filter(([url]) => url.endsWith("/deployments/submit"));
    expect(submitCalls).toHaveLength(2);
    expect(JSON.parse(submitCalls[0][1].body).deployment_id).toBe(firstId);
    expect(JSON.parse(submitCalls[1][1].body).deployment_id).toBe(firstId);
    expect(window.sessionStorage.getItem("homeops.activeFleetDeploymentId")).toBe(firstId);

    first.unmount();
    const reloaded = render(<FleetDeployView apiUrl="https://api.homeops.now" />);
    await screen.findAllByTestId("fleet-target-card");
    expect(screen.getByTestId("deployment-attempt-id").textContent).toBe(firstId);

    reloaded.unmount();
    window.sessionStorage.removeItem("homeops.fleetDeployAttemptId");
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);
    await screen.findAllByTestId("fleet-target-card");
    expect(screen.getByTestId("deployment-attempt-id").textContent).not.toBe(firstId);
  });

  it("validates explicit targets and exposes only supported capabilities", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    fireEvent.click(screen.getByRole("radio", { name: /individual targets/i }));
    expect(screen.getByTestId("deployment-validation-errors")).toHaveTextContent(
      "Select at least one simulated target",
    );
    expect(screen.getByRole("button", { name: "Select targets to deploy" })).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: "test-vehicle-01" }));
    expect(screen.queryByText("Select at least one simulated target.")).not.toBeInTheDocument();
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent('"target_ids": [');
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent('"test-vehicle-01"');
    expect(screen.getByRole("button", { name: "Deploy to 1 selected vehicle" })).not.toBeDisabled();

    expect(screen.getByLabelText("Strategy")).toHaveValue("all_at_once");
    expect(screen.getByLabelText("Failure mode")).toHaveValue("abort");
    expect(screen.queryByRole("option", { name: "rolling" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "canary" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "rollback" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Implementation"), { target: { value: "ansible" } });
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent('"implementation": "ansible"');
    expect(screen.getByRole("button", { name: "Deploy to 1 selected vehicle" })).not.toBeDisabled();
  });

  it("reconstructs a persisted deployment run from Actions and simulator state", async () => {
    const deployment = {
      simulated: true,
      target_kind: "simulated",
      deployment_id: "demo-reload-001",
      target_ids: [
        "test-vehicle-01",
        "test-vehicle-02",
        "test-vehicle-03",
        "test-vehicle-04",
      ],
      desired: { color: "green", shape: "square" },
      desired_digest: "green-square-digest",
      status: "succeeded",
      error: null,
      created_at: "2026-09-26T12:00:00Z",
      updated_at: "2026-09-26T12:00:30Z",
      verification: "verified",
      verified: true,
      targets: fleetSnapshot().targets.slice(0, 4).map((target) => ({
        ...target,
        desired: { color: "green", shape: "square" },
        desired_digest: "green-square-digest",
        observed: { color: "green", shape: "square" },
        observed_digest: "green-square-digest",
        status: "succeeded",
      })),
      manifest_commit_sha: "e".repeat(40),
      manifest_commit_url: "https://github.com/dhleach/homeops/commit/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      workflow_run_id: 789,
      workflow_url: "https://github.com/dhleach/homeops/actions/runs/789",
      workflow_status: "completed",
      workflow_conclusion: "success",
      workflow_created_at: "2026-09-26T12:00:01Z",
      workflow_updated_at: "2026-09-26T12:00:30Z",
      workflow: {
        id: 789,
        url: "https://github.com/dhleach/homeops/actions/runs/789",
        status: "completed",
        conclusion: "success",
        created_at: "2026-09-26T12:00:01Z",
        updated_at: "2026-09-26T12:00:30Z",
        jobs: [{
          id: 1,
          name: "Deploy immutable artifact to simulator",
          status: "completed",
          conclusion: "success",
          started_at: "2026-09-26T12:00:10Z",
          completed_at: "2026-09-26T12:00:29Z",
          url: "https://github.com/dhleach/homeops/actions/runs/789/job/1",
          failed_step: null,
        }],
      },
      dispatch_status: "dispatched",
      dispatch_error: null,
    };
    window.sessionStorage.setItem("homeops.activeFleetDeploymentId", "demo-reload-001");
    const fetchMock = vi.fn((url) => {
      if (url.includes("/deploy/api/deployments/")) {
        return Promise.resolve({ ok: true, json: async () => deployment });
      }
      return Promise.resolve({ ok: true, json: async () => fleetSnapshot() });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    expect(await screen.findByTestId("deployment-run-state")).toBeInTheDocument();
    expect(screen.getByText("4 / 4")).toBeInTheDocument();
    expect(screen.getByText("Deploy immutable artifact to simulator")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Manifest commit" })).toHaveAttribute(
      "href",
      deployment.manifest_commit_url,
    );
    expect(screen.getByTestId("fleet-state-column")).toContainElement(screen.getByTestId("deployment-run-state"));
    expect(screen.getByTestId("fleet-state-column")).toContainElement(screen.getByTestId("fleet-environment-test"));
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.homeops.now/deploy/api/deployments/demo-reload-001",
      expect.objectContaining({ cache: "no-store" }),
    );
  });
});
