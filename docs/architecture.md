# HomeOps architecture and interface inventory

This is the operational map for the production HomeOps system. It is kept
separate from the recruiter-facing overview in [`README.md`](../README.md) so
future changes can update concrete interfaces without turning the landing page
into a runbook.

Last verified: **2026-08-21** from the repository, public DNS, and public HTTP
health checks. Addresses and host paths are implementation details, not
credentials; tokens and private keys must never be added here.

## Active production path

```text
Home Assistant (Pi host network :8123)
        │ WebSocket state_changed events
        ▼
observer.py ──► state/observer/events.jsonl
        │ local JSONL tail
        ▼
consumer.py ──► state/consumer/events.jsonl
        │
        └──► :8001/metrics ── Tailscale ──► Prometheus (:9090 on EC2)
                                                │
                         ┌──────────────────────┴──────────────────────┐
                         ▼                                             ▼
                  Grafana (:3000)                                FastAPI (:8000)
                                                                       ├── Valkey (:6379, loopback)
                         │                                             │
                         └──────────── Nginx :80/:443 ──────────────────┘
                                          │
                                  api.homeops.now

React/Vite build ──► private S3 bucket ──► CloudFront ──► homeops.now
```

The active runtime uses Docker Compose with `network_mode: host` on EC2. The
backend, Prometheus, and Grafana therefore bind to the EC2 host's loopback
interfaces; Nginx is the public reverse-proxy boundary for those services.

## Address and interface inventory

| Surface | Current address / identifier | Interface | Owner / authority |
|---|---|---|---|
| Home Assistant | Pi loopback | `http://127.0.0.1:8123`; WebSocket `/api/websocket` | `compose/docker-compose.yml` |
| Pi deployment host | `100.115.21.72` (Tailscale IPv4) | SSH as `github-deploy`; repo `/home/leachd/repos/homeops` | `.github/workflows/deploy.yml`, `infra/variables.tf` |
| Pi metrics | `100.115.21.72:8001` (Tailnet only) | `GET /metrics` scraped every 15 seconds | `services/consumer/metrics.py`, `dashboard/prometheus/prometheus.yml` |
| EC2 public host | `32.194.69.77` (current DNS result) | API/Grafana DNS and fallback SSH as `ubuntu` | Terraform EIP + `.github/workflows/deploy.yml` |
| EC2 CI interface | `homeops-ec2` (Tailnet hostname) | Preferred SSH target from the Tailnet-connected GitHub runner; public EIP is fallback | EC2 bootstrap hostname + `.github/workflows/deploy.yml` |
| EC2 backend | EC2 loopback | `http://127.0.0.1:8000`; FastAPI `/health`, `/api/current-temps`, authenticated `/api/diagnostic`, internal `/metrics` | `dashboard/docker-compose.yml`, `dashboard/backend/main.py`, `dashboard/backend/security.py`, `dashboard/prometheus/prometheus.yml` |
| Ask HomeOps quota store | EC2 loopback | Valkey `redis://127.0.0.1:6379/0`; atomic per-user/IP windows | `dashboard/docker-compose.yml`, `dashboard/backend/security.py` |
| Ask HomeOps identity | Cognito managed login | Browser authorization-code + PKCE; backend verifies access-token JWKS | `infra/cognito.tf`, `dashboard/frontend/src/auth/oidc.js`, `dashboard/backend/security.py` |
| Prometheus | EC2 loopback | `http://127.0.0.1:9090`; public read-only `/prometheus/` | `dashboard/docker-compose.yml`, `dashboard/nginx/api.homeops.now.conf` |
| Grafana | EC2 loopback | `http://127.0.0.1:3000`; public read-only `/grafana/` | `dashboard/docker-compose.yml`, `dashboard/nginx/api.homeops.now.conf` |
| Frontend | `homeops.now` | CloudFront HTTPS → private S3 origin | `infra/cloudfront.tf`, `infra/s3.tf` |

The EC2 public address is an Elastic IP and should be refreshed from
`terraform output -raw ec2_public_ip` if the infrastructure is recreated. The
Pi address is the stable Tailnet peer used by both CI and Prometheus.

`infra/variables.tf` also has an `agent_ip` input. It controls the EC2 security
group's SSH allowlist for the Bob/OpenClaw container; it is intentionally not
hard-coded here because it is environment-specific and may change.

## Public verification surface

These are the endpoints the release gate checks after deployment:

