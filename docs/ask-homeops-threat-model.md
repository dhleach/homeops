# Ask HomeOps threat model and quota policy

**Policy version:** `homeops.ask-homeops-policy.v1`  
**Status:** Baseline implemented for the pre-Bob-demo security gate; production recovery and final public exposure remain gated
**Last reviewed:** 2026-08-21
**Applies to:** `POST https://api.homeops.now/api/diagnostic`

This document defines the security boundary and initial resource policy for
Ask HomeOps. It remains intentionally provider-agnostic: the application now
has the bearer-principal and atomic limiter contracts, but this document does
not choose an identity vendor or a storage product for limiter state.

## Executive decision

Ask HomeOps is a read-only HVAC diagnostic, not a general-purpose assistant
and not a device-control surface. Before the constrained Bob demo is exposed
to the public, the endpoint must have all of the following:

1. Verified authentication for normal diagnostic access.
2. An edge IP backstop and an application-level quota keyed by verified user
   identity.
3. A global model-call budget, bounded concurrency, and abuse/cost metrics.
4. Safe error responses and logs that do not disclose prompts, telemetry,
   credentials, or provider internals.
5. An OpenAPI contract that accurately describes the authentication and
   throttling behavior.

Until those gates are complete, the endpoint is a public transitional surface
for the existing HomeOps dashboard only. It must not be marketed as the
public Bob demo.

## Current production boundary

```text
Browser / Internet
        │ HTTPS
        ▼
Nginx on EC2 :443
        │ reverse proxy to localhost:8000
        ▼
FastAPI /api/diagnostic
        ├── Cognito OIDC/JWKS token verification
        ├── atomic per-user/IP quotas in loopback Valkey :6379
        ├── read-only Prometheus queries on EC2 :9090
        └── Gemini generateContent request
                │ API key stays server-side
                ▼
        Google Gemini API
```

The active deployment is Docker Compose with host networking on one EC2
instance. Nginx is the public edge; FastAPI is not directly exposed as a
public port. The frontend sends the homeowner's question to the endpoint, and
the backend builds a current HVAC snapshot from Prometheus before making one
Gemini request.

The endpoint boundary requires a standard bearer credential and a verified
Cognito access token carrying the configured
`https://api.homeops.now/diagnostic:read` scope. CORS restricts browser origins
but is not authentication: a non-browser client can call the public HTTPS
endpoint directly, so token validation and shared quota state remain explicit
server controls. The public `/api/current-temps`,
`/grafana/`, and `/prometheus/` surfaces are related exposure areas but are not
silently changed by this policy.

### Edge configuration finding

The Nginx preflight contract now advertises `GET, POST, OPTIONS`, matching the
frontend's `POST /api/diagnostic` request. This remains a browser-compatibility
control, not an authentication control; bearer validation and quota state are
still enforced by FastAPI.

## Assets and security objectives

| Asset | Security objective |
|---|---|
| `GEMINI_API_KEY` / SSM-backed secret | Never disclose to clients, logs, prompts, or source control; prevent unbounded paid-provider use. |
| Live HVAC telemetry | Treat temperatures, setpoints, calls, runtimes, and timestamps as private household operational data. |
| Gemini/provider budget | Bound requests, input, output, concurrency, and daily consumption; fail closed when the global budget is exhausted. |
| API availability | Prevent one client or provider stall from exhausting the single EC2 backend. |
| Diagnostic response | Keep it read-only, homeowner-focused, bounded, and free of internal errors or credentials. |
| Authentication identity | Derive quotas from a verified token subject, never from a client-supplied header or body field. |
| Deployment boundary | Keep Nginx, EC2, Pi, SSM, and CI credentials outside the public assistant's authority. |

## Threats and controls

