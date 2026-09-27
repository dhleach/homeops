# Fleet Deploy Lab integration map

Status: PR 19 deployment history and auditable reset in progress; PR 18 merged
Repository: `dhleach/homeops`
Default branch: `master`
Latest merged integration snapshot: `master` after PR #399
Active GitHub issue: https://github.com/dhleach/homeops/issues/400

This document records the real HomeOps integration points for the Fleet Deploy
Lab as the implementation advances. It is deliberately specific about what
exists now, what is planned for later PRs, and what still needs an operator
decision.

## Purpose and boundary

The Fleet Deploy Lab provides a public simulated-fleet deployment demo at
`homeops.now/deploy`. A visitor configures and submits a constrained deployment
spec; the backend writes a validated manifest; a workflow running trusted
`master` validates the manifest and builds an immutable profile artifact; and a
Python or Ansible deployer updates twelve logical simulated vehicles.

The lab is not a real Home Assistant deployment, a second production release
path, hardware-in-the-loop, ECU control, or an autonomous-vehicle fleet. The
normal HomeOps Pi, EC2, frontend, and observability deployments remain separate.

## Current route map

| Surface | Current path | Current owner | Current behavior | Fleet Deploy Lab target |
| --- | --- | --- | --- | --- |
| Public frontend | `https://homeops.now/deploy` | CloudFront → private S3 → React/Vite SPA | CloudFront serves the SPA shell. The current Fleet Deploy Lab presents a bounded control plane with live desired/observed TEST/STAGE/PROD cards, a constrained DeploymentSpec form, and a target-aware deploy action. | The browser sends only the closed contract; server-side GitHub credentials remain in the backend. |
| Existing backend liveness | `https://api.homeops.now/health` | Nginx → FastAPI | Returns `{"status":"ok"}`. | Remains the process liveness check. |
| Fleet demo health | `https://api.homeops.now/deploy/api/health` | Nginx → FastAPI | Implemented by merged PR 04; availability follows the normal backend deployment. | Public simulator readiness and target-count check. |
| Existing telemetry | `https://api.homeops.now/api/current-temps` | FastAPI → EC2-local Prometheus | Current production telemetry contract. | Must remain unchanged. |
| Existing diagnostic | `https://api.homeops.now/api/diagnostic` | FastAPI → authenticated provider path | Authenticated, quota-limited, read-only HVAC diagnostics. | Must remain separate from Fleet Deploy credentials and state. |

The `/deploy/api/*` prefix is intentionally on `api.homeops.now`, not under
the CloudFront frontend origin. The browser page at `homeops.now/deploy` will
call the API using the existing `VITE_API_URL=https://api.homeops.now` build
configuration. Nginx already proxies the default API location and allows
`GET`, `POST`, and `OPTIONS` for the `homeops.now` origin.

### Route smoke contract

The following checks are the intended public smoke contract. All three are
valid after the merged PR 04 backend deployment and PR 05 frontend deployment;
the Fleet Deploy route also exposes the bounded configuration and submission
control plane described below.

```bash
curl -fsS https://homeops.now/deploy >/dev/null
curl -fsS https://api.homeops.now/health
curl -fsS https://api.homeops.now/deploy/api/health
```

For a discovery-only run against the current production deployment, the third
check should be recorded as unavailable only when the backend deployment has
not yet completed. Once PR 04 is deployed, it is a required 200/readiness check,
followed by the anonymous `/deploy/api/fleet` read.

## Frontend and hosting boundary

| Concern | Source of truth | Production path |
| --- | --- | --- |
| React entrypoint | `dashboard/frontend/src/main.jsx` and `dashboard/frontend/src/App.jsx` | Vite build output in `dashboard/frontend/dist/` |
| Frontend build | `dashboard/frontend/package.json` | `npm ci` then `npm run build` |
| Frontend workflow | `.github/workflows/frontend-deploy.yml` | Runs on `master` changes under `dashboard/frontend/**` or the workflow itself |
| Static origin | `infra/s3.tf` | Private S3 bucket `homeops-frontend-production` |
| Public delivery | `infra/cloudfront.tf` | CloudFront alias `homeops.now`, OAC to S3, invalidation after sync |
| SPA routing | CloudFront Function `homeops-spa-router-production` | Extensionless paths are rewritten to `/index.html`; `/bob/evals` has an explicit static exception |
| Frontend smoke | `scripts/deploy_smoke_check.py` | Runs after the frontend workflow and checks the public site/API surfaces |

CloudFront already makes `/deploy` reachable as an SPA path; no new DNS record,
S3 origin, certificate, or Terraform route is needed. The frontend application
now performs the explicit route-aware rendering in `App.jsx` and
`FleetDeployView.jsx`. The module is included in the existing Vite build and
frontend workflow; it is not published as an unrelated static site.

## Fleet Deploy form — constrained controls and deployment specification

