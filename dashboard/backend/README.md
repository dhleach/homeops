# HomeOps Dashboard API

FastAPI service that runs on the EC2 host and queries the local Prometheus
instance for live HVAC telemetry. The production container listens on
`0.0.0.0:8000` through host networking; Nginx exposes the supported public
interface at `https://api.homeops.now`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Process liveness; returns `{"status":"ok"}` |
| `GET /api/current-temps` | Current floor/outdoor temperatures, setpoints, calls, furnace state, and freshness timestamp |
| `POST /api/diagnostic` | Authenticated Gemini-backed HVAC diagnostic using live Prometheus context |
| `GET /metrics` | Internal diagnostic abuse/cost metrics for EC2-local Prometheus; not a public route |
| `GET /openapi.json` | Generated API contract |

`/api/diagnostic` accepts a standard `Authorization: Bearer <token>` header.
The provider-neutral verifier must return a verified subject with the
`diagnostic:read` scope; missing/invalid credentials return `401`, and a
verified identity without that scope returns `403`. The default verifier rejects
all tokens until an OIDC-compatible provider adapter is configured, so the
application cannot silently fall back to anonymous access. The threat model and
quota policy live in [`docs/ask-homeops-threat-model.md`](../../docs/ask-homeops-threat-model.md).

The endpoint rejects blank, oversized, or unexpected request fields; caps
questions at 1,000 characters and model output at 256 tokens; bounds Prometheus
context assembly to 5 seconds; and bounds each Gemini request to 10 seconds.
Provider and missing configuration failures return a generic safe error rather
than exception text. Before any Prometheus or Gemini work, the endpoint applies
independent per-user and per-IP windows using the verified token subject and a
client IP resolved only from configured trusted proxy hops. The baseline limits
are 10 user requests/minute, 30 IP requests/minute, 100 user requests/day, 200
IP requests/day, and 2/5 user/IP in-flight calls. Quota rejections are HTTP
`429` responses with `Retry-After` and `RateLimit-*` headers.

`RateLimitStore` is an atomic adapter seam for Redis/Valkey or another shared
backend. The included memory implementation is explicitly for tests/local
development only; the default unconfigured store returns a generic `503` so a
production deployment cannot accidentally run without shared quota state. The
process also enforces a 20-call global in-flight limit and a 500-call UTC-day
provider budget as the final single-instance cost backstop.

The backend publishes low-cardinality request/provider outcome, latency, input
size, estimated output-token, in-flight, daily-budget, model, and approximate
cost metrics. The Prometheus scrape is bound to EC2 loopback; Nginx explicitly
returns 404 for public `/metrics` requests.
The quota defaults can be overridden with `ASK_HOMEOPS_GLOBAL_MAX_IN_FLIGHT`
and `ASK_HOMEOPS_GLOBAL_DAILY_CALL_LIMIT`; the approximate cost estimator uses
the optional `GEMINI_INPUT_COST_USD_PER_MILLION_TOKENS` and
`GEMINI_OUTPUT_COST_USD_PER_MILLION_TOKENS` overrides.

`/api/current-temps` returns a structured response with nullable telemetry
fields. A non-null `error` means Prometheus was unreachable; the deployment
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
`GEMINI_API_KEY` only for `/api/diagnostic`. Never commit that key. The
limiter reference backend can be selected with
`ASK_HOMEOPS_LIMITER_BACKEND=memory` for local development only. Production
must provide a real shared `RateLimitStore` adapter and an OIDC-compatible
bearer verifier before exposing the Bob demo. Configure trusted reverse-proxy
networks with `ASK_HOMEOPS_TRUSTED_PROXY_IPS` and the hop count with
`ASK_HOMEOPS_TRUSTED_PROXY_HOPS`.

The active production topology, ports, public routes, internal scrape target, and release checks are
documented in [`docs/architecture.md`](../../docs/architecture.md) and
[`docs/deployment.md`](../../docs/deployment.md).
