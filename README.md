# homeops

Home Assistant runtime plus a lightweight observer service for streaming state changes from the Home Assistant WebSocket API.

## Repository Layout

```text
homeops/
├── compose/
│   └── docker-compose.yml
├── services/
│   └── observer/
│       ├── observer.py
│       └── requirements.txt
├── secrets/
│   └── ha.env            # local only, gitignored
└── README.md
```

## Components

### Home Assistant (`compose/docker-compose.yml`)

- Runs `ghcr.io/home-assistant/home-assistant:stable`
- Uses host networking and restarts automatically
- Persists Home Assistant config to:
  - `/home/leachd/srv/homeops/homeassistant/config`

### Observer (`services/observer/observer.py`)

- Connects to Home Assistant WebSocket API
- Authenticates with long-lived access token
- Subscribes to `state_changed` events
- Emits one JSON line per matching update to stdout
- Reconnects automatically with exponential backoff

Example output:

```json
{"ts":"2026-02-20T17:22:11.123456+00:00","entity_id":"binary_sensor.furnace_heating","old_state":"off","new_state":"on"}
```

## Prerequisites

- Docker + Docker Compose
- Python 3.11+
- A Home Assistant long-lived access token

## Setup

1. Start Home Assistant:

```bash
cd compose
docker compose up -d
```

2. Create observer virtualenv and install dependencies:

```bash
cd ../services/observer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Create `secrets/ha.env` (already gitignored):

```dotenv
HA_BASE_URL=http://127.0.0.1:8123
HA_WS_URL=ws://127.0.0.1:8123/api/websocket
HA_TOKEN=<your_long_lived_token>
WATCH_ENTITIES=binary_sensor.furnace_heating,binary_sensor.floor_1_heating_call
```

## Running the Observer

From repo root:

```bash
cd services/observer
source .venv/bin/activate
python observer.py
```

Optional override for env file path:

```bash
HA_ENV_FILE=../../secrets/ha.env python observer.py
```

## Environment Variables

- `HA_WS_URL`: Home Assistant WebSocket endpoint.
- `HA_TOKEN`: Long-lived Home Assistant access token.
- `WATCH_ENTITIES`: Optional comma-separated entity IDs to filter events. If empty, prints all entity state changes.
- `HA_ENV_FILE`: Optional dotenv path for observer config (default: `secrets/ha.env`).

## Security Notes

- Never commit secrets from `secrets/`.
- Treat `HA_TOKEN` like a password.
- If a token is exposed, revoke it in Home Assistant and create a new one immediately.

## Operational Notes

- Observer logs status/reconnect messages to stderr.
- Observer event data is newline-delimited JSON on stdout, suitable for piping into log processors or other services.