The public `/deploy` route offers only the finite environments, simulated target
IDs, profile colors and shapes, Python/Ansible implementations, strategies, and
failure modes defined by the shared `deploy_demo.deployment_spec` contract. It
also offers an optional selected-target switch for the deterministic simulated
verification failure path; that switch names only one logical fleet vehicle and
cannot carry a command, URL, path, or credential. It
generates the deployment attempt ID automatically instead of asking a visitor
to invent one, keeps that ID in tab-scoped session storage across reloads, and
shows the exact deployment specification that will be submitted. A retry keeps
the same ID and unchanged spec; changing an already-submitted configuration or
starting another deployment rotates to a new ID. The backend's durable
admission boundary makes an unchanged replay idempotent while rejecting a
different spec under an existing ID.

Submitting creates a validated manifest and dispatches the trusted workflow.
The browser accepts no repository path, URL, command, arbitrary code, or
credential; GitHub and protected simulator credentials remain server-side.

## REV 07 — resolved target selection

The form now makes the actual target set visible before submission. Environment
mode summarizes the four resolved vehicles with short labels such as `TEST-01`,
while individual mode exposes the same short labels and keeps stable machine IDs
in the DeploymentSpec details. The Fleet Deploy cards highlight the targets in
the current resolved set. Switching modes clears inactive individual selection,
so the summary, highlighted cards, and submitted payload cannot disagree about
which vehicles will deploy.

## REV 08 — actionable submission and failure status

The public route now persists the validated request summary with each
deployment record and displays that frozen request alongside the run state. The
submission boundary classifies busy-fleet, cooldown, validation, service,
manifest-commit, and workflow-dispatch failures with concrete recovery guidance
that never exposes provider credentials. Editing a failed or completed form
creates a new attempt while preserving the prior result in an explicit
Previous attempt area.

Editing a setting on a run that is still being reconciled rotates only the next
form attempt ID; it does not clear the tracked run panel or submit a duplicate.
The existing run remains server-backed and continues polling while the visitor
prepares the next request. The bounded Previous attempt record preserves the
status, dispatch/workflow conclusion, error and recovery evidence, selected
implementation and strategy, target set and verification summary, timestamps,
and manifest/workflow links through a reload.

Run reconciliation remains server-backed and idempotent. The browser keeps
polling after simulator state becomes terminal until the workflow conclusion
and its job rows are both current, so a stale Running row cannot survive behind
a terminal Succeeded label. The same active run identity and waiting state are
recoverable after reload.

- Terraform apply required: **No**
- Manual console/setup: **None**
- Terraform resources changed: **None**
- Safety gate: no browser credential, provider response, Home Assistant state,
  thermostat, Pi/EC2 deployment, or normal release path is exposed or changed.

## REV 09 — compact vehicle-themed fleet cards

The fleet snapshot now presents each target as a compact vehicle-themed card.
The short target label, observed profile, and health state lead the card, while
the desired-versus-observed comparison remains explicit and uses text as well
as visual treatment so it does not depend on color perception. Full machine
identifiers and profile digests remain available in an on-demand details
disclosure. The vehicle silhouette is decorative presentation only; it does
not change the `DeploymentSpec`, fleet API payload, or reported simulator state.

- Terraform apply required: **No**
- Manual console/setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the frontend-only implementation.
- Safety gate: no API schema, credential, workflow, Home Assistant, thermostat,
  Pi/EC2 deployment, or normal release path is changed.

## DEFECT 03 — collapsed implementation source disclosure

The implementation source view is collapsed on first load so the resolved
targets and Deploy control remain visible without excessive scrolling. The
native disclosure remains keyboard and pointer accessible; opening it exposes
the commit-pinned Python/Ansible tabs and read-only source without changing the
trusted implementation selection or submitting the form. Desktop and narrow
viewport regression coverage protects the initial collapsed state, disclosure,
source-tab access, and Deploy-button visibility.

- Terraform apply required: **No**
- Manual console/setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the frontend-only implementation.
- Safety gate: no API schema, credential, workflow, Home Assistant, thermostat,
  Pi/EC2 deployment, or normal release path is changed.

## REV 05 — generated request identifiers and safe retries

