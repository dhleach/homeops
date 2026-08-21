# HomeOps Frontend

React + Vite + Tailwind single-page dashboard for `homeops.now`.

## Interfaces

- Reads live telemetry from `VITE_API_URL/api/current-temps`.
- Embeds the four provisioned Grafana dashboards from
  `VITE_GRAFANA_URL` (default: `https://api.homeops.now/grafana`).
- Sends homeowner diagnostic questions to `VITE_API_URL/api/diagnostic`.

The production build is created by
`.github/workflows/frontend-deploy.yml`, synced to the private S3 frontend
bucket, invalidated through CloudFront, and verified with the public release
smoke checks. The deployment and route map is in
[`docs/deployment.md`](../../docs/deployment.md).

## Local development

```bash
npm ci
npm run dev
```

Set `VITE_API_URL` and `VITE_GRAFANA_URL` when pointing the local frontend at a
different backend or dashboard host. Tests run with `NODE_ENV=test npm test`.