| Threat | Attack path | Impact | Required control |
|---|---|---|---|
| Unrestricted resource consumption | Scripted `POST` requests or many concurrent connections | Gemini spend, Prometheus/EC2 exhaustion, degraded dashboard | Edge IP limit, user quota, global daily cap, concurrency limit, request/time/token bounds, and metrics. |
| Identity or quota bypass | Spoofed `X-Forwarded-For`, forged user header, expired/unsigned token | A caller evades limits or acts as another user | Trust client IP only from the configured proxy chain; validate token signature, issuer, audience, and expiry; use verified `sub`. |
| Prompt injection / instruction extraction | Question asks the model to ignore its role, reveal system instructions, or invent telemetry | Misleading answer or disclosure of internal prompt/context | Treat question as untrusted content; deterministically refuse known prompt/memory/tool/policy/control requests before provider work; keep the model read-only; never grant tools or actuation; cap output; test jailbreak-shaped inputs. System instructions are guidance, not a complete security boundary. |
| Household telemetry inference | Anonymous caller repeatedly asks for current conditions or timing patterns | Occupancy/comfort information is exposed | Require auth for the Bob demo; minimize telemetry in responses; review `/api/current-temps` and Grafana exposure separately; do not log raw snapshots. |
| Secret/error leakage | Provider exception, prompt, response, or environment value reaches client/logs | API-key compromise or sensitive operational disclosure | Generic client errors; log exception type/outcome only; never log API keys, tokens, full questions, prompts, responses, or raw telemetry. |
| Provider failure and retry storm | Gemini latency/errors cause clients or the app to retry aggressively | Amplified cost and latency; cascading failure | One provider call per request, explicit timeout, no automatic application retries, bounded client retry guidance, and a circuit-breaker follow-up. |
| Request-body / slow-request abuse | Large JSON body, slow upload, or long-lived concurrent request | Worker/socket exhaustion before model limits apply | Enforce body and connection timeouts at Nginx, reject unknown fields, cap question length, and cap in-flight work. |
| Misconfigured public edge | Prometheus/Grafana or backend route is exposed without intended boundary | Telemetry or operational control-plane exposure | Keep Nginx route ownership explicit; smoke-test public routes; do not treat CORS or obscurity as access control. |
| Supply-chain / deployment confusion | A docs or config change is assumed to be live without a release gate | Policy and runtime diverge | Require CI, deploy, OpenAPI, and post-deploy smoke checks; record deployed SHA; keep secrets in SSM/GitHub secret stores only. |

## Controls already shipped

PR #226 merged the non-authenticated request guardrails:

- `question` is trimmed, must contain non-whitespace content, and is capped at
  1,000 characters.
- Unexpected request fields are rejected.
- Each Prometheus query is capped at 2 seconds; telemetry assembly is capped at
  5 seconds.
- Model context is capped at 4,000 characters.
- Gemini generation is capped at 256 output tokens and 10 seconds.
- Telemetry timeout/failure uses a safe fallback context.
- Missing configuration and provider failures return a generic error; provider
  exception text is not returned to the client.

These controls reduce blast radius but do not identify callers or stop repeated
requests. They are necessary, not sufficient, for public Bob exposure.

## Controls implemented by the observability follow-up

The global provider backstop and its telemetry are now implemented as a
process-local safety layer. It reserves provider work before Gemini, rejects
new work with a generic HTTP 429 when either 20 calls are already in flight or
500 calls have been reserved in the current UTC day, and always releases an
in-flight reservation on provider success, failure, or cancellation. The
daily cap counts attempted provider calls, including provider failures.

The backend publishes the required low-cardinality request outcome, rate-limit
scope, provider outcome, provider/request latency, input character,
estimated-output-token, in-flight, daily-budget, model, and approximate-cost
metrics. Prometheus scrapes `/metrics` through EC2 loopback; Nginx returns 404
for the public `/metrics` route. Regression tests cover daily rollover,
daily-cap rejection, concurrent-cap rejection before Gemini, label safety, and
the Nginx/Prometheus contracts.

The global layer is intentionally process-local and is not the shared
authenticated user/IP limiter required by the public Bob-demo gate.

## Prompt-injection and excessive-agency controls

The diagnostic request model exposes only the homeowner's question. Known
high-risk request shapes are rejected after authentication and per-user/IP
quota acquisition but before Prometheus context assembly or Gemini reservation.
The stable refusal does not include telemetry, prompt text, private memory, or
provider details. The guard covers attempts to:

- reveal system/developer instructions or hidden prompts;
- read private memory, session files, credentials, secrets, or tokens;
- invoke tools, shell commands, functions, or code execution;
- alter safety policy or bypass the read-only contract; and
- set, update, or otherwise control a thermostat, setpoint, temperature, HVAC
  zone, or furnace.