The Fleet Deploy form now generates a unique attempt ID with browser-side
randomness (using cryptographic APIs when available) and stores it only in the
current tab. Network errors
and dispatch failures leave the attempt available through an explicit
`Retry same attempt` action, so a double click or retry cannot create a second
manifest for the same spec. A reload does not submit anything automatically;
it reconstructs the same attempt ID, while a fresh tab receives a different
attempt and cannot inherit another visitor's active run. `Start another
deployment` and edits after a completed attempt explicitly rotate the ID.

This is a frontend/API behavior change only:

- Terraform apply required: **No**
- Manual console/setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the implementation; the
  existing backend idempotency and server-side credential boundary remain in
  force.
- Safety gate: no browser credential, repository path, arbitrary command,
  Home Assistant state, thermostat, Pi/EC2 deployment, or normal release path
  is changed.

## REV 06 — expose only supported method and strategy combinations

REV 06 closes the gap between the finite request vocabulary and the behavior
that the two deployers actually execute. The shared contract now publishes a
capability matrix with four verified paths: Python + all-at-once + abort,
Python + canary + abort, Python + canary + rollback, and Ansible + all-at-once + abort. The anonymous fleet
response carries that matrix to the frontend, which exposes canary only for
Python and hides it for Ansible until equivalent serial behavior is proven. The
same shared validator rejects unsupported combinations at the backend boundary,
including when a caller bypasses the browser.

- Terraform apply required: **No**
- Manual console/setup: **None**
- Terraform resources changed: **None**
- Safety gate: the simulator remains bounded and readback-verified; no UI or
  API claim is made for rolling semantics that are not implemented; Python
  canary is the only target-scoped rollout currently enabled.

## Fleet Deploy form safety boundary

- Terraform apply required: **No**
- Manual console setup: **None**
- Terraform resources changed: **None**
- Safety gate: writes are limited to the simulated Fleet API through the trusted workflow; Home Assistant, thermostats, Pi, EC2, and normal production deployment state remain outside this route.

## PR 07 — local Python deployer

PR 07 adds the dependency-free `deploy_demo.deployer` client and CLI. It
reuses the validated `DeploymentSpec`, accepts only an exact canonical
`profile.json` artifact, and binds every run to that artifact's SHA-256
identity. The deployer resolves the spec's known target IDs, queues desired
state through the protected Fleet API, applies it, then performs a separate
fresh readback.

The readback gate verifies the deployment ID, schema version, stable target
set, desired and observed color/shape, desired and observed profile digests,
simulated target kind, succeeded target status, and the API's explicit
`verification: "verified"` result. A transport-level HTTP 200 without those
facts is a failed deployment. Lifecycle events are structured, immutable
values keyed by deployment ID and include the schema version, target set, and
artifact digest for later workflow/pipeline reporting.

For local or trusted workflow use:

```bash
FLEET_DEPLOY_API_KEY='not-for-browser' \
python -m deploy_demo.deployer \
  --api-base-url https://api.homeops.now/deploy/api \
  --spec deployment.json \
  --artifact profile.json \
  --artifact-sha256 <sha256>
```

The API key is read from the process environment and never appears in the
result or event payload. PR 07 changes no browser behavior, credentials,
Terraform, GitHub Actions dispatch, Home Assistant state, Pi state, or normal
HomeOps deployment path; PR 08 owns the simulator/deployer integration suite.

## PR 07 disposition

- Terraform apply required: **No**
- Manual console setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the local deployer implementation; later workflow work owns trusted artifact execution.
- Safety gate: the client can write only through the dedicated simulated Fleet API and requires independent observed-state verification before success.

## PR 08 — end-to-end local simulator/deployer tests

PR 08 proves the merged Python deployer against the real FastAPI Fleet API and
an isolated temporary SQLite simulator. The harness sends the real
`FleetApiClient` requests through an in-process `TestClient`, so the tests
exercise the protected queue route, explicit apply route, public readback, and
the durable state store together without touching production or starting a
server.

The suite covers one-target success, environment expansion, replay idempotence,
dedicated-credential rejection, queue-only web behavior, and a tampered fresh
readback. The last case demonstrates that an apply that returns HTTP 200 still
fails closed when the independent observed state does not match the immutable
artifact. No web request invokes the deployer synchronously: queueing leaves
the deployment pending until a separate protected apply request.

Run the demo-specific integration suite from the repository root:

```bash
PYTHONPATH=services/consumer:services/observer:services/insights:dashboard/backend:scripts \
python3 -m pytest --import-mode=importlib \
  deploy_demo/tests/test_deployer_e2e.py
