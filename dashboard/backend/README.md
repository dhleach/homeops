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

The active production topology, ports, public routes, internal scrape target, and release checks are
documented in [`docs/architecture.md`](../../docs/architecture.md) and
[`docs/deployment.md`](../../docs/deployment.md).