The deterministic guard is deliberately a narrow backstop, not a claim that
pattern matching can identify every jailbreak. The provider payload also marks
the question as untrusted user content and carries a fixed system instruction
that says the model has no tools, private-memory access, policy authority, or
thermostat-write capability. The regression suite verifies both layers: known
attack prompts stop before any telemetry/provider call, while the payload
contains no tool registration and no hidden request fields.

## Controls implemented by the auth/quota boundary

The application now accepts only a standard `Authorization: Bearer` credential
through a Cognito-backed OIDC/JWKS `TokenVerifier`. The verifier validates the
RSA signature, issuer, expiry, subject, and app-client `client_id`; a verified
principal must carry the configured diagnostic scope. Missing/invalid
credentials return `401`, a valid principal without the scope returns `403`,
and verifier outages return a generic `503`. No verifier exception can
downgrade a request to anonymous access.

Before Prometheus context assembly or Gemini work, the endpoint atomically
checks the policy's independent user and IP dimensions. Client IP extraction
trusts forwarding headers only when the direct peer is inside the configured
proxy networks and selects the configured hop from the right; untrusted
forwarding headers are ignored. The `RateLimitStore` contract reserves all
dimensions together and releases only in-flight reservations after the request
finishes, preventing a partially applied quota check.

The repository includes a deterministic in-memory store for tests and local
development plus a Redis-compatible Valkey adapter for production. Valkey is
bound to EC2 loopback and all user/IP dimensions are reserved in one atomic
Lua script. If the shared adapter is missing or unavailable, the default store
returns a generic `503`, so a production process cannot silently run without
shared quota state.

## Proposed initial quota policy

These are deliberately conservative starting values for a single-home,
portfolio-demo workload. They are policy defaults, not claims about provider
pricing. Tune them only after observing real request volume and provider usage.
All windows use UTC and all limits apply before the Gemini call.

| Caller class | Per-IP rate | Per-user rate | Per-IP daily cap | Per-user daily cap | In-flight cap |
|---|---:|---:|---:|---:|---:|
| Anonymous transitional access | 2/minute, burst 1 | N/A | 20 | N/A | 1/IP |
| Authenticated standard user | 30/minute, burst 5 | 10/minute, burst 2 | 200 | 100 | 5/IP and 2/user |
| Global service backstop | N/A | N/A | N/A | N/A | 20 total; 500 model calls/day |

Policy rules:

- Anonymous access is transitional only. It may support the existing dashboard
  while auth is being built, but it is not an acceptable Bob-demo release
  state.
- The per-IP limit is a backstop, not the identity system. Use the actual
  connection address from a trusted Nginx proxy chain; never accept an
  arbitrary `X-Forwarded-For` value from the Internet.
- The per-user key must come from a verified token subject (`sub`) after
  signature, issuer, audience, and expiry validation. A user cannot select or
  override the quota key.
- A shared limiter state is required for a release-quality deployment. An
  in-process counter is acceptable only for a short-lived local prototype; it
  resets on restart and is not sufficient once there are multiple workers or
  instances.
- Every attempted provider call counts against the daily model-call cap,
  including calls that time out or receive a provider error. This prevents
  failure storms from becoming an unmetered retry path.
- No automatic application retry is permitted. A client may retry a transient
  503 only with bounded backoff and remains subject to the same quota.
- When the global cap is reached, reject new model work until the next UTC
  window. Do not silently fall back to a more expensive or less controlled
  provider.

### Quota response contract

Target behavior before the Bob demo:

- `401` for missing or invalid authentication when authentication is required.
- `403` for a valid identity without access to the diagnostic surface.
- `429` for a per-IP, per-user, concurrency, or global quota rejection.
- `503` for provider/telemetry unavailability after the request passes quota.
- `Retry-After` is required on `429`; `RateLimit-Limit`,
  `RateLimit-Remaining`, and `RateLimit-Reset` should be supplied when the
  limiter can calculate them.
- Error bodies must be short, stable, and generic. They must not include the
  quota key, raw IP, token claims, provider status body, prompt, or traceback.

The current endpoint preserves a `200` response with an `error` field for
provider/configuration failures. A future status-code change must be versioned
or coordinated with the frontend; quota rejections must not use that success
shape because callers need to distinguish throttling from a completed request.

## Authentication contract (Cognito OIDC)