```

The same test file is included in the explicit Ruff lists and the repository's
full pytest/test-count workflow. It adds six Python tests and changes no
Terraform resource, credential, browser behavior, Pi state, Home Assistant
state, or normal HomeOps deployment path.

## PR 08 disposition

- Terraform apply required: **No**
- Manual console setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the local integration-test PR; later workflow work owns trusted artifact execution.
- Safety gate: all writes are against an isolated test store and the local FastAPI simulator; production routes and deployment actions are not invoked.

## PR 09 — trusted workflow and immutable profile artifact

PR 09 adds `.github/workflows/fleet-deploy.yml`, which is a build-and-evidence
workflow only. It accepts `deployment_id` and a full `event_commit_sha` through
`workflow_dispatch`, and it fails unless the dispatch ref is protected
`master`. The runner pins both jobs to the trusted master commit selected at
dispatch, fetches the
`fleet-deployments` branch only as Git objects, verifies that the event commit
is an ancestor of that branch, and reads exactly
`manifests/<deployment_id>.json` with `git show`. It never checks out or
executes the writable manifest branch. Because this is a Git-over-HTTPS fetch
rather than a REST request, the runner builds a short-lived Basic
`x-access-token` header from its built-in read-only token; the token is never
printed or uploaded.

The trusted `deploy_demo.trusted_artifact` module reuses the shared
`DeploymentSpec` validator, rejects duplicate keys and non-canonical manifest
bytes, and binds the requested deployment ID and event commit SHA to the
selected file. Validation runs before the workflow installs lint/test tools or
builds an artifact. A later step creates exact canonical `profile.json` bytes,
prints their SHA-256 digest, and uploads the profile alongside separate
`profile-provenance.json` metadata. The event commit SHA is recorded as source
provenance and is never treated as the profile digest.

The workflow deliberately has only `contents: read` permission and does not
contain a Fleet API credential, deployer invocation, workflow dispatch, or
production side effect. It will fail closed until the later manifest-branch
task provides `fleet-deployments` and a reviewed manifest. The static workflow
readiness test is included in the ordinary CI test and Ruff surfaces.

## PR 09 disposition

- Terraform apply required: **No**
- Manual console setup: **None for this PR**; the later credential/prerequisite task owns repository and Fleet API secrets.
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the trusted workflow/artifact PR; PR 10 owns manifest commits and trusted workflow dispatch.
- Safety gate: manifest data is read at an exact, ancestry-checked commit; invalid data blocks before lint, tests, or artifact generation; no deployment is executed.

## PR 10 — commit manifests and dispatch the trusted workflow

PR 10 adds `POST /deploy/api/deployments/submit` for the public constrained
form. The backend reuses the shared `DeploymentSpec` validator, serializes the
exact canonical manifest bytes, and writes one deterministic
`manifests/<deployment_id>.json` path to the `fleet-deployments` branch through
the server-side GitHub Contents API. It then dispatches PR09's workflow from
`master` with the matching deployment ID and manifest commit SHA.

The SQLite state store creates the pending deployment record before external
calls under one `BEGIN IMMEDIATE` admission transaction. That transaction
enforces the per-IP cooldown, global active-deployment limit, and target
reservation atomically. A per-deployment lease prevents concurrent requests
from duplicating commit/dispatch work. If dispatch fails after the manifest is
committed, a retry reuses the stored commit and dispatches again; it does not
write a second manifest. Workflow run ID/URL fields are populated only when
the provider actually returns them—PR10 never guesses a run URL.

The public response contains only simulated deployment state, manifest identity,
safe operation codes, and provider-returned run metadata. The GitHub token and
the protected Fleet API key are backend-only values.

### PR 10 disposition

- Terraform apply required: **No for the code PR**
- Manual console/setup required before a live public submission: **Yes** — an operator must create the reviewed `fleet-deployments` branch from the approved base, provision `FLEET_DEPLOY_GITHUB_TOKEN` with repository-scoped Contents/Actions authority, and provision the separate `FLEET_DEPLOY_API_KEY` workflow/backend boundary.
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR10; the credential/IAM prerequisite owns secret entry, rotation, and any later Terraform apply. The backend intentionally does not auto-create the manifest branch.
- Safety gate: missing branch/credential fails closed; the browser cannot provide a repository, path, URL, command, or credential; commit-success/dispatch-failure remains recoverable from durable state.

## PR 11 — deploy the validated artifact from GitHub Actions

PR 11 extends the trusted workflow with a separate simulator-deployment job.
The read-only job validates the exact manifest before installing tools, runs its
lint and focused tests, builds the canonical profile, and uploads the validated
deployment spec beside the profile and provenance files. The deploy job runs
only after that job succeeds, downloads the immutable artifact, revalidates the
manifest/artifact/provenance binding, and invokes the dependency-free Python
deployer against the protected `/deploy/api` simulator API.

The deploy job receives only the separate `FLEET_DEPLOY_API_KEY` GitHub Actions
secret, uses a static concurrency group so simulator writes cannot overlap, and
fails before any write when the credential is absent or artifact verification
fails. The job dispatches the validated `python` deployer or `ansible` playbook
selected by the manifest; both queue desired state, apply it, and perform a
fresh readback that must prove all requested TEST targets succeeded. STAGE and
PROD remain untouched for the TEST environment. The normal Pi/EC2 deployment
secret and workflow remain outside this path.

### PR 11 disposition

- Terraform apply required: **No for the code PR**
- Manual console/setup required before a live submission: **Yes** — provision the separate GitHub Actions `FLEET_DEPLOY_API_KEY` secret and ensure the protected simulator API is reachable at `https://api.homeops.now/deploy/api`.
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR11; the supporting credential/IAM prerequisite owns secret entry and rotation.
- Safety gate: the deploy job is downstream of manifest validation, lint, tests, artifact generation, and provenance revalidation; it writes only to the simulated Fleet API and never to Home Assistant, thermostats, Pi state, or the normal production deployment path.

## PR 12 — show actual deployment run state in the UI

PR 12 makes the public `/deploy` page reconstruct a deployment from the real
GitHub Actions run and the simulator's observed state. The workflow now uses
the deterministic run name `Fleet deployment <deployment_id>`, allowing the
server to resolve the run created by GitHub's otherwise metadata-free `204`
dispatch response without guessing based on recency. The server reads the run
and bounded job state, exposes the manifest commit URL, run URL, timestamps,
job conclusions, and actionable failure details, and reconciles only proven
state transitions. REV 08 extends this boundary with a persisted frozen
request summary and keeps polling until terminal deployment state and current
workflow/job metadata converge in the same read cycle.

