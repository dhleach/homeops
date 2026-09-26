# Fleet Deploy Lab integration map

Status: PR 09 trusted master-only artifact workflow on top of the merged PR 08 simulator/deployer integration suite
Repository: `dhleach/homeops`
Default branch: `master`
Latest integration snapshot: `d9f5f70`
GitHub issue: https://github.com/dhleach/homeops/issues/348

This document records the real HomeOps integration points for the Fleet Deploy
Lab as the implementation advances. It is deliberately specific about what
exists now, what is planned for later PRs, and what still needs an operator
decision.

## Purpose and boundary

The Fleet Deploy Lab will add a public simulated-fleet deployment demo at
`homeops.now/deploy`. A visitor will submit a constrained deployment spec; a
backend will write a validated manifest; a workflow running trusted `master`
will validate the manifest and build an immutable profile artifact; and a
Python or Ansible deployer will update twelve logical simulated vehicles.

The lab is not a real Home Assistant deployment, a second production release
path, hardware-in-the-loop, ECU control, or an autonomous-vehicle fleet. The
normal HomeOps Pi, EC2, frontend, and observability deployments remain separate.

## Current route map

| Surface | Current path | Current owner | Current behavior | Fleet Deploy Lab target |
| --- | --- | --- | --- | --- |
| Public frontend | `https://homeops.now/deploy` | CloudFront → private S3 → React/Vite SPA | CloudFront serves the SPA shell. The merged PR 05 renders the read-only fleet snapshot and responsive TEST/STAGE/PROD cards; PR 06 adds a preview-only constrained form and canonical DeploymentSpec view without enabling submission. | PR 09 builds the trusted profile artifact but does not connect the disabled Deploy control; PR 10 owns manifest submission/dispatch. |
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
the PR 06 form is client-side preview state only.

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

## PR 06 — constrained controls and read-only DeploymentSpec preview

PR 06 adds `DeploymentSpecForm.jsx` to the public `/deploy` route. The form
offers only the finite environments, simulated target IDs, profile colors and
shapes, Python/Ansible implementations, strategies, and failure modes defined
by the shared `deploy_demo.deployment_spec` contract. It validates the visible
deployment ID and strategy/failure-mode combinations, then renders the exact
field names and current values as a read-only JSON preview.

The Deploy button remains disabled. The browser makes no write request and
accepts no repository path, URL, command, arbitrary code, or credential. A
later PR owns the trusted manifest/workflow path and may enable submission only
after that boundary is reviewed.

## PR 06 disposition

- Terraform apply required: **No**
- Manual console setup: **None**
- Terraform resources changed: **None**
- Sequence and owner: Derek reviews and merges the frontend-only PR; a later trusted workflow PR owns deployment submission.
- Safety gate: no backend/API write, credential, GitHub Actions dispatch, Home Assistant, thermostat, normal production deployment, or infrastructure behavior changes.

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
`master`. The runner checks out trusted `master` code, fetches the
`fleet-deployments` branch only as Git objects, verifies that the event commit
is an ancestor of that branch, and reads exactly
`manifests/<deployment_id>.json` with `git show`. It never checks out or
executes the writable manifest branch.

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

## Backend and API boundary

| Concern | Source of truth | Production path |
| --- | --- | --- |
| FastAPI application | `dashboard/backend/main.py` | Uvicorn on port 8000 inside host-networked Docker Compose |
| Backend image | `dashboard/backend/Dockerfile` | Explicitly copies the FastAPI files and shared `deploy_demo` package from the repository-root build context |
| Backend Compose | `dashboard/docker-compose.yml` | `backend`, `valkey`, `prometheus`, and `grafana` services |
| Public edge | `dashboard/nginx/api.homeops.now.conf` | TLS Nginx on `api.homeops.now`, default location proxies to `localhost:8000` |
| Backend deployment | `deploy/deploy-ec2.sh` | Fast-forward EC2 checkout, refresh runtime env, rebuild/recreate backend, wait for `/health`, validate Nginx |
| Current routes | `dashboard/backend/main.py`, `dashboard/backend/fleet_api.py` | `/health`, `/metrics`, `/api/current-temps`, `/api/diagnostic`, `/deploy/api/health`, `/deploy/api/fleet`, `/deploy/api/fleet/{target_id}`, `/deploy/api/deployments/{deployment_id}`, and protected deployment queue/apply routes |

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

The current repository has no `fleet-deployments` branch. PR 10 must either
create it through an explicitly reviewed setup step or fail closed until an
operator creates it from the approved base. The current GitHub API credential
could list workflows and repository secret names, but the Actions policy
endpoints returned 403; repository Actions policy and branch-protection state
must therefore be explicitly verified before enabling public dispatch.

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
Ask HomeOps/OpenAI/OIDC/Valkey settings. There is no GitHub Contents/Actions
credential or Fleet API write credential in the current backend environment.

The supporting prerequisite task must explicitly cover:

- a repository-scoped server-side credential for manifest Contents writes and
  workflow dispatch;
- a separate workflow secret for protected simulator writes;
- SSM parameter names, EC2 IAM read permissions, Compose environment entries,
  and redacted missing-secret behavior;
- manual secret entry and rotation ownership; and
- a Terraform plan/apply safety check that does not replace the EC2 instance or
  Elastic IP.

The browser must never receive either credential. PR 01 makes no Terraform
changes and requires no apply; later credential/IAM work must declare its own
Terraform action in its PR and handoff.

## Verified findings at this snapshot

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
