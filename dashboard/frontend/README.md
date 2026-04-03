# HomeOps Dashboard — Frontend (React)

Vite + React + Tailwind CSS landing page for homeops.now. Shows live HVAC data for all 3 floors and the outdoor sensor, auto-refreshing every 30 seconds.

## Pages

**`/`** — Live temperature dashboard
- Floor 1 / 2 / 3 temp cards with HVAC status badges (heating / idle)
- Outdoor temp card
- Live indicator with last-refresh time + manual refresh button
- "View Full Dashboard" → Grafana
- About section explaining the system architecture

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_URL` | `` (relative) | API base URL — empty = proxied in dev, set to `https://api.homeops.now` in prod |
| `VITE_GRAFANA_URL` | `#` | Link for the "View Full Dashboard" button |

## Local Development

```bash
cd dashboard/frontend
NODE_ENV=development npm install
npm run dev          # starts Vite dev server on :5173, proxies /api → localhost:8000
```

## Build

```bash
npm run build        # outputs to dist/
```

The `dist/` folder is deployed to S3 via GitHub Actions on every push to `dashboard/frontend/**`.

## Tests

```bash
NODE_ENV=development npm test
```

23 tests across StatusBadge, TempCard, OutdoorCard, and ErrorBanner components.