An in-progress Actions run maps to `applying`; a completed failure marks the
deployment failed while leaving observed profiles unchanged; a completed
success is accepted only when the simulator readback already proves every
requested target converged. The frontend polls these APIs for state rather
than advancing a client-side timer, refreshes the fleet cards after terminal
state, and keeps the active deployment ID in browser storage so a reload can
recover the same run without leaving stale workflow-job rows behind.

### PR 12 disposition

- Terraform apply required: **No for the code PR**
- Manual console/setup required: **None beyond the existing PR10/PR11 GitHub and simulator credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR12; the existing credential/IAM prerequisite remains separate.
- Safety gate: GitHub workflow/job state and observed simulator state are read and reconciled fail-closed; no Home Assistant, thermostat, Pi, EC2, or normal production deployment path is changed.

## PR 13 — implement the same operation in Ansible

PR 13 adds a real Ansible playbook at `ansible/deploy.yml`. The controller
validates the exact canonical DeploymentSpec and profile artifact through the
same Python parser and artifact identity checks used by the Python deployer.
Ansible then resolves the selected logical vehicle IDs through
`ansible/inventory.yml`, queues and applies through the protected simulator
API with `ansible.builtin.uri`, and performs a fresh unauthenticated
readback before reporting success.

The inventory names are logical simulator identifiers only. The playbook runs
on localhost and does not SSH to, create, or imply twelve real hosts. API
credentials are read from `FLEET_DEPLOY_API_KEY`, hidden on protected URI
tasks, and never sent to the browser. Syntax, API failure, artifact mismatch,
inventory mismatch, and observed-state mismatch all fail nonzero.

### PR 13 disposition

- Terraform apply required: **No**
- Manual console/setup required: **None beyond the existing Fleet Deploy simulator/API credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR13; REV 02 owns proving the selected Python and Ansible paths against the live TEST fleet before PR14+ UI work.
- Safety gate: local/synthetic Ansible tests only; no real hosts, Home Assistant, thermostat, Pi, EC2, or normal production deployment mutation is included.

## PR 15 — add a one-target canary rollout

PR 15 enables the Python `canary` + `abort` capability. Queueing still reserves
the complete stable-order target set, but the protected apply boundary accepts a
target subset only for a validated canary request. The Python deployer applies
and freshly verifies the first target, then applies and verifies the remaining
targets only after that readback succeeds. Its structured events identify the
canary and remaining-rollout scopes; a canary validation failure stops before a
remaining-target apply.

Ansible remains intentionally limited to its verified all-at-once path. The
shared capability matrix prevents the browser, backend, or trusted workflow
from claiming Ansible canary behavior before it has an equivalent implementation.

- Terraform apply required: **No**
- Manual console/setup required: **None beyond the existing Fleet Deploy simulator/API credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR15; no production deployment or simulator mutation is part of the code change itself.
- Safety gate: canary target subsets are accepted only for the validated Python capability; later targets remain pending until canary verification passes.

## PR 16 — inject a deterministic verification failure

PR 16 adds the optional `failure_target_id` to the canonical DeploymentSpec.
When present, the simulator deliberately leaves that selected logical target's
observed profile divergent and records a bounded target-level error. Python and
Ansible still use their normal queue, apply, and fresh public readback paths;
the deployer exits nonzero when verification cannot prove the requested state.
For a canary failure, later targets remain pending and the `/deploy` UI exposes
the desired-versus-observed drift, target error, manifest commit, and failed
Actions run.

- Terraform apply required: **No**
- Manual console/setup required: **None beyond the existing Fleet Deploy simulator/API credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR16; no production deployment or simulator mutation is part of the code change itself.
- Safety gate: the switch is validated against the fixed simulated target set and cannot reach Home Assistant, the normal HomeOps release path, Pi, or EC2.

## PR 17 — stop and restore a partial rollout

PR 17 enables Python `canary` + `rollback`. The deployer snapshots the
observed profiles returned before the first apply. If a later target fails
verification, it stops before applying any further targets, restores the
targets that actually changed through a protected API endpoint, and performs a
fresh public readback against those snapshots. The run remains failed even
when rollback is verified; an unverified rollback stays visible as partial
desired/observed state instead of becoming a false success.

- Terraform apply required: **No**
- Manual console/setup required: **None beyond the existing Fleet Deploy simulator/API credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR17; no production deployment or simulator mutation is part of the code change itself.
- Safety gate: rollback accepts only bounded finite profiles for selected simulated targets and cannot reach Home Assistant, the normal HomeOps release path, Pi, or EC2.

## PR 18 — show actual pipeline steps and deployment events

PR 18 extends the public deployment read with the bounded GitHub Actions step
receipts returned by the Jobs API and a durable, server-ordered event history
from the trusted Python deployer. Events are bound to the validated artifact
digest and selected target IDs, deduplicated by canonical lifecycle fields, and
stored in the persistent SQLite deployment row. The `/deploy` run panel keeps
Actions jobs/steps visually distinct from simulator events, labels downstream
jobs skipped after an upstream failure, and reconstructs both evidence sources
after a reload.

