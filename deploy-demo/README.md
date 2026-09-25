# Fleet Deploy Lab integration map

Status: PR 01 discovery snapshot
Repository: `dhleach/homeops`
Default branch: `master`
Snapshot commit: `20e540b`
GitHub issue: https://github.com/dhleach/homeops/issues/328

This document records the real HomeOps integration points for the Fleet Deploy
Lab before implementation. It is deliberately specific about what exists now,
what is planned for later PRs, and what still needs an operator decision.

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
| Public frontend | `https://homeops.now/deploy` | CloudFront → private S3 → React/Vite SPA | Returns HTTP 200 and the existing SPA shell. `App.jsx` does not currently route on `window.location.pathname`, so it renders the existing HVAC dashboard. | PR 05 adds a route-aware Fleet Deploy view without changing existing routes. |
| Existing backend liveness | `https://api.homeops.now/health` | Nginx → FastAPI | Returns `{"status":"ok"}`. | Remains the process liveness check. |
| Fleet demo health | `https://api.homeops.now/deploy/api/health` | Nginx → FastAPI | Currently HTTP 404; no Fleet Deploy routes exist yet. | PR 04 adds a public read-only demo health/state boundary. |
| Existing telemetry | `https://api.homeops.now/api/current-temps` | FastAPI → EC2-local Prometheus | Current production telemetry contract. | Must remain unchanged. |
| Existing diagnostic | `https://api.homeops.now/api/diagnostic` | FastAPI → authenticated provider path | Authenticated, quota-limited, read-only HVAC diagnostics. | Must remain separate from Fleet Deploy credentials and state. |

The `/deploy/api/*` prefix is intentionally on `api.homeops.now`, not under
the CloudFront frontend origin. The browser page at `homeops.now/deploy` will
call the API using the existing `VITE_API_URL=https://api.homeops.now` build
configuration. Nginx already proxies the default API location and allows
`GET`, `POST`, and `OPTIONS` for the `homeops.now` origin.

### Route smoke contract

The following checks are the intended public smoke contract. The first two are
valid now; the third is expected to remain 404 until PR 04 is merged and
deployed.

```bash
curl -fsS https://homeops.now/deploy >/dev/null
curl -fsS https://api.homeops.now/health
curl -fsS https://api.homeops.now/deploy/api/health
```

For a discovery-only run, the third check should be recorded as “not yet
implemented,” not silently treated as a working endpoint. After PR 04, it must
become a required 200/readiness check.

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

CloudFront already makes `/deploy` reachable as an SPA path; PR 01 does not
need a new DNS record, S3 origin, certificate, or Terraform route. The
frontend application still needs explicit route-aware rendering. A new
frontend module or route must be included in the existing Vite build and
frontend workflow; it must not be published as an unrelated static site.

## Backend and API boundary

| Concern | Source of truth | Production path |
| --- | --- | --- |
| FastAPI application | `dashboard/backend/main.py` | Uvicorn on port 8000 inside host-networked Docker Compose |
| Backend image | `dashboard/backend/Dockerfile` | Currently copies only `main.py` and `security.py`; new modules require an explicit Dockerfile update |
| Backend Compose | `dashboard/docker-compose.yml` | `backend`, `valkey`, `prometheus`, and `grafana` services |
| Public edge | `dashboard/nginx/api.homeops.now.conf` | TLS Nginx on `api.homeops.now`, default location proxies to `localhost:8000` |
| Backend deployment | `deploy/deploy-ec2.sh` | Fast-forward EC2 checkout, refresh runtime env, rebuild/recreate backend, wait for `/health`, validate Nginx |
| Current routes | `dashboard/backend/main.py` | `/health`, `/metrics`, `/api/current-temps`, `/api/diagnostic` |

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

### Future Fleet Deploy workflow

The planned `deploy-demo.yml` must run from trusted `master`, accept only the
deployment ID and event commit SHA as dispatch inputs, and treat the manifest
commit as untrusted data. It must never check out or execute code from the
writable manifest branch.

The current repository has no `fleet-deployments` branch. PR 10 must either
create it through an explicitly reviewed setup step or fail closed until an
operator creates it from the approved base. The current GitHub API credential
could list workflows and repository secret names, but the Actions policy
endpoints returned 403; repository Actions policy and branch-protection state
must therefore be explicitly verified before enabling public dispatch.

## State and persistence findings

The existing Compose file persists Prometheus and Grafana data in named Docker
volumes. The backend has no data volume. Valkey is configured with snapshots and
AOF disabled, so it is not a durable store for deployment records or simulator
state.

PR 03 must choose and test one of these explicit boundaries:

- a named Docker volume for a small SQLite deployment/simulator store; or
- an explicitly host-managed state directory outside the Git checkout.

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
- `https://api.homeops.now/deploy/api/health` currently returns HTTP 404 because
  PR 04 has not been implemented.
- The `fleet-deployments` branch does not currently exist.
- The current public repository secret names are limited to the existing AWS,
  Pi/EC2 SSH, and Tailnet deployment credentials; no Fleet Deploy credential is
  present.
- Master branch protection was not confirmed by the available GitHub API
  credential; do not describe master as protected until the repository setting
  is verified.
- The current backend Dockerfile and CI workflow use explicit file lists, so
  later demo modules and tests must be materialized into both surfaces.

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
