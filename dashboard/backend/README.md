# HomeOps Dashboard API

FastAPI service that runs on the EC2 host and queries the local Prometheus
instance for live HVAC telemetry. The production container listens on
`0.0.0.0:8000` through host networking; Nginx exposes the supported public
interface at `https://api.homeops.now`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Process liveness; returns `{"status":"ok"}` |
| `GET /api/current-temps` | Current floor/outdoor temperatures, setpoints, heating/cooling calls, inferred AC state, per-zone action, and freshness timestamp |
| `POST /api/diagnostic` | Authenticated GPT-5.6 Luna-backed HVAC diagnostic using live Prometheus context |
| `GET /deploy/api/health` | Read-only Fleet Deploy Lab simulator readiness |
| `GET /deploy/api/fleet` | Anonymous desired/observed state for all twelve explicitly simulated targets |
| `GET /deploy/api/fleet/{target_id}` | Anonymous read of one simulated target |
| `GET /deploy/api/deployments/{deployment_id}` | Anonymous fresh deployment state and verification result |
| `POST /deploy/api/deployments/submit` | Anonymous constrained submission; commits one manifest and dispatches the trusted workflow through the server-only GitHub adapter |
| `POST /deploy/api/deployments` | Protected desired-state queue operation |
| `POST /deploy/api/deployments/{deployment_id}/apply` | Protected simulator apply/observation operation; safe to replay |
| `GET /metrics` | Internal diagnostic abuse/cost metrics for EC2-local Prometheus; not a public route |
| `GET /openapi.json` | Generated API contract |

`/api/diagnostic` accepts a standard `Authorization: Bearer <token>` header.
The configured Cognito OIDC verifier validates the RSA signature, issuer,
expiry, subject, and app-client `client_id` against the user-pool JWKS. A
verified subject must carry the configured diagnostic scope; missing/invalid
credentials return `401`, and a verified identity without that scope returns
`403`. If OIDC settings are absent or the JWKS is unavailable, authentication
fails closed with a generic `503`. The threat model and quota policy live in
[`docs/ask-homeops-threat-model.md`](../../docs/ask-homeops-threat-model.md).

The endpoint rejects blank, oversized, or unexpected request fields; caps
questions at 1,000 characters and provider output at 1,024 tokens; bounds
Prometheus context assembly to 5 seconds; and bounds each OpenAI request to 15
seconds. Provider and missing configuration failures return a generic safe
error rather than exception text. Before any Prometheus or provider work, the
endpoint applies independent per-user and per-IP windows using the verified token subject and a
client IP resolved only from configured trusted proxy hops. The baseline limits
are 10 user requests/minute, 30 IP requests/minute, 100 user requests/day, 200
IP requests/day, and 2/5 user/IP in-flight calls. Quota rejections are HTTP
`429` responses with `Retry-After` and `RateLimit-*` headers.

`RateLimitStore` is backed in production by a loopback-only Valkey container.
The adapter reserves all dimensions in one Lua script, hashes user/IP material
before writing keys, and releases only in-flight reservations after the
request. The included memory implementation is explicitly for tests/local
development only; the default unconfigured store returns a generic `503` so a
production deployment cannot accidentally run without shared quota state. The
process also enforces a 20-call global in-flight limit and a 500-call UTC-day
provider budget as the final single-instance cost backstop.

Ask HomeOps treats the question as untrusted content, never as an instruction.
Known requests to reveal prompts/private memory, use tools, change policy, or
write thermostat state receive a stable read-only refusal before Prometheus or
provider work. The OpenAI request has a fixed system instruction that reiterates
the same boundary, registers no tools, and limits the model to explaining the
supplied telemetry; it cannot execute commands, access files, or control a
thermostat. Adversarial requests and control-plane fields are covered by the
backend regression suite. Gemini remains available only when explicitly selected
as a rollback provider.

The diagnostic context includes the current thermostat-derived cooling calls,
inferred whole-home AC demand, and conservative per-zone `heating`, `cooling`,
`idle`, or unavailable actions. It also includes today's cooling runtime when
the corresponding gauges are present. Missing or contradictory cooling gauges
remain explicitly unavailable; the context and prompt never turn them into an
idle state or claim that the compressor is running. A separate thermostat
`hvac_mode` is not exposed by this context, so an idle zone is described only as
having no observed heat or cooling call.

The backend publishes low-cardinality request/provider outcome, latency, input
size, estimated output-token, in-flight, daily-budget, model, and approximate
cost metrics. The Prometheus scrape is bound to EC2 loopback; Nginx explicitly
returns 404 for public `/metrics` requests.
The default provider is OpenAI GPT-5.6 Luna. Set
`ASK_HOMEOPS_DIAGNOSTIC_PROVIDER=gemini` only for an explicit rollback. The
quota defaults can be overridden with `ASK_HOMEOPS_GLOBAL_MAX_IN_FLIGHT`
and `ASK_HOMEOPS_GLOBAL_DAILY_CALL_LIMIT`; the approximate cost estimator uses
the optional provider-specific `OPENAI_INPUT_COST_USD_PER_MILLION_TOKENS`,
`OPENAI_OUTPUT_COST_USD_PER_MILLION_TOKENS`,
`GEMINI_INPUT_COST_USD_PER_MILLION_TOKENS`, and
`GEMINI_OUTPUT_COST_USD_PER_MILLION_TOKENS` overrides.