- Terraform apply required: **No**
- Manual console/setup required: **None beyond the existing Fleet Deploy simulator/API credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR18; no production deployment or simulator mutation is part of the code change itself.
- Safety gate: the timeline is read-only, simulated-only evidence; it never exposes credentials, accepts arbitrary event types or targets, or changes Home Assistant, the normal HomeOps release path, Pi, or EC2.

## PR 19 — add deployment history and a real reset

PR 19 adds a bounded anonymous recent-run history backed by the same durable
SQLite deployment rows used by the active run panel. Each entry exposes the
request selector, implementation, profile-artifact digest, outcome, timestamp,
manifest commit, and exact Actions run link; selecting an entry reopens the
server-backed deployment read rather than relying on browser-only state.

Fleet reset is a first-class `DeploymentSpec` operation. The browser submits a
full-fleet reset through the ordinary manifest admission, cooldown, active-run,
GitHub commit, trusted workflow, protected apply, and fresh-readback path. The
trusted simulator restores each target's deterministic readable baseline only
after the workflow reaches the protected deploy step. Reset admission is capped
at one request per UTC day by default through
`FLEET_DEPLOY_RESET_DAILY_CAP`; an active run still wins with the ordinary
busy-fleet response. No reset path directly mutates the database from the
browser.

- Terraform apply required: **No**
- Manual console/setup required: **None beyond the existing Fleet Deploy simulator/API credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges PR19; no production deployment or simulator mutation is part of the code change itself.
- Safety gate: reset accepts only the closed full-fleet operation, uses the existing trusted manifest/workflow boundary, and changes only the simulated Fleet API; Home Assistant, the normal HomeOps release path, Pi, and EC2 remain outside this boundary.

## DEFECT 01 — clear stale Fleet busy capacity after a failed canary

The public submission boundary now reconciles every queued/applying dispatched
workflow before counting active capacity. This covers the case where editing
the next request or reloading stops the browser from polling the older attempt
while its Actions run finishes. Terminal success and failure transitions also
clear the deployment's target reservations atomically; target status, desired /
observed drift, and pending later-canary evidence remain intact. Repeated
reconciliation and a backend restart are safe, and a new retry or independent
baseline restore is no longer blocked by a stale reservation.

- Terraform apply required: **No**
- Manual console/setup required: **None beyond the existing Fleet Deploy simulator/API credentials**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the defect-fix PR; no production deployment or simulator mutation is part of the code change itself.
- Safety gate: reconciliation reads only bounded simulated workflow/state records and releases only reservations owned by terminal simulated deployments; Home Assistant, the normal HomeOps release path, Pi, and EC2 remain outside this boundary.

## REV 02 — selected implementation live execution

The trusted simulator job reads the already validated `implementation` field
from the downloaded DeploymentSpec and dispatches exactly one bounded
controller path. `python` invokes `deploy_demo.deployer`; `ansible` invokes
`ansible/deploy.yml` against the same logical simulator inventory. The API key
remains an Actions-only environment secret, Ansible protected URI tasks remain
`no_log`, and both paths require the same verified fresh readback. The workflow
never executes commands or paths supplied by the manifest.

## Backend and API boundary

| Concern | Source of truth | Production path |
| --- | --- | --- |
| FastAPI application | `dashboard/backend/main.py` | Uvicorn on port 8000 inside host-networked Docker Compose |
| Backend image | `dashboard/backend/Dockerfile` | Explicitly copies the FastAPI files and shared `deploy_demo` package from the repository-root build context |
| Backend Compose | `dashboard/docker-compose.yml` | `backend`, `valkey`, `prometheus`, and `grafana` services |
| Public edge | `dashboard/nginx/api.homeops.now.conf` | TLS Nginx on `api.homeops.now`, default location proxies to `localhost:8000` |
| Backend deployment | `deploy/deploy-ec2.sh` | Fast-forward EC2 checkout, refresh runtime env, rebuild/recreate backend, wait for `/health`, validate Nginx |
| Current routes | `dashboard/backend/main.py`, `dashboard/backend/fleet_api.py` | `/health`, `/metrics`, `/api/current-temps`, `/api/diagnostic`, `/deploy/api/health`, `/deploy/api/fleet`, `/deploy/api/fleet/{target_id}`, `/deploy/api/deployments/history`, `/deploy/api/deployments/{deployment_id}`, anonymous `/deploy/api/deployments/submit`, and protected deployment queue/apply routes |

The demo management API is a new authorization boundary. Public reads may be
anonymous, but desired-state/apply/verification writes must require a
dedicated demo credential. That credential must not be the Home Assistant,
Ask HomeOps, Pi deploy, or normal EC2 deploy credential.

## Existing deployment workflows

### Normal application deployment

`.github/workflows/deploy.yml` runs on every push to `master` and uses the
following sequence:

1. Check out the repository on a GitHub-hosted Ubuntu runner.
2. Connect the runner to the Tailnet using `tailscale/github-action@v3`.
3. Verify Pi reachability and choose the EC2 Tailnet hostname, with a bounded
   public-EIP fallback.
