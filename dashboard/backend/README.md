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
| `POST /api/diagnostic` | Gemini-backed HVAC diagnostic using live Prometheus context |
| `GET /openapi.json` | Generated API contract |

`/api/diagnostic` is currently a public, read-only endpoint. Authentication,
per-user/IP quotas, and the remaining public-assistant controls are tracked in
the separate Ask HomeOps hardening P0; this endpoint must not be presented as
an authenticated or unrestricted production assistant. The current
non-authenticated guardrails
reject blank, oversized, or unexpected request fields; cap questions at 1,000
characters and model output at 256 tokens; bound Prometheus context assembly to
5 seconds; and bound each Gemini request to 10 seconds. Provider and missing
configuration failures return a generic safe error rather than exception text.

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
`GEMINI_API_KEY` only for `/api/diagnostic`. Never commit that key.

The active production topology, ports, public routes, and release checks are
documented in [`docs/architecture.md`](../../docs/architecture.md) and
[`docs/deployment.md`](../../docs/deployment.md).
