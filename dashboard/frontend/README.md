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
  the backend-published capability matrix and exposes the verified
  Python + all-at-once/abort, Python + canary/abort, Python + canary/rollback, and Ansible +
  all-at-once/abort paths. Canary remains hidden when Ansible is selected until
  equivalent serial behavior is implemented and proven. Python rollback keeps
  the run failed while showing verified restoration or partial state. The form can also
  select one resolved simulated target for the deterministic verification
  failure demonstration; the target switch cannot reach Home Assistant or
  normal HomeOps infrastructure. Submitted runs retain a frozen request summary, classify known
  service/busy/validation/commit/dispatch failures with credential-safe next
  steps, and keep older results in an explicit Previous attempt area when the
  form is edited. Editing rotates only the next form attempt: a tracked active
  run remains visible and continues polling while the new request is prepared.
  The bounded previous-attempt record retains status, workflow/error evidence,
  target verification, timestamps, and manifest/workflow links across reloads.
  Run polling continues until the workflow conclusion and job rows are current
  together, including after a reload. The run panel also renders a traceable deployment timeline: GitHub Actions
  jobs expose their real step conclusions, skipped downstream jobs are labeled
  as skipped after an upstream failure, and server-recorded Python deployer
  events retain their target IDs, status, detail, and recording order across
  reloads. Green evidence is shown only when it comes from an Actions step or
  stored simulator event. The route also reads the bounded durable
  `/deploy/api/deployments/history` list, showing selector, implementation,
  artifact digest, outcome, timestamps, manifest commit, and Actions run link;
  selecting a history row reopens that exact deployment read. The header's
  Reset simulated fleet control submits the closed full-fleet `operation:
  "reset"` DeploymentSpec through the same manifest/workflow/apply/readback
  path and explains the default one-reset-per-UTC-day cap. The route remains
  bounded at phone widths, exposes visible keyboard focus, explains disabled
  controls, announces live target/run changes, and uses contrast-checked text
  labels instead of color alone. It sets a route-specific browser title and
  exposes quiet links to the exact frontend source and README. CI injects the
  full `GITHUB_SHA` as `VITE_BUILD_SHA`; the deployed route links that commit
  only when the value is a valid full revision, while local builds omit the
  build-revision claim. Fleet snapshot read time is labeled separately from
  deployment-run and workflow timestamps.

The production build is created by
`.github/workflows/frontend-deploy.yml`, synced to the private S3 frontend
bucket, invalidated through CloudFront, and verified with the public release
smoke checks. The deployment and route map is in
[`docs/deployment.md`](../../docs/deployment.md).

The `/deploy` route imports the checked-in `deploy_demo/deployer.py` and
`ansible/deploy.yml` files as build-time raw source, so its implementation tabs
cannot drift from the code shipped with that frontend build. A full
`VITE_BUILD_SHA` pins the GitHub source links to the same revision; local builds
fall back to `master`. The implementation source disclosure is collapsed by
default so the deployment controls remain easy to find; expanding it preserves
the form state while exposing the selected source. The trusted Fleet workflow publishes the selected
implementation and its checked-in source path in the completed Actions run
summary, and the run panel links that proof separately from the frozen request
parameters.

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
at a different backend or identity configuration. `VITE_BUILD_SHA` is optional
for local development and should only be set to a known full 40-character
commit SHA. Tests run with `NODE_ENV=test npm test`.

The browser uses authorization code + PKCE through `oidc-client-ts`; it stores
the short-lived session in browser session storage and sends only the access
token to the diagnostic endpoint. No client secret is included in the build.
If OIDC metadata is absent, the widget shows a configuration message instead
of issuing a request that would predictably receive `401`.