4. Run `deploy/deploy-pi.sh` over SSH as `github-deploy`.
5. Run `deploy/deploy-ec2.sh` over SSH as `ubuntu`.
6. Run `scripts/deploy_smoke_check.py` against the public deployment.

This workflow has `contents: read` permissions and uses repository secrets for
Tailnet and SSH access. It is not the Fleet Deploy workflow and must not be
modified by PR 01.

### Frontend-only deployment

`.github/workflows/frontend-deploy.yml` builds the React app, injects the
public API/OIDC variables, syncs `dashboard/frontend/dist/` to the private S3
bucket, invalidates CloudFront, and runs the public smoke check. It does not
deploy the FastAPI backend.

### Trusted Fleet Deploy workflow

PR 09 implements `.github/workflows/fleet-deploy.yml` from trusted `master`.
It accepts only the deployment ID and event commit SHA as dispatch inputs,
treats the manifest commit as untrusted data, and never checks out or executes
code from the writable manifest branch. The workflow reads
`manifests/<deployment_id>.json` at the exact event commit, validates it with
the shared contract, and produces profile/provenance artifacts without
deploying. PR 10 owns committing manifests and dispatching this workflow.

The current repository has no `fleet-deployments` branch. The PR10 backend
fails closed until an operator creates it from the approved base; it does not
silently create a branch from a public request. Repository Actions policy and
branch-protection state must be explicitly verified before enabling public
dispatch.

## State and persistence findings

The original integration snapshot had no backend data volume. PR 03 now uses a
named Docker volume for a small SQLite deployment/simulator store. Valkey is
configured with snapshots and AOF disabled, so it remains unsuitable as the
durable store for deployment records or simulator state.

The state must survive backend/container recreation and preserve pending,
desired, observed, failed, and rollback states. A database file inside the
image or an unpersisted Valkey key is not sufficient.

## Credential and infrastructure boundary

The current EC2 runtime environment is populated by
`deploy/deploy-ec2.sh` from the existing `/homeops/production/*` SSM paths for
Ask HomeOps/OpenAI/OIDC/Valkey settings. REV 01 adds two dedicated SecureString
paths for the Fleet backend:

- `/homeops/<environment>/fleet-deploy-github-token` — the repository-scoped
  server credential for GitHub Contents writes and workflow dispatch;
- `/homeops/<environment>/fleet-deploy-api-key` — the separate bearer value
  shared by the backend and the protected simulator workflow.

The deploy script refreshes those values before recreating the backend and
preserves existing `.env` values when an unrelated SSM read is temporarily
unavailable. The backend expects the repository-scoped
`FLEET_DEPLOY_GITHUB_TOKEN` for GitHub Contents writes and workflow dispatch;
the existing `FLEET_DEPLOY_API_KEY` remains a separate protected simulator
write credential. Neither value is a Vite variable or browser response field.

Until the additive SSM/IAM rollout is applied, the production deploy workflow
temporarily injects the two masked repository secrets over its encrypted SSH
stream. This bridge is deliberately temporary: replace the broad GitHub OAuth
credential with a least-privilege token or GitHub App credential, populate the
two SSM paths, verify the safe Terraform plan, and then remove the workflow
injection.

The supporting prerequisite task must explicitly cover:

- a repository-scoped server-side `FLEET_DEPLOY_GITHUB_TOKEN` for manifest
  Contents writes and workflow dispatch;
- a separate workflow secret for protected simulator writes;
- SSM parameter names, EC2 IAM read permissions, Compose environment entries,
  and redacted missing-secret behavior;
- manual secret entry and rotation ownership; and
- a Terraform plan/apply safety check that does not replace the EC2 instance or
  Elastic IP.

The browser must never receive either credential. PR 01 makes no Terraform
changes and requires no apply; later credential/IAM work must declare its own
Terraform action in its PR and handoff.

## REV 01 — runtime credential hardening

The repository-side deploy path now preserves the Fleet credentials instead of
silently erasing them when `dashboard/.env` is regenerated. New EC2 instances
also read the same two SSM paths during bootstrap. Missing values remain
fail-closed and produce only redacted setup warnings.

- Terraform apply required: **Yes** — attach the additive
  `fleet_deploy_runtime_read` policy to the existing EC2 role.
- Manual console/setup: create the reviewed `fleet-deployments` branch; store
  the GitHub Contents/Actions credential in the Fleet GitHub SSM path; store a
  generated Fleet API key in the Fleet API SSM path and the matching
  `FLEET_DEPLOY_API_KEY` GitHub Actions secret.
- Terraform resources: `aws_iam_policy.fleet_deploy_runtime_read` and
  `aws_iam_role_policy_attachment.fleet_deploy_runtime_read`; no EC2 or EIP
  replacement is intended.
- Sequence and owner: merge the implementation first; the infrastructure
  owner applies the reviewed additive plan, then provisions the two values and
  reruns the production deploy before live acceptance.
- Safety gate: the plan must contain no EC2/EIP replacement and must preserve
  the existing bootstrap and Ask HomeOps IAM policies.

## Historical discovery findings at the PR 01 snapshot

