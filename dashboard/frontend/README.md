# HomeOps Frontend

React + Vite + Tailwind single-page dashboard for `homeops.now`.

## Interfaces

- Reads live telemetry from `VITE_API_URL/api/current-temps`.
- Embeds the four provisioned Grafana dashboards from
  `VITE_GRAFANA_URL` (default: `https://api.homeops.now/grafana`).
- Sends homeowner diagnostic questions to `VITE_API_URL/api/diagnostic`; the
  endpoint requires a Cognito OIDC access token with the diagnostic scope.

The production build is created by
`.github/workflows/frontend-deploy.yml`, synced to the private S3 frontend
bucket, invalidated through CloudFront, and verified with the public release
smoke checks. The deployment and route map is in
[`docs/deployment.md`](../../docs/deployment.md).

The recruiter-facing Bob evaluation dashboard is published at
[`/bob/evals/`](https://homeops.now/bob/evals/). It is a reviewed static
snapshot under `public/bob/evals/`, copied from the canonical
`openclaw-config/dashboard/evaluation/` bundle at the recorded source commit.
The deterministic report is authoritative; the adjacent scripted live-trial
fixture is explicitly optional and non-gating. Refreshing this snapshot
requires a review of the source repository and its redaction/public-safety
tests before copying the bundle.

## Local development

```bash
npm ci
npm run dev
```

Set `VITE_API_URL`, `VITE_GRAFANA_URL`, `VITE_OIDC_AUTHORITY`,
`VITE_OIDC_CLIENT_ID`, and `VITE_OIDC_SCOPE` when pointing the local frontend
at a different backend or identity configuration. Tests run with
`NODE_ENV=test npm test`.

The browser uses authorization code + PKCE through `oidc-client-ts`; it stores
the short-lived session in browser session storage and sends only the access
token to the diagnostic endpoint. No client secret is included in the build.
If OIDC metadata is absent, the widget shows a configuration message instead
of issuing a request that would predictably receive `401`.