The implementation uses Amazon Cognito as the OIDC provider. The browser uses
the authorization-code flow with PKCE and a public app client; the backend
consumes the resulting access token:

- Accept a standard bearer token in the `Authorization` header, not an API key
  in the URL and not a user identity in JSON.
- Validate the RSA signature from Cognito's JWKS, issuer, `client_id` audience,
  expiry, and required subject claims.
- Use the stable `sub` claim as the quota subject. Do not use email as the
  primary key; email can change and is more identifying than necessary.
- Fail closed when the identity provider or key set cannot be validated. Do
  not silently downgrade an authenticated request to anonymous access.
- Keep the Gemini key server-side. The browser receives only the diagnostic
  response or a generic error.
- Add the security scheme and `401`/`403` responses to the generated OpenAPI
  contract and test the contract in CI.

The Cognito issuer, app-client ID, JWKS URL, and diagnostic scope are supplied
through SSM-backed deployment configuration. No Cognito client secret is sent
to or embedded in the public frontend.

## Data handling and logging rules

The backend sends the user's question and a current HVAC snapshot to Gemini.
The snapshot is household operational data even though it contains no name or
street address. Treat it as private by default.

Do:

- Assign a request ID at the edge or application boundary.
- Emit aggregate outcomes, latency, bounded input/output sizes, quota scope,
  and provider status class.
- Keep the SSM/GitHub/Gemini credentials in their existing secret stores.
- Document the provider account's applicable data-use and retention terms before
  expanding the audience beyond the existing dashboard.

Do not:

- Log the raw question, system prompt, HVAC snapshot, model response, bearer
  token, Gemini key, or full provider error body.
- Add question text, user ID, IP, or request ID as an unbounded metric label.
- Treat a successful model answer as evidence that the caller is authorized.
- Put credentials or copied production telemetry in fixtures, screenshots, or
  documentation.

## Required metrics and alerts

The abuse/cost implementation exposes low-cardinality metrics equivalent to:

- `homeops_diagnostic_requests_total{outcome,auth_state}`
- `homeops_diagnostic_rate_limited_total{scope}`
- `homeops_diagnostic_provider_calls_total{outcome}`
- `homeops_diagnostic_provider_latency_seconds`
- `homeops_diagnostic_input_chars`
- `homeops_diagnostic_output_tokens`
- `homeops_diagnostic_inflight`

Do not include IP, user subject, question, or prompt text in labels. Alert on:

- 50% and 80% of the global daily model-call cap.
- Provider failure rate above 5% over a five-minute window.
- Provider p95 latency above 8 seconds.
- Sustained quota rejection spikes or unexpected anonymous traffic.

## Release gate for the public Bob demo

Do not expose the demo until all boxes are true:

- [x] Cognito authentication method selected and token validation tested.
- [ ] IP edge limit and verified-user quota enforced before provider work.
- [x] Shared Valkey limiter state is in place.
- [ ] Global daily model-call cap and in-flight cap enforced.
- [ ] `429`, `401`, `403`, and provider failure behavior covered by tests.
- [ ] Metrics and alerts above are present without high-cardinality labels.
- [ ] Load test proves concurrent requests do not exceed the provider-call
      budget and that quota rejection happens before Gemini is called.
- [ ] OpenAPI documents the security scheme and rate-limit responses.
- [ ] Public telemetry/Grafana exposure has an explicit owner decision.
- [ ] Production deployment and public smoke checks pass on the release SHA.

## Non-goals

This policy does not authorize or design:

- HVAC control, thermostat changes, or any other actuation.
- A general-purpose chatbot, arbitrary document retrieval, or tool calling.
- A replacement for Nginx, AWS security groups, SSM, GitHub secret hygiene, or
  provider account billing controls.
- A claim that prompt instructions alone prevent jailbreaks or data leakage.

## References

- [OWASP API4:2023 — Unrestricted Resource Consumption](https://owasp.org/API-Security/editions/2023/en/0xa4-unrestricted-resource-consumption/)
- [OWASP API2:2023 — Broken Authentication](https://owasp.org/API-Security/editions/2023/en/0xa2-broken-authentication/)
- [Google Cloud — System instructions](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/prompts/system-instruction-introduction)
- [Google AI — Gemini `generateContent` API](https://ai.google.dev/api/generate-content)
