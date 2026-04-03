# HomeOps Dashboard — Backend (FastAPI)

FastAPI backend for homeops.now. Serves live HVAC data from the HomeOps Raspberry Pi to the public dashboard.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/api/current-temps` | Live temps for all zones + outdoor sensor |

### `GET /api/current-temps`

```json
{
  "zones": {
    "floor_1": {
      "zone": "floor_1",
      "current_temp_f": 70,
      "setpoint_f": 68,
      "hvac_mode": "heat",
      "hvac_action": "idle",
      "last_updated": "2026-04-02T20:00:00Z"
    },
    "floor_2": { ... },
    "floor_3": { ... }
  },
  "outdoor_temp_f": 52.5,
  "outdoor_last_updated": "2026-04-02T19:59:00Z",
  "fetched_at": 1743638400.0
}
```

## Architecture

Reads from the Pi over Tailscale SSH: tails `state/consumer/events.jsonl`, parses the latest `thermostat_*` and `outdoor_temp_updated` events. Results are cached for 30s (configurable) to avoid hammering the Pi.

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `PI_HOST` | `100.115.21.72` | Pi Tailscale IP |
| `PI_SSH_USER` | `bob` | SSH user on Pi |
| `PI_SSH_KEY_PATH` | `/app/keys/id_ed25519` | Path to private key (mount as volume) |
| `PI_EVENTS_PATH` | `/home/leachd/repos/homeops/state/consumer/events.jsonl` | Events log on Pi |
| `CACHE_TTL_SECONDS` | `30` | Seconds to cache Pi data |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |

## Local Development

```bash
cd dashboard/backend
pip install -r requirements.txt
PI_SSH_KEY_PATH=~/.ssh/id_ed25519 uvicorn main:app --reload
```

## Docker

```bash
docker build -t homeops-backend .
docker run -p 8000:8000 \
  -v ~/.ssh/id_ed25519:/app/keys/id_ed25519:ro \
  -e PI_HOST=100.115.21.72 \
  homeops-backend
```

## Tests

```bash
python3 -m pytest tests/ -v
```

26 tests covering endpoint shape, error handling, event parsing, and cache behaviour.
