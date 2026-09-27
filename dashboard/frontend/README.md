# HomeOps Frontend

React + Vite + Tailwind single-page dashboard for `homeops.now`.

## Interfaces

- Reads live telemetry from `VITE_API_URL/api/current-temps`.
- The response retains the legacy heating fields and additionally provides
  `floor_1/2/3_cooling_call`, `ac_cooling_active`, and
  `floor_1/2/3_hvac_action` (`heating`, `cooling`, `idle`, or `null` when
  unavailable); cooling state is thermostat-derived, not compressor feedback.
- Zone cards and the live summary consume `floor_1/2/3_hvac_action` as the
  authoritative mode. They show distinct heating, cooling, idle, and
  unavailable states; they never infer heating from the legacy `*_call`
  booleans. Cooling targets use directional `Cooling to ...` copy, while the
  existing heating presentation remains unchanged. Failed or stale snapshots
  are labeled as unavailable rather than showing an active mode.
- Embeds the four provisioned Grafana dashboards from
  `VITE_GRAFANA_URL` (default: `https://api.homeops.now/grafana`).
- Sends homeowner diagnostic questions to `VITE_API_URL/api/diagnostic`; the
  endpoint requires a Cognito OIDC access token with the diagnostic scope.
- Renders the existing HVAC dashboard at `/` and the Fleet Deploy Lab at
  `/deploy`. The fleet route reads `VITE_API_URL/deploy/api/fleet`, groups all
  twelve explicitly simulated targets by TEST, STAGE, and PROD, and displays
  compact vehicle-themed cards with short target IDs, observed profiles,
  health, and desired-versus-observed state readable without relying on color;
  full target identifiers and digests remain available in an on-demand details
  disclosure. Its bounded control
  plane lets a visitor configure and submit only the finite `DeploymentSpec` to
  `VITE_API_URL/deploy/api/deployments/submit`. The form generates the
  deployment attempt ID automatically, keeps it in tab-scoped session storage
  for reloads and safe retries, and rotates it when a visitor starts a new
  deployment or edits an already-submitted attempt. Target selection shows the
  resolved short labels (`TEST-01` through `PROD-04`) and highlights the
  matching fleet cards; switching modes clears inactive targeting so the visible
  summary always matches the payload. Repeating an unchanged attempt is
  therefore safe at the backend's idempotent boundary. It never sends a GitHub
  or protected fleet-management credential to the browser. The form consumes
  the backend-published capability matrix and exposes only the verified
  Python/Ansible + all-at-once/abort paths; rolling, canary, and rollback remain
  visibly unavailable until their execution semantics are implemented and
  proven. Submitted runs retain a frozen request summary, classify known
  service/busy/validation/commit/dispatch failures with credential-safe next
  steps, and keep older results in an explicit Previous attempt area when the
  form is edited. Run polling continues until the workflow conclusion and job
  rows are current together, including after a reload.

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