- Repository default branch is `master`; `origin/master` is `20e540b`.
- The repository is public and the existing Actions workflow files are active.
- `https://homeops.now/deploy` currently returns HTTP 200, but the downloaded
  asset is the existing HomeOps SPA shell and the source `App.jsx` has no
  `/deploy` route.
- `https://api.homeops.now/health` currently returns HTTP 200.
- `https://api.homeops.now/deploy/api/health` remains HTTP 404 on the currently
  deployed production backend because PR 04 is implemented in this branch but
  has not yet been merged and deployed.
- The `fleet-deployments` branch does not currently exist.
- The current public repository secret names are limited to the existing AWS,
  Pi/EC2 SSH, and Tailnet deployment credentials; no Fleet Deploy credential is
  present.
- Master branch protection was not confirmed by the available GitHub API
  credential; do not describe master as protected until the repository setting
  is verified.
- The current backend Dockerfile and CI workflow use explicit file lists, so
  later demo modules and tests must be materialized into both surfaces. PR 03
  adds the state module and PR 04 adds the API module plus backend contract
  tests to those lists.

## PR 01 disposition

PR 01 is documentation/discovery only. No normal HomeOps workflow, production
secret, Terraform resource, or public runtime behavior is changed here.

Infrastructure contract for this PR:

- Terraform apply required: **No**
- Manual console setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: documentation can merge before implementation; Derek
  remains the merge authority.
- Safety gate: no deployment workflow changes and no production resource
  replacement.

## PR 02 — canonical deployment contract

PR 02 adds the dependency-free `deploy_demo.deployment_spec` package and its
focused regression suite. It is the single validator/serializer that later
API, workflow, deployer, and UI work must reuse. The human-readable contract,
target inventory, enum compatibility, canonical JSON examples, provenance
boundary, and browser trust boundary are documented in
[`deployment-contract.md`](deployment-contract.md).

This PR does not add a public route, persistence, credentials, GitHub Actions
workflow, deployer behavior, Terraform, or production deployment behavior.

## PR 03 — durable simulated-fleet state

PR 03 adds the dependency-free [`deploy_demo.fleet_state`](../deploy_demo/fleet_state.py)
store. It seeds twelve fixed logical vehicles—`TEST-01` through `PROD-04`—with
readable baseline profiles and keeps the requested (`desired`) profile separate
from the last confirmed (`observed`) profile.

The store records deployment IDs, canonical profile SHA-256 digests, target
sets, queued/applying/succeeded/failed status, and bounded failure text. A
queue operation and its target updates run in one SQLite `BEGIN IMMEDIATE`
transaction. Replaying the same deployment ID and profile digest is a no-op;
reusing the ID with a different profile or target set fails closed. A failed
deployment leaves observed state unchanged so a pending or failed card can
honestly show the desired/observed divergence. A successful transition copies
desired profiles to observed state atomically.

The production Compose wiring mounts the database at the named
`fleet_deploy_state` volume and passes
`FLEET_DEPLOY_STATE_PATH=/var/lib/homeops/deploy-demo/fleet-state.sqlite3` to
the backend. The backend image copies the shared `deploy_demo` package from
the repository root. Recreating the backend container therefore reopens the
same database instead of creating an image-local file. The state module remains
API-independent; PR 04 adds the public read and protected management routes.

## PR 03 disposition

- Terraform apply required: **No**
- Manual console setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the implementation PR; the
  named Docker volume is created by Compose on the normal backend deployment.
- Safety gate: no Home Assistant, thermostat, production deployment, public
  route, credential, or Terraform behavior changes.

## PR 04 — public reads and protected management API

PR 04 adds `dashboard/backend/fleet_api.py` and mounts it under
`/deploy/api`. Anonymous reads expose simulator health, all twelve target
snapshots, individual target state, and deployment readback. Every response
marks the target kind as `simulated` and keeps desired profiles/digests
separate from observed profiles/digests.

The protected contract queues a validated shared `DeploymentSpec` and applies
it through a dedicated `FLEET_DEPLOY_API_KEY` bearer credential. Applying a
queued deployment models the `applying` transition and then atomically updates
observed state; reapplying a successful deployment is an idempotent readback.
The response includes explicit deployment ID, desired digest, status, target
state, and `pending`/`verified`/`failed` verification.

The credential is backend-only and is not provisioned, copied to the frontend,
or reused from Home Assistant, Ask HomeOps, Pi deploy, or EC2 deploy secrets.
Missing configuration and invalid credentials fail closed. This API remains a
simulator boundary and does not write Home Assistant, thermostats, telemetry,
or normal production deployment state.

## PR 04 disposition

- Terraform apply required: **No**
- Manual console setup: **None for this PR**; the later supporting-prerequisite task owns secret provisioning and rotation.
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the implementation PR; the backend receives `FLEET_DEPLOY_API_KEY` only when the separately planned credential boundary is provisioned.
- Safety gate: no EC2 replacement, Elastic IP replacement, IAM change, Home Assistant write, thermostat write, telemetry mutation, or normal production deployment behavior.