| URL | Expected result | Purpose |
|---|---|---|
| `https://homeops.now/` | HTTP 200, HomeOps SPA shell | CloudFront/S3 frontend |
| `https://api.homeops.now/health` | `{"status":"ok"}` | FastAPI process liveness |
| `https://api.homeops.now/openapi.json` | Includes `/health` and `/api/current-temps` | API contract is served |
| `https://api.homeops.now/api/current-temps` | Required temperature/setpoint, heating/cooling-call, inferred-AC, and per-zone action fields with `error: null` | Prometheus → FastAPI data path |
| `https://api.homeops.now/api/diagnostic` | `POST` diagnostic route | OpenAI GPT-5.6 Luna-backed HVAC analysis; requires a verified bearer principal and bounded user/IP quotas before provider work |
| `https://api.homeops.now/metrics` | HTTP 404 | Backend abuse/cost metrics are internal-only and scraped from EC2 loopback |
| `https://api.homeops.now/grafana/api/health` | `database: ok` | Grafana process/data health |
| `https://api.homeops.now/prometheus/-/healthy` | Body contains `Healthy` | Prometheus process health |

The smoke checker is [`scripts/deploy_smoke_check.py`](../scripts/deploy_smoke_check.py).
It reports schema/health failures without printing household telemetry values.

Ask HomeOps uses Cognito as its OIDC identity provider. The browser obtains an
access token through authorization code + PKCE; FastAPI verifies its signature,
issuer, expiry, `client_id`, subject, and configured diagnostic scope against
Cognito's JWKS. The per-user quota key comes from the verified subject, while
the IP key comes only from configured trusted Nginx proxy hops. Valkey stores
all quota dimensions atomically; the default unconfigured backend still fails
closed with `503`, and the included memory backend is local/test-only.

## Active versus migration surfaces

- **Active CI/CD:** `.github/workflows/deploy.yml` updates the Pi first, then the
  EC2 Docker Compose backend; `.github/workflows/frontend-deploy.yml` builds
  and publishes the React app to S3/CloudFront.
- **PR lifecycle signal:** .github/workflows/pr-lifecycle-merge.yml runs only
  for a merged pull request targeting master. It publishes a bounded,
  marker-only pr-lifecycle-event artifact for the private OpenClaw
  reconciliation backstop; it does not deploy, expose a webhook, or execute
  pull-request code.
- **Active infrastructure:** Terraform provisions the EC2 host, Elastic IP,
  DNS, CloudFront, S3, certificates, IAM, and host bootstrap. The EC2
  bootstrap can join the Tailnet and an optional k3s cluster.
- **Not active in CI:** `k8s/nginx/` is the M2 Kubernetes/Traefik migration
  surface. The current workflows do not run `kubectl apply`; changing a file
  there does not change the active Docker Compose deployment.
- **Migration warning:** `k8s/nginx/nginx-configmap.yaml` contains a hard-coded
  `100.75.59.106` backend target. Treat it as a historical EC2 Tailnet address
  until the k3s migration is deliberately resumed and the target is verified.
- **Unsupported DNS alias:** Terraform creates `grafana.homeops.now`, but the
  active Nginx configuration serves Grafana under
  `api.homeops.now/grafana/`. The separate alias currently has a certificate/
  virtual-host mismatch and is not a supported public interface.

## Data and ownership boundaries

| Data / process | Location | Notes |
|---|---|---|
| Home Assistant config | Pi `/home/leachd/srv/homeops/homeassistant/config` | Host-mounted into the HA container |
| Raw observer events | Pi `state/observer/events.jsonl` | Append-only JSONL, rotated by logrotate |
| Derived consumer events | Pi `state/consumer/events.jsonl` | Append-only JSONL, source for metrics |
| Consumer state | Pi `state/consumer/state.json` | Restart/bootstrap state; not a source-control artifact |
| Prometheus data | EC2 Docker volume | 90-day retention configured in Compose |
| Grafana data | EC2 Docker volume | Provisioned dashboards are versioned under `dashboard/grafana/` |
| Frontend assets | Private S3 bucket | CloudFront OAC is the public delivery boundary |

The Pi checkout is owned by `leachd`; the CI SSH account is only the deployment
principal. The Pi deployment script runs Git as `leachd` and restarts the two
systemd units with narrowly scoped `sudo`, which avoids making the repository
world-writable or granting `github-deploy` direct write access to `.git`.
