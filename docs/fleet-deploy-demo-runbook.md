# Fleet Deploy Lab demo and interview runbook

The Fleet Deploy Lab is the recruiter-facing deployment-infrastructure demo at
[`https://homeops.now/deploy`](https://homeops.now/deploy). It is a bounded,
anonymous control plane for twelve **simulated vehicles**. Use this runbook for
a short Latitude AI deployment-infrastructure walkthrough and for the release
checks before sharing the link.

The demo is intentionally separate from the real HomeOps HVAC release path.
It is not hardware-in-the-loop, ECU control, an autonomous-vehicle fleet, or a
deployment to a real vehicle. The production HomeOps dashboard and its Pi,
EC2, thermostat, and telemetry state remain outside this control plane.

## The 60–90 second story

> “The browser can submit only a closed deployment contract for a fixed set of
> simulated targets. The server commits the validated manifest and dispatches a
> trusted `master` workflow. That workflow validates an immutable artifact,
> runs one selected implementation, and requires fresh observed-state
> readback. The UI shows the actual Actions steps and simulator events. A
> deliberate verification failure demonstrates that the system stops, reports
> drift, and rolls back only what changed instead of claiming success.”

Lead with the boundary. Do not call the targets “cars in production,” and do
not imply that the demo proves hardware, ECU, HIL, or vehicle-network behavior.

## Architecture and code paths

```text
anonymous browser
  └─ /deploy (React/Vite)
       └─ closed DeploymentSpec JSON
            └─ FastAPI /deploy/api/deployments/submit
                 ├─ SQLite admission/history + manifest commit
                 └─ trusted master workflow_dispatch
                      ├─ validate manifest and build profile artifact
                      └─ Python or Ansible deployer
                           └─ protected simulator API
                                └─ fresh public readback + durable evidence
```

| Concern | Source of truth | Interview point |
| --- | --- | --- |
| Browser route and evidence UI | [`FleetDeployView.jsx`](../dashboard/frontend/src/components/FleetDeployView.jsx) | The visitor sees fixed targets, request parameters, real workflow steps, simulator events, and verified readback. |
| API boundary | [`fleet_api.py`](../dashboard/backend/fleet_api.py) | Public reads are separate from bearer-protected simulator writes. Submission accepts only the shared contract. |
| Contract and canonical JSON | [`deployment_spec.py`](../deploy_demo/deployment_spec.py) | Environment, target, implementation, strategy, failure mode, profile, and reset values are finite and validated in one place. |
| Durable simulator state | [`fleet_state.py`](../deploy_demo/fleet_state.py) | Desired and observed profiles remain distinct; history and reservations survive a backend restart. |
| Python implementation | [`deployer.py`](../deploy_demo/deployer.py) | Queues, applies, verifies, records lifecycle events, and performs bounded canary rollback. |
| Ansible implementation | [`deploy.yml`](../ansible/deploy.yml) | Uses the same validated spec and protected simulator contract for the verified all-at-once path. |
| Trust boundary | [`fleet-deploy.yml`](../.github/workflows/fleet-deploy.yml) | Trusted `master` code reads the manifest as data, creates the artifact, and receives only the simulator write credential in the deploy job. |
| Public release gate | [`deploy_smoke_check.py`](../scripts/deploy_smoke_check.py) | Checks `/deploy`, simulator readiness, all twelve simulated labels, and durable history in addition to the normal HomeOps surfaces. |

## Preconditions

Run the public checks from a machine with Python 3.11+ and no credentials in
the shell history:

```bash
curl -fsS https://homeops.now/deploy >/dev/null
curl -fsS https://api.homeops.now/health
curl -fsS https://api.homeops.now/deploy/api/health
curl -fsS https://api.homeops.now/deploy/api/fleet >/dev/null
python3 scripts/deploy_smoke_check.py --skip-observability
```

Open `/deploy` in an incognito/private window. No login is required. Confirm
the page says **Fleet Deploy Lab**, **12 simulated vehicles**, and **Public
simulated control plane**. If the newest history row is still `Applying`, or
the cards show an old failed/canary run, do not narrate that as a healthy demo:
use **Reset simulated fleet** first and wait for the run to reach its terminal
readback. Reset uses the same manifest/workflow/apply/readback path as a normal
deployment and is capped at one request per UTC day by default.

The ordinary HomeOps release gate is separate. A successful Fleet read does not
prove the protected submission pipeline is healthy, and a healthy Fleet demo
does not prove the HVAC telemetry or normal Pi/EC2 release path is healthy.

## Normal success walkthrough

1. Choose **Environment** and `test` so the resolved target summary shows
   `TEST-01` through `TEST-04`.
2. Choose `python`, **all at once**, and `abort`. Leave failure injection off.
   Use the default profile or choose a visible profile such as green/square.
3. Read the resolved target list and the `DeploymentSpec` disclosure before
   selecting **Deploy to 4 test vehicles**.
4. Follow the run panel through queued/applying, the GitHub Actions run, the
   Python deployer events, and the fresh observed-state readback.
5. Point out that success is not a timer or a browser guess: the run is green
   only after the Actions/deployer evidence and target digests agree.
6. Open the exact Actions run and, after returning to the page, select the
   history row to show that the durable run can be reopened after reload.

Expected evidence:

- The manifest commit and exact workflow link are visible.
- Actions jobs and server-recorded simulator events are separate timelines.
- Desired and observed profile labels/digests converge for all four targets.
- The UI identifies the target kind as simulated and never asks the browser for
  a GitHub, Fleet API, Home Assistant, Pi, or EC2 credential.

## Verification-failure and rollback walkthrough

Start from the baseline if possible; otherwise choose a target set that is not
already reserved by an active run.

1. Choose `test`, `python`, **canary**, and **rollback**.
2. Select a failure target such as `TEST-02` in **Inject verification failure**.
3. Submit a visible profile change and watch the first target apply before the
   selected target fails verification.
4. Explain the resulting evidence: later canary targets remain pending, the
   selected target remains visibly divergent/failed, and the deployer restores
   only targets that actually changed. The overall run remains failed even when
   rollback is verified; a partial or unverified rollback stays visible as
   partial state.
5. Expand the Actions and deployer-event timelines and point to the failed
   verification evidence rather than treating an HTTP 200 or a completed timer
   as success.

This path is deterministic and bounded to the fixed simulated target set. It
does not call Home Assistant, alter a thermostat, mutate HVAC telemetry, or
run a real deployment.

## Reset and recovery walkthrough

The **Reset simulated fleet** control submits the closed full-fleet
`operation: "reset"` spec through the ordinary path. It does not write the
SQLite database directly from the browser. The trusted workflow restores each
target's deterministic baseline only after it reaches the protected simulator
apply step, then the public readback proves the result.

Use reset when:

- a prior failure left desired/observed drift that would confuse the next demo;
- a demo was interrupted while the browser was closed or reloaded; or
- the interview walkthrough needs a clean starting state.

The default cap is one reset per UTC day. An active run takes precedence and
must reach a terminal state before a new reset can be admitted. If the cap or
busy-fleet guard rejects the request, explain the explicit recovery state and
do not bypass it by calling protected endpoints from the browser.

## Security and public-boundary talking points

- The browser sends only the finite `DeploymentSpec`; it cannot choose a
  repository, branch, path, URL, command, or credential.
- `FLEET_DEPLOY_GITHUB_TOKEN` stays server-side for manifest Contents writes and
  workflow dispatch. `FLEET_DEPLOY_API_KEY` is a separate protected simulator
  write credential supplied only to the trusted workflow/deployer boundary.
- Neither Fleet credential is a Vite variable, frontend asset, public API
  response, normal HomeOps deployment credential, Home Assistant token, or
  diagnostic/OIDC credential.
- The workflow checks out trusted `master` code and reads the writable manifest
  branch as an ancestry-checked Git object. It validates the exact canonical
  manifest before building or executing the artifact.
- The deployer requires a fresh simulator readback of desired/observed profile
  digests. It never turns a transport success into a deployment success.
- Public reads are anonymous and useful for the demo; protected queue/apply and
  lifecycle-event writes require the dedicated bearer boundary.

Never paste a secret into the browser, a public issue, a screenshot, or the
runbook. If a credential or workflow result is needed for discussion, show the
redacted GitHub run metadata and the public evidence fields only.

## Known limitations and honest answers

- The twelve targets are logical simulated vehicles. This demo does not prove
  HIL, ECU, CAN, firmware, physical actuator, network-partition, or real-fleet
  behavior.
- GitHub Actions latency and availability are part of the demo experience; the
  UI exposes queued/running/failed state instead of hiding the delay.
- The public Fleet read can be healthy while protected submission, GitHub
  dispatch, or the workflow is unavailable. The page says this explicitly.
- The history list is deliberately bounded. It is evidence for the demo, not a
  long-term deployment audit system.
- Reset is conservative and limited to one request per UTC day. It is a
  recovery affordance, not an operator bypass.
- Ansible currently exposes only its verified all-at-once/abort capability;
  Python owns the proven canary and rollback paths.
- The normal HomeOps release workflow, HVAC telemetry, Home Assistant, Pi, and
  EC2 deployment state are separate systems and are not exercised by this
  simulated-fleet control plane.

## Verification checklist and evidence record

Local source checks:

```bash
PYTHONPATH=services/consumer:services/observer:services/insights:dashboard/backend:scripts \
  python3 -m pytest --import-mode=importlib \
  deploy_demo/tests dashboard/backend/tests scripts/tests

(cd dashboard/frontend && NODE_ENV=test npm test)
~/.local/bin/ruff check services/
~/.local/bin/ruff format --check services/
```

For a release handoff, record all of the following:

| Evidence | Required result |
| --- | --- |
| `https://homeops.now/deploy` | HTTP 200 and the Fleet SPA renders anonymously. |
| `https://api.homeops.now/deploy/api/health` | `status=ok`, `simulated=true`, `target_count=12`. |
| `https://api.homeops.now/deploy/api/fleet` | Twelve unique `TEST`/`STAGE`/`PROD` simulated labels. |
| `/deploy/api/deployments/history` | HTTP 200 and a bounded simulated history list. |
| Desktop browser | Use 1440×900 or similar; no horizontal overflow, console errors, or failed requests. |
| Phone browser | Use 390×844 or similar; no horizontal overflow, console errors, or failed requests. |
| Keyboard pass | Every interactive control has visible focus; source/spec disclosures open without losing form state. |
| Boundary review | No public page, asset, response, log, or screenshot contains a Fleet credential. |
| Normal HomeOps gate | `scripts/deploy_smoke_check.py` passes; do not substitute a healthy Fleet read for this check. |

The last anonymous browser rehearsal on the merged PR19 snapshot (`299e5d7`)
passed at 1440×900 and 390×844: the route title was
`Fleet Deploy Lab · HomeOps`, the page rendered all required markers, both
viewports had `scrollWidth == innerWidth`, and the browser reported zero
console errors and failed requests. Rerun the checks after each frontend or
backend deployment; this historical evidence does not replace a current smoke
check.

See the [integration map](../deploy-demo/README.md) for the full historical
PR sequence and the [deployment guide](deployment.md) for the normal HomeOps
release boundary.