`/api/current-temps` returns a structured response with nullable telemetry
fields. The legacy `floor_N_call` and `furnace_active` fields remain heating-only;
additive `floor_N_cooling_call`, `ac_cooling_active`, and
`floor_N_hvac_action` fields expose thermostat-derived cooling without claiming
compressor feedback. Each action is `heating`, `cooling`, or `idle`; it is
`null` when either paired call gauge is unavailable or the gauges contradict
each other. A non-null `error` means Prometheus was unreachable; the deployment
smoke gate treats that as unhealthy. CORS is owned by
`dashboard/nginx/api.homeops.now.conf`; do not add FastAPI middleware that
creates duplicate `Access-Control-Allow-Origin` headers.

## Local development

```bash
cd dashboard/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

The backend expects Prometheus at `http://localhost:9090` and reads
`OPENAI_API_KEY` for `/api/diagnostic` by default. Never commit either provider
key. Set `ASK_HOMEOPS_DIAGNOSTIC_PROVIDER=gemini` and provide
`GEMINI_API_KEY` only when deliberately rolling back. The
limiter reference backend can be selected with
`ASK_HOMEOPS_LIMITER_BACKEND=memory` for local development only. Production
uses `ASK_HOMEOPS_LIMITER_BACKEND=redis` and
`ASK_HOMEOPS_REDIS_URL=redis://127.0.0.1:6379/0`, with the Valkey service
started by Compose. Configure OIDC with `ASK_HOMEOPS_OIDC_ISSUER`,
`ASK_HOMEOPS_OIDC_AUDIENCE`, optional `ASK_HOMEOPS_OIDC_JWKS_URL`,
`ASK_HOMEOPS_OIDC_AUDIENCE_CLAIM=client_id`, and
`ASK_HOMEOPS_DIAGNOSTIC_SCOPE`. Configure trusted reverse-proxy networks with
`ASK_HOMEOPS_TRUSTED_PROXY_IPS` and the hop count with
`ASK_HOMEOPS_TRUSTED_PROXY_HOPS`.

## Fleet Deploy Lab API boundary

The Fleet Deploy Lab is a separate simulator control plane. Every fleet read
returns `simulated: true`, `target_kind: "simulated"`, and explicit target
labels (`TEST-01` through `PROD-04`). Each target keeps `desired` and
`observed` profiles, their SHA-256 digests, deployment status, and the active
deployment ID separate so a queued or failed operation cannot look successful.

The management endpoints accept only
`Authorization: Bearer <FLEET_DEPLOY_API_KEY>`. The backend reads that value
from the dedicated `FLEET_DEPLOY_API_KEY` environment variable and fails closed
with `503` when it is not configured; invalid or missing credentials receive
`401`. This is deliberately not the Cognito/OIDC diagnostic credential, a Home
Assistant token, or a normal Pi/EC2 deployment secret. Compose passes the value
only to the backend; it is not a frontend/Vite variable and must never reach a
browser.

`POST /deploy/api/deployments` validates the exact shared `DeploymentSpec`
contract and records desired state. The protected `/apply` operation moves the
simulator through `applying` and atomically copies desired profiles to observed
profiles. Replaying an already successful apply is a read-only idempotent
response. Public deployment reads expose `verification: "pending"`,
`"verified"`, or `"failed"` plus the fresh target snapshots for a deployer or
UI to verify digest, color, shape, and status.

`POST /deploy/api/deployments/submit` accepts only the shared closed
`DeploymentSpec` JSON. It never accepts a repository, branch, path, URL,
command, or credential from the browser. The backend creates the durable
pending row under SQLite's global admission transaction, enforcing the
per-client-IP cooldown and active-deployment limit, then writes the canonical
`manifests/<deployment_id>.json` file to the dedicated `fleet-deployments`
branch and dispatches `.github/workflows/fleet-deploy.yml` from trusted
`master`. The server expects `FLEET_DEPLOY_GITHUB_TOKEN` and fails closed when
it is absent; that token never appears in logs or responses.

The response records the manifest commit SHA and any workflow run metadata
GitHub actually returns. A dispatch failure stores a safe operation code and
leaves the exact commit available for a retry of the same deployment ID, so a
recovery request cannot create a second manifest commit. The backend does not
guess a workflow-run URL when the dispatch endpoint returns `204`.

The trusted workflow's downstream deploy job consumes only the validated
profile/provenance artifact and the separate `FLEET_DEPLOY_API_KEY` Actions
secret. It serializes simulator writes, calls the protected queue/apply routes,
and requires a fresh public readback before reporting success; this workflow
does not reuse the normal Pi/EC2 deployment credential.

The active production topology, ports, public routes, internal scrape target, and release checks are
documented in [`docs/architecture.md`](../../docs/architecture.md) and
[`docs/deployment.md`](../../docs/deployment.md).

## Fleet Deploy Lab state boundary

The separate Fleet Deploy Lab uses the shared dependency-free `deploy_demo`
package for its simulated-fleet contract, state store, and trusted Python
deployer client. Compose builds this
image from the repository root so the package is present in the backend image,
mounts the named `fleet_deploy_state` volume at
`/var/lib/homeops/deploy-demo`, and sets
`FLEET_DEPLOY_STATE_PATH` to the SQLite file inside that volume. This is a
demo control-plane store only; the Fleet API does not alter HVAC, telemetry,
diagnostic, or normal HomeOps deployment behavior. The state store must never
be moved to an image-local path or an unpersisted Valkey key.

The local end-to-end proof suite exercises the real `FleetApiClient` and
`FleetDeployer` against an isolated FastAPI `TestClient` and temporary SQLite
store. It covers protected queue/apply/readback behavior, environment target
expansion, replay idempotence, queue-only web requests, and fail-closed
verification. Run it from the repository root with:

```bash
PYTHONPATH=services/consumer:services/observer:services/insights:dashboard/backend:scripts \
python3 -m pytest --import-mode=importlib \
  deploy_demo/tests/test_deployer_e2e.py
```
