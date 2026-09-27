import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
      { implementation: "python", strategy: "canary", failure_mode: "abort" },
      { implementation: "python", strategy: "canary", failure_mode: "rollback" },
      { implementation: "ansible", strategy: "all_at_once", failure_mode: "abort" },
    ],
    targets,
  };
}

function failedCanaryDeployment(deploymentId) {
  const targetIds = [
    "test-vehicle-01",
    "test-vehicle-02",
    "test-vehicle-03",
    "test-vehicle-04",
  ];
  const targets = fleetSnapshot().targets.slice(0, 4).map((target, index) => (
    index === 1
      ? {
        ...target,
        desired: { color: "orange", shape: "triangle" },
        desired_digest: "orange-triangle-digest",
        status: "failed",
        last_error: "deterministic verification failure",
      }
      : {
        ...target,
        status: index === 0 ? "succeeded" : "pending",
      }
  ));
  return {
    simulated: true,
    target_kind: "simulated",
    deployment_id: deploymentId,
    target_ids: targetIds,
    desired: { color: "orange", shape: "triangle" },
    desired_digest: "orange-triangle-digest",
    status: "failed",
    error: "deterministic verification failure injected for target test-vehicle-02",
    error_code: "verification_failed",
    error_recovery: "Compare desired and observed target state before retrying this request.",
    created_at: "2026-09-27T16:00:00Z",
    updated_at: "2026-09-27T16:00:20Z",
    verification: "failed",
    verified: false,
    targets,
    events: [
      {
        sequence: 1,
        event_type: "deployment_canary_started",
        schema_version: 1,
        artifact_sha256: "a".repeat(64),
        target_ids: ["test-vehicle-01"],
        status: "applying",
        detail: "verifying canary target test-vehicle-01",
        recorded_at: "2026-09-27T16:00:06Z",
      },
      {
        sequence: 2,
        event_type: "deployment_canary_verified",
        schema_version: 1,
        artifact_sha256: "a".repeat(64),
        target_ids: ["test-vehicle-01"],
        status: "applying",
        detail: "verified canary target test-vehicle-01",
        recorded_at: "2026-09-27T16:00:10Z",
      },
      {
        sequence: 3,
        event_type: "deployment_failed",
        schema_version: 1,
        artifact_sha256: "a".repeat(64),
        target_ids: ["test-vehicle-02"],
        status: "failed",
        detail: "readback failed for test-vehicle-02",
        recorded_at: "2026-09-27T16:00:18Z",
      },
    ],
    request_summary: {
      deployment_id: deploymentId,
      environment: "test",
      failure_mode: "abort",
      failure_target_id: "test-vehicle-02",
      implementation: "python",
      profile: { color: "orange", shape: "triangle" },
      schema_version: 1,
      strategy: "canary",
    },
    manifest_path: `manifests/${deploymentId}.json`,
    manifest_sha256: "a".repeat(64),
    manifest_commit_sha: "b".repeat(40),
    manifest_commit_url: "https://github.com/dhleach/homeops/commit/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    workflow_run_id: 901,
    workflow_url: "https://github.com/dhleach/homeops/actions/runs/901",
    workflow_status: "completed",
    workflow_conclusion: "failure",
    workflow_created_at: "2026-09-27T16:00:01Z",
    workflow_updated_at: "2026-09-27T16:00:19Z",
    workflow_error: null,
    workflow: {
      id: 901,
      url: "https://github.com/dhleach/homeops/actions/runs/901",
      status: "completed",
      conclusion: "failure",
      created_at: "2026-09-27T16:00:01Z",
      updated_at: "2026-09-27T16:00:19Z",
      jobs: [{
        id: 1,
        name: "Validate manifest and build profile artifact",
        status: "completed",
        conclusion: "failure",
        started_at: "2026-09-27T16:00:05Z",
        completed_at: "2026-09-27T16:00:08Z",
        url: "https://github.com/dhleach/homeops/actions/runs/901/job/1",
        failed_step: "Validate exact manifest before build gates",
        steps: [
          {
            number: 1,
            name: "Validate exact manifest before build gates",
            status: "completed",
            conclusion: "failure",
            started_at: "2026-09-27T16:00:06Z",
            completed_at: "2026-09-27T16:00:08Z",
          },
        ],
      }, {
        id: 2,
        name: "Deploy immutable artifact to simulator",
        status: "completed",
        conclusion: "skipped",
        started_at: null,
        completed_at: null,
        url: "https://github.com/dhleach/homeops/actions/runs/901/job/2",
        failed_step: null,
        steps: [],
      }],
    },
    dispatch_status: "dispatched",
    dispatch_error: null,
    rollback_status: "not_started",
    rollback_target_ids: [],
    rollback_error: null,
    retry_after_seconds: null,
  };
}

function resetDeployment(deploymentId, overrides = {}) {
  const snapshot = fleetSnapshot();
  const targetIds = snapshot.targets.map((target) => target.target_id);
  return {
    simulated: true,
    target_kind: "simulated",
    deployment_id: deploymentId,
    operation: "reset",
    target_ids: targetIds,
    desired: { color: "blue", shape: "circle" },
    desired_digest: "blue-circle-digest",
    status: "succeeded",
    error: null,
    created_at: "2026-09-27T17:00:00Z",
    updated_at: "2026-09-27T17:00:20Z",
    verification: "verified",
    verified: true,
    targets: snapshot.targets.map((target) => ({
      ...target,
      status: "succeeded",
      active_deployment_id: null,
    })),
    request_summary: {
      deployment_id: deploymentId,
      target_ids: targetIds,
      operation: "reset",
      profile: { color: "blue", shape: "circle" },
      schema_version: 1,
      implementation: "python",
      strategy: "all_at_once",
      failure_mode: "abort",
    },
    manifest_commit_url: "https://github.com/dhleach/homeops/commit/cccccccccccccccccccccccccccccccccccccccc",
    workflow_run_id: 902,
    workflow_url: "https://github.com/dhleach/homeops/actions/runs/902",
    workflow_status: "completed",
    workflow_conclusion: "success",
    workflow: {
      id: 902,
      url: "https://github.com/dhleach/homeops/actions/runs/902",
      status: "completed",
      conclusion: "success",
      created_at: "2026-09-27T17:00:01Z",
      updated_at: "2026-09-27T17:00:19Z",
      jobs: [],
    },
    dispatch_status: "dispatched",
    dispatch_error: null,
    rollback_status: "not_started",
    rollback_target_ids: [],
    rollback_error: null,
    ...overrides,
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
    window.sessionStorage.removeItem("homeops.previousFleetDeployment");
    window.history.replaceState({}, "", "/");
    document.title = "HomeOps";
  });

  it("renders all twelve simulated targets grouped by environment", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    expect(await screen.findAllByTestId("fleet-target-card")).toHaveLength(12);
    expect(screen.getByRole("heading", { name: "test" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "stage" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "prod" })).toBeInTheDocument();
    expect(screen.getAllByText("Observed profile")).toHaveLength(12);
    expect(screen.getAllByText("test-vehicle-01").length).toBeGreaterThan(0);
    expect(screen.getAllByText("blue circle")).toHaveLength(9);
    expect(screen.getAllByText("Desired = observed")).toHaveLength(12);
  });

  it("loads durable history and reopens the selected run", async () => {
    const deploymentId = "demo-history-001";
    const deployment = failedCanaryDeployment(deploymentId);
    const historyEntry = {
      deployment_id: deploymentId,
      operation: "deploy",
      selector: { environment: "test" },
      implementation: "python",
      artifact_sha256: "a".repeat(64),
      outcome: "failed",
      status: "failed",
      verification: "failed",
      created_at: deployment.created_at,
      updated_at: deployment.updated_at,
      manifest_commit_url: deployment.manifest_commit_url,
      workflow_run_id: deployment.workflow_run_id,
      workflow_url: deployment.workflow_url,
    };
    const fetchMock = vi.fn((url) => {
      if (url.includes("/deploy/api/deployments/history")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ simulated: true, target_kind: "simulated", deployments: [historyEntry] }),
        });
      }
      if (url.includes(`/deploy/api/deployments/${deploymentId}`)) {
        return Promise.resolve({ ok: true, json: async () => deployment });
      }
      return Promise.resolve({ ok: true, json: async () => fleetSnapshot() });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    expect(await screen.findByTestId("deployment-history-entry")).toHaveTextContent("demo-history-001");
    expect(screen.getByTestId("deployment-history-entry")).toHaveTextContent("TEST environment");
    expect(screen.getByRole("link", { name: "Actions run" })).toHaveAttribute(
      "href",
      deployment.workflow_url,
    );
    fireEvent.click(screen.getByTestId("deployment-history-row"));

    expect(await screen.findByTestId("deployment-run-state")).toHaveTextContent(deploymentId);
    expect(fetchMock.mock.calls.some(([url]) => url.includes(`/deployments/${deploymentId}`))).toBe(true);
  });

  it("submits fleet reset through the normal audited deployment path", async () => {
    const resetId = "demo-reset-ui-001";
    const completed = resetDeployment(resetId);
    const fetchMock = vi.fn((url, options) => {
      if (url.includes("/deploy/api/deployments/history")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ simulated: true, target_kind: "simulated", deployments: [] }),
        });
      }
      if (url.endsWith("/deployments/submit")) {
        const request = JSON.parse(options.body);
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ...completed,
            deployment_id: request.deployment_id,
            status: "queued",
            verification: "pending",
            verified: false,
            workflow_status: "in_progress",
            workflow_conclusion: null,
            workflow: null,
          }),
        });
      }
      if (url.includes("/deploy/api/deployments/")) {
        return Promise.resolve({ ok: true, json: async () => completed });
      }
      return Promise.resolve({ ok: true, json: async () => fleetSnapshot() });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);
    await screen.findAllByTestId("fleet-target-card");
    fireEvent.click(screen.getByTestId("fleet-reset"));

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/deployments/submit"))).toBe(true));
    const submitCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/deployments/submit"));
    const request = JSON.parse(submitCall[1].body);
    expect(request).toEqual(expect.objectContaining({
      operation: "reset",
      implementation: "python",
      strategy: "all_at_once",
      failure_mode: "abort",
    }));
    expect(request.target_ids).toHaveLength(12);
    expect(submitCall[1].headers).not.toHaveProperty("Authorization");
    expect(await screen.findByTestId("deployment-run-state")).toHaveTextContent(request.deployment_id);
  });

  it("keeps the narrow layout bounded and explains accessible control states", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    expect(screen.getByTestId("fleet-deploy-page")).toHaveClass("min-w-0", "overflow-x-clip");
    expect(screen.getByTestId("fleet-deploy-main")).toHaveClass("min-w-0", "px-4", "sm:px-6");
    expect(screen.getByTestId("deployment-config-panel")).toHaveClass("min-w-0");
    expect(screen.getByTestId("deployment-resolved-targets")).toHaveAttribute("aria-live", "polite");

    const specSummary = screen.getByText("DeploymentSpec JSON").closest("summary");
    expect(specSummary).toHaveClass("focus-visible:ring-2");

    fireEvent.click(screen.getByRole("radio", { name: /individual targets/i }));
    const environment = screen.getByLabelText("Environment");
    expect(environment).toBeDisabled();
    expect(environment).toHaveAttribute("aria-describedby", "deployment-environment-disabled-help");
    expect(screen.getByText("Switch to Environment target selection to choose a complete environment.")).toBeInTheDocument();

    const targetCheckboxes = screen.getAllByRole("checkbox");
    expect(targetCheckboxes).toHaveLength(12);
    expect(targetCheckboxes[0]).toHaveClass("focus-visible:ring-2");
  });

  it("keeps the main card content compact and reveals full identifiers on demand", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    expect(screen.getAllByTestId("fleet-vehicle-glyph")).toHaveLength(12);

    const firstCard = screen.getByTestId("fleet-environment-test").querySelector(
      '[data-target-id="test-vehicle-01"]',
    );
    expect(firstCard).not.toBeNull();
    expect(within(firstCard).getByRole("heading", { name: "TEST-01" })).toBeInTheDocument();
    expect(within(firstCard).getByText("Observed profile")).toBeInTheDocument();

    const details = within(firstCard).getByTestId("fleet-target-details");
    expect(details).not.toHaveAttribute("open");
    fireEvent.click(within(details).getByText("Full target details"));
    expect(details).toHaveAttribute("open");
    expect(within(details).getByText("test-vehicle-01")).toBeInTheDocument();
    expect(within(details).getAllByText("sha256:test-desired")).toHaveLength(2);
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

  it("identifies the deployment route and links to truthful source context", async () => {
    window.history.replaceState({}, "", "/deploy");

    render(<App />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "Fleet Deploy Lab" })).toBeInTheDocument());
    expect(document.title).toBe("Fleet Deploy Lab · HomeOps");
    expect(screen.getByRole("link", { name: "View frontend source" })).toHaveAttribute(
      "href",
      "https://github.com/dhleach/homeops/blob/master/dashboard/frontend/src/components/FleetDeployView.jsx",
    );
    expect(screen.getByRole("link", { name: "Frontend README" })).toHaveAttribute(
      "href",
      "https://github.com/dhleach/homeops/blob/master/dashboard/frontend/README.md",
    );
    expect(screen.queryByTestId("fleet-build-revision")).not.toBeInTheDocument();
  });

  it("keeps the implementation tabs tied to checked-in read-only source", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    const disclosure = screen.getByTestId("implementation-source-disclosure");
    expect(disclosure).not.toHaveAttribute("open");
    fireEvent.click(disclosure.querySelector("summary"));
    expect(disclosure).toHaveAttribute("open");
    expect(screen.getByRole("tab", { name: "Python deployer" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("implementation-source-code")).toHaveTextContent("class FleetDeployer");
    expect(screen.getByRole("link", { name: "Open Python deployer on GitHub" })).toHaveAttribute(
      "href",
      "https://github.com/dhleach/homeops/blob/master/deploy_demo/deployer.py",
    );
    expect(screen.getByTestId("implementation-source-code")).not.toHaveAttribute("contenteditable");

    fireEvent.click(screen.getByRole("tab", { name: "Ansible playbook" }));

    await waitFor(() => expect(screen.getByLabelText("Implementation")).toHaveValue("ansible"));
    expect(screen.getByRole("tab", { name: "Ansible playbook" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("implementation-source-code")).toHaveTextContent("ansible.builtin.assert");
    expect(screen.getByRole("link", { name: "Open Ansible playbook on GitHub" })).toHaveAttribute(
      "href",
      "https://github.com/dhleach/homeops/blob/master/ansible/deploy.yml",
    );
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent('"implementation": "ansible"');
  });

  it("collapses source by default and keeps the deploy control reachable through keyboard disclosure", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    const disclosure = screen.getByTestId("implementation-source-disclosure");
    const summary = disclosure.querySelector("summary");
    expect(summary).not.toBeNull();
    expect(disclosure).not.toHaveAttribute("open");
    expect(screen.getByTestId("deployment-config-panel")).toHaveClass("min-w-0");
    expect(screen.getByRole("button", { name: "Deploy to 4 test vehicles" })).toBeInTheDocument();

    summary.focus();
    fireEvent.keyDown(summary, { key: "Enter" });
    expect(disclosure).toHaveAttribute("open");
    expect(screen.getByRole("tab", { name: "Python deployer" })).toBeInTheDocument();

    const environment = screen.getByLabelText("Environment");
    fireEvent.click(screen.getByRole("tab", { name: "Ansible playbook" }));
    await waitFor(() => expect(screen.getByLabelText("Implementation")).toHaveValue("ansible"));
    expect(environment).toHaveValue("test");
    expect(screen.getByRole("button", { name: "Deploy to 4 test vehicles" })).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([url]) => url.endsWith("/deployments/submit"))).toHaveLength(0);
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
    expect(screen.getByTestId("deployment-capability-note")).toHaveTextContent("Python canary verifies the first target");
    expect(screen.getByText("Public simulated control plane")).toBeInTheDocument();
    expect(screen.getByTestId("fleet-read-boundary")).toHaveTextContent(
      "does not prove that the protected submission pipeline is healthy",
    );
    expect(screen.getByText("Fleet snapshot read")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy to 4 test vehicles" })).not.toBeDisabled();
    expect(screen.getByText("Simulated fleet")).toBeInTheDocument();
  });

  it("shows the resolved target set and highlights the matching fleet cards", async () => {
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    const resolvedTargets = screen.getByTestId("deployment-resolved-targets");
    expect(resolvedTargets).toHaveTextContent("4 vehicles");
    expect(resolvedTargets).toHaveTextContent("TEST-01, TEST-02, TEST-03, TEST-04");
    expect(screen.getAllByTestId("fleet-target-card").filter(
      (card) => card.dataset.selected === "true",
    )).toHaveLength(4);

    fireEvent.click(screen.getByRole("radio", { name: /individual targets/i }));
    await waitFor(() => {
      expect(resolvedTargets).toHaveTextContent("No targets selected");
      expect(screen.getAllByTestId("fleet-target-card").filter(
        (card) => card.dataset.selected === "true",
      )).toHaveLength(0);
    });
    expect(screen.getByRole("checkbox", { name: "TEST-01" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "TEST-01" }));
    await waitFor(() => {
      expect(resolvedTargets).toHaveTextContent("1 vehicle");
      expect(resolvedTargets).toHaveTextContent("TEST-01");
      expect(screen.getAllByTestId("fleet-target-card").filter(
        (card) => card.dataset.selected === "true",
      )).toHaveLength(1);
    });

    fireEvent.click(screen.getByRole("radio", { name: /^environment/i }));
    await waitFor(() => {
      expect(resolvedTargets).toHaveTextContent("TEST-01, TEST-02, TEST-03, TEST-04");
      expect(screen.getAllByTestId("fleet-target-card").filter(
        (card) => card.dataset.selected === "true",
      )).toHaveLength(4);
    });
    expect(screen.queryByRole("checkbox", { name: "TEST-01" })).not.toBeInTheDocument();
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
    const submitCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/deployments/submit"));
    expect(submitCall).toBeDefined();
    const [url, options] = submitCall;
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

  it("serializes a selected deterministic failure target in the constrained spec", async () => {
    const snapshot = fleetSnapshot();
    const fetchMock = vi.fn((url, options) => {
      if (url.endsWith("/deployments/submit")) {
        const request = JSON.parse(options.body);
        return Promise.resolve({
          ok: true,
          json: async () => ({ deployment_id: request.deployment_id, dispatch_status: "dispatched" }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => snapshot });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    await screen.findAllByTestId("fleet-target-card");
    fireEvent.change(screen.getByLabelText("Inject verification failure"), {
      target: { value: "test-vehicle-01" },
    });
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent(
      '"failure_target_id": "test-vehicle-01"',
    );
    fireEvent.click(screen.getByTestId("deploy-submit"));

    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/deployments/submit"))).toBe(true));
    const submitCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/deployments/submit"));
    expect(JSON.parse(submitCall[1].body)).toEqual(expect.objectContaining({
      failure_target_id: "test-vehicle-01",
    }));
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

    fireEvent.click(screen.getByRole("checkbox", { name: "TEST-01" }));
    expect(screen.queryByText("Select at least one simulated target.")).not.toBeInTheDocument();
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent('"target_ids": [');
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent('"test-vehicle-01"');
    expect(screen.getByRole("button", { name: "Deploy to 1 selected vehicle" })).not.toBeDisabled();

    expect(screen.getByLabelText("Strategy")).toHaveValue("all_at_once");
    expect(screen.getByLabelText("Failure mode")).toHaveValue("abort");
    expect(screen.queryByRole("option", { name: "rolling" })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "canary" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "rollback" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Implementation"), { target: { value: "ansible" } });
    expect(screen.getByTestId("deployment-spec-preview")).toHaveTextContent('"implementation": "ansible"');
    expect(screen.queryByRole("option", { name: "canary" })).not.toBeInTheDocument();
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
      request_summary: {
        deployment_id: "demo-reload-001",
        environment: "test",
        failure_mode: "abort",
        implementation: "python",
        profile: { color: "green", shape: "square" },
        schema_version: 1,
        strategy: "all_at_once",
      },
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
      if (url.includes("/deploy/api/deployments/") && !url.includes("/deploy/api/deployments/history")) {
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
    expect(screen.getByTestId("deployment-implementation-proof")).toHaveTextContent("Python deployer");
    expect(screen.getByRole("link", { name: "Run proof: Python deployer" })).toHaveAttribute(
      "href",
      deployment.workflow_url,
    );
    expect(screen.getByTestId("fleet-state-column")).toContainElement(screen.getByTestId("deployment-run-state"));
    expect(screen.getByTestId("fleet-state-column")).toContainElement(screen.getByTestId("fleet-environment-test"));
    expect(screen.getByTestId("deployment-workflow-status")).toHaveAttribute("aria-live", "polite");
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.homeops.now/deploy/api/deployments/demo-reload-001",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("shows verified rollback while keeping the deployment visibly failed", async () => {
    const deployment = {
      simulated: true,
      target_kind: "simulated",
      deployment_id: "demo-rollback-ui-001",
      target_ids: [
        "test-vehicle-01",
        "test-vehicle-02",
        "test-vehicle-03",
        "test-vehicle-04",
      ],
      desired: { color: "orange", shape: "triangle" },
      desired_digest: "orange-triangle-digest",
      status: "failed",
      error: "deterministic verification failure injected for target test-vehicle-02",
      error_code: "verification_failed",
      error_recovery: "Deployment remains failed; rollback verified the changed simulated targets against their pre-deployment profiles.",
      request_summary: {
        deployment_id: "demo-rollback-ui-001",
        environment: "test",
        failure_mode: "rollback",
        failure_target_id: "test-vehicle-02",
        implementation: "python",
        profile: { color: "orange", shape: "triangle" },
        schema_version: 1,
        strategy: "canary",
      },
      created_at: "2026-09-27T15:00:00Z",
      updated_at: "2026-09-27T15:00:20Z",
      verification: "failed",
      verified: false,
      rollback_status: "succeeded",
      rollback_target_ids: ["test-vehicle-01"],
      rollback_error: null,
      targets: fleetSnapshot().targets.slice(0, 4).map((target, index) => ({
        ...target,
        desired: index === 0 ? target.observed : { color: "orange", shape: "triangle" },
        desired_digest: index === 0 ? target.observed_digest : "orange-triangle-digest",
        observed: index === 0 ? target.observed : target.observed,
        observed_digest: index === 0 ? target.observed_digest : target.observed_digest,
        status: index === 0 ? "ready" : index === 1 ? "failed" : "pending",
        active_deployment_id: index === 0 ? null : "demo-rollback-ui-001",
      })),
      dispatch_status: "failed",
      dispatch_error: null,
    };
    window.sessionStorage.setItem("homeops.activeFleetDeploymentId", "demo-rollback-ui-001");
    vi.stubGlobal("fetch", vi.fn((url) => {
      if (url.includes("/deploy/api/deployments/") && !url.includes("/deploy/api/deployments/history")) {
        return Promise.resolve({ ok: true, json: async () => deployment });
      }
      return Promise.resolve({ ok: true, json: async () => fleetSnapshot() });
    }));

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    const rollback = await screen.findByTestId("deployment-rollback-state");
    expect(rollback).toHaveTextContent("Rollback: Verified");
    expect(rollback).toHaveTextContent("Deployment remains failed");
    expect(screen.getByTestId("deployment-run-state")).toHaveTextContent("Failed");
  });

  it("keeps a frozen request summary visible and polls until workflow jobs converge", async () => {
    const inProgress = {
      simulated: true,
      target_kind: "simulated",
      deployment_id: "demo-converge-001",
      target_ids: ["test-vehicle-01", "test-vehicle-02"],
      desired: { color: "green", shape: "square" },
      desired_digest: "green-square-digest",
      status: "succeeded",
      error: null,
      request_summary: {
        deployment_id: "demo-converge-001",
        environment: "test",
        failure_mode: "abort",
        implementation: "python",
        profile: { color: "green", shape: "square" },
        schema_version: 1,
        strategy: "all_at_once",
      },
      created_at: "2026-09-26T12:00:00Z",
      updated_at: "2026-09-26T12:00:30Z",
      verification: "verified",
      verified: true,
      targets: fleetSnapshot().targets.slice(0, 2).map((target) => ({
        ...target,
        desired: { color: "green", shape: "square" },
        desired_digest: "green-square-digest",
        observed: { color: "green", shape: "square" },
        observed_digest: "green-square-digest",
        status: "succeeded",
      })),
      manifest_commit_url: "https://github.com/dhleach/homeops/commit/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      workflow_run_id: 790,
      workflow_url: "https://github.com/dhleach/homeops/actions/runs/790",
      workflow_status: "in_progress",
      workflow_conclusion: null,
      workflow_created_at: "2026-09-26T12:00:01Z",
      workflow_updated_at: "2026-09-26T12:00:30Z",
      workflow: {
        id: 790,
        url: "https://github.com/dhleach/homeops/actions/runs/790",
        status: "in_progress",
        conclusion: null,
        created_at: "2026-09-26T12:00:01Z",
        updated_at: "2026-09-26T12:00:30Z",
        jobs: [{
          id: 2,
          name: "Deploy immutable artifact to simulator",
          status: "in_progress",
          conclusion: null,
          started_at: "2026-09-26T12:00:10Z",
          completed_at: null,
          url: "https://github.com/dhleach/homeops/actions/runs/790/job/2",
          failed_step: null,
        }],
      },
      dispatch_status: "dispatched",
      dispatch_error: null,
      error_code: null,
      error_recovery: null,
    };
    const completed = {
      ...inProgress,
      workflow_status: "completed",
      workflow_conclusion: "success",
      workflow_updated_at: "2026-09-26T12:00:45Z",
      workflow: {
        ...inProgress.workflow,
        status: "completed",
        conclusion: "success",
        updated_at: "2026-09-26T12:00:45Z",
        jobs: [{
          ...inProgress.workflow.jobs[0],
          status: "completed",
          conclusion: "success",
          completed_at: "2026-09-26T12:00:44Z",
        }],
      },
    };
    window.sessionStorage.setItem("homeops.activeFleetDeploymentId", "demo-converge-001");
    let deploymentReads = 0;
    const fetchMock = vi.fn((url) => {
      if (url.includes("/deploy/api/deployments/") && !url.includes("/deploy/api/deployments/history")) {
        deploymentReads += 1;
        return Promise.resolve({
          ok: true,
          json: async () => (deploymentReads === 1 ? inProgress : completed),
        });
      }
      return Promise.resolve({ ok: true, json: async () => fleetSnapshot() });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    expect(await screen.findByTestId("workflow-convergence")).toHaveTextContent("Waiting for the final workflow conclusion");
    expect(screen.getByTestId("deployment-request-summary")).toHaveTextContent(
      "TEST environment (four vehicles) · python / all_at_once / abort",
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2_100));
    });
    await waitFor(() => expect(screen.queryByTestId("workflow-convergence")).not.toBeInTheDocument());
    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(deploymentReads).toBeGreaterThanOrEqual(2);
  });

  it("keeps failed canary evidence active while editing, survives reload, and submits only the next attempt", async () => {
    const failedId = "demo-failed-canary-002";
    const failedDeployment = failedCanaryDeployment(failedId);
    const submitCalls = [];
    const fetchMock = vi.fn((url, options) => {
      if (url.endsWith("/deployments/submit")) {
        const request = JSON.parse(options.body);
        submitCalls.push(request);
        const nextDeployment = failedCanaryDeployment(request.deployment_id);
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ...nextDeployment,
            status: "queued",
            error: null,
            error_code: null,
            error_recovery: null,
            verification: "pending",
            verified: false,
            dispatch_status: "dispatched",
            workflow_run_id: null,
            workflow_url: null,
            workflow_status: null,
            workflow_conclusion: null,
            workflow: null,
          }),
        });
      }
      if (url.includes("/deploy/api/deployments/") && !url.includes("/deploy/api/deployments/history")) {
        const requestedId = decodeURIComponent(url.split("/").pop());
        return Promise.resolve({
          ok: true,
          json: async () => requestedId === failedId
            ? failedDeployment
            : { ...failedDeployment, deployment_id: requestedId },
        });
      }
      return Promise.resolve({ ok: true, json: async () => fleetSnapshot() });
    });
    vi.stubGlobal("fetch", fetchMock);
    window.sessionStorage.setItem("homeops.activeFleetDeploymentId", failedId);
    window.sessionStorage.setItem("homeops.fleetDeployAttemptId", failedId);

    const first = render(<FleetDeployView apiUrl="https://api.homeops.now" />);
    expect(await screen.findByTestId("deployment-run-state")).toHaveTextContent(failedId);
    expect(screen.getByTestId("deployment-run-state")).toHaveTextContent("Verification failed");
    expect(screen.getByTestId("deployment-timeline")).toBeInTheDocument();
    expect(screen.getByTestId("github-actions-timeline")).toHaveTextContent("GitHub Actions jobs and steps");
    expect(screen.getByTestId("github-actions-timeline")).toHaveTextContent("Validate exact manifest before build gates");
    expect(screen.getByTestId("workflow-job-skipped")).toHaveTextContent("Skipped because an earlier GitHub Actions job did not complete successfully");
    expect(screen.getByTestId("deployment-events-timeline")).toHaveTextContent("Canary Started");
    expect(screen.getByTestId("deployment-events-timeline")).toHaveTextContent("TEST-02");

    fireEvent.change(screen.getByLabelText("Profile color"), { target: { value: "green" } });

    expect(await screen.findByTestId("active-run-preserved")).toHaveTextContent("has not submitted");
    expect(screen.getByTestId("deployment-run-state")).toBeInTheDocument();
    expect(screen.getByTestId("previous-attempt-execution")).toHaveTextContent("python / canary");
    expect(screen.getByTestId("previous-attempt-targets")).toHaveTextContent("TEST-01, TEST-02, TEST-03, TEST-04");
    expect(screen.getByTestId("previous-attempt-workflow")).toHaveTextContent("completed · failure");
    expect(screen.getByTestId("previous-attempt-verification")).toHaveTextContent("failed");
    expect(screen.getByRole("link", { name: "View previous manifest commit" })).toHaveAttribute(
      "href",
      failedDeployment.manifest_commit_url,
    );
    expect(submitCalls).toHaveLength(0);

    const persistedPrevious = JSON.parse(window.sessionStorage.getItem("homeops.previousFleetDeployment"));
    expect(persistedPrevious).toEqual(expect.objectContaining({
      deployment_id: failedId,
      status: "failed",
      error_code: "verification_failed",
      manifest_commit_url: failedDeployment.manifest_commit_url,
      workflow_conclusion: "failure",
      target_ids: failedDeployment.target_ids,
    }));

    first.unmount();
    render(<FleetDeployView apiUrl="https://api.homeops.now" />);

    expect(await screen.findByTestId("active-run-preserved")).toBeInTheDocument();
    expect(screen.getByTestId("previous-attempt-error")).toHaveTextContent("Verification failed");
    expect(screen.getByTestId("previous-attempt-workflow")).toHaveTextContent("completed · failure");

    fireEvent.click(screen.getByTestId("deploy-submit"));
    await waitFor(() => expect(submitCalls).toHaveLength(1));
    expect(submitCalls[0].deployment_id).not.toBe(failedId);
    expect(screen.getByTestId("previous-attempt")).toHaveTextContent(failedId);
    expect(screen.getByTestId("previous-attempt")).toHaveTextContent("View previous workflow run");
  });

  it("classifies busy submissions and keeps their result under Previous attempt when edited", async () => {
    const fetchMock = vi.fn((url) => {
      if (url.endsWith("/deployments/submit")) {
        return Promise.resolve({
          ok: false,
          status: 429,
          headers: new Headers({ "Retry-After": "12" }),
          json: async () => ({
            detail: {
              code: "capacity_full",
              message: "Fleet deployment capacity is temporarily full.",
              recovery: "Wait for the active run to finish, then retry the same attempt.",
              retry_after_seconds: 12,
            },
          }),
        });
      }
      return Promise.resolve({ ok: true, json: async () => fleetSnapshot() });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<FleetDeployView apiUrl="https://api.homeops.now" />);
    await screen.findAllByTestId("fleet-target-card");
    const attemptId = screen.getByTestId("deployment-attempt-id").textContent;
    fireEvent.click(screen.getByTestId("deploy-submit"));

    expect(await screen.findByTestId("submission-error-class")).toHaveTextContent("Fleet busy");
    expect(screen.getByTestId("retry-guidance")).toHaveTextContent("12 seconds");
    fireEvent.change(screen.getByLabelText("Profile color"), { target: { value: "green" } });

    expect(await screen.findByTestId("previous-attempt")).toBeInTheDocument();
    expect(screen.getByTestId("previous-attempt")).toHaveTextContent(attemptId);
    expect(screen.getByTestId("previous-attempt-error")).toHaveTextContent("Fleet busy");
  });
});
