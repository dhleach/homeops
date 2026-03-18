# homeops

Home Assistant runtime plus two lightweight Python services:
- `observer`: streams Home Assistant `state_changed` events
- `consumer`: derives higher-level heating/floor session events

## Repository Layout

```text
homeops/
├── compose/
│   └── docker-compose.yml
├── services/
│   ├── observer/
│   │   ├── observer.py
│   │   └── requirements.txt
│   └── consumer/
│       ├── consumer.py
│       └── requirements.txt
├── state/
│   ├── observer/events.jsonl   # local runtime output, gitignored
│   └── consumer/events.jsonl   # derived event output, gitignored
├── .githooks/
│   └── pre-commit
├── pyproject.toml              # Ruff lint/format config
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
- Optionally appends events to a local JSONL log file
- Reconnects automatically with exponential backoff

Example output:

```json
{"schema":"homeops.observer.state_changed.v1","source":"ha.websocket","ts":"2026-02-23T03:32:00.607550+00:00","data":{"entity_id":"binary_sensor.floor_1_heating_call","old_state":"on","new_state":"off"}}
```

### Consumer (`services/consumer/consumer.py`)

- Tails observer JSONL events (`EVENT_LOG`)
- Emits derived events to stdout and `DERIVED_EVENT_LOG`
- Tracks:
  - per-floor heating call sessions (`floor_call_started/ended`)
  - furnace heating sessions (`heating_session_started/ended`)
- Computes durations in seconds when start/end timestamps are available

Example derived output:

```json
{"schema":"homeops.consumer.floor_call_ended.v1","source":"consumer.v1","ts":"2026-02-23T04:33:14.293259+00:00","data":{"floor":"floor_2","ended_at":"2026-02-23T04:33:14.225552+00:00","entity_id":"binary_sensor.floor_2_heating_call","duration_s":36}}
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

2. Create Python virtualenv and install dependencies:

```bash
cd ../services/observer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r ../consumer/requirements.txt
pip install ruff
```

For local development, this uses one shared virtualenv for both services.
Runtime dependencies are still split across service-level `requirements.txt` files for clearer ownership/deployment.

3. Create `secrets/ha.env` (already gitignored):

```dotenv
HA_BASE_URL=http://127.0.0.1:8123
HA_WS_URL=ws://127.0.0.1:8123/api/websocket
HA_TOKEN=<your_long_lived_token>
WATCH_ENTITIES=binary_sensor.furnace_heating,binary_sensor.floor_1_heating_call
OBSERVER_EVENT_LOG=state/observer/events.jsonl
DERIVED_EVENT_LOG=state/consumer/events.jsonl
```

## Running the Observer

From repo root:

```bash
cd services/observer
source .venv/bin/activate
HA_ENV_FILE=../../secrets/ha.env python observer.py
```

Alternative from repo root:

```bash
HA_ENV_FILE=secrets/ha.env services/observer/.venv/bin/python services/observer/observer.py
```

## Running the Consumer

From repo root:

```bash
services/observer/.venv/bin/python services/consumer/consumer.py
```

The consumer defaults to:
- `EVENT_LOG=state/observer/events.jsonl`
- `DERIVED_EVENT_LOG=state/consumer/events.jsonl`

Run both services together in separate terminals:

1. Observer
```bash
cd services/observer
source .venv/bin/activate
HA_ENV_FILE=../../secrets/ha.env python observer.py
```
2. Consumer
```bash
cd ../consumer
../observer/.venv/bin/python consumer.py
```

## Environment Variables

- `HA_WS_URL`: Home Assistant WebSocket endpoint.
- `HA_TOKEN`: Long-lived Home Assistant access token.
- `WATCH_ENTITIES`: Optional comma-separated entity IDs to filter events. If empty, prints all entity state changes.
- `HA_ENV_FILE`: Optional dotenv path for observer config (default: `secrets/ha.env`).
- `OBSERVER_EVENT_LOG`: Optional JSONL append path for persisted observer events.
- `EVENT_LOG`: Consumer input log path (default: `state/observer/events.jsonl`).
- `DERIVED_EVENT_LOG`: Consumer output log path (default: `state/consumer/events.jsonl`).

## Linting And Formatting

Ruff is configured via `pyproject.toml`.

Manual checks:

```bash
services/observer/.venv/bin/ruff format --check --diff services/observer/observer.py services/consumer/consumer.py
services/observer/.venv/bin/ruff check services/observer/observer.py services/consumer/consumer.py
```

Apply fixes:

```bash
services/observer/.venv/bin/ruff format services/observer/observer.py services/consumer/consumer.py
services/observer/.venv/bin/ruff check --fix services/observer/observer.py services/consumer/consumer.py
```

Pre-commit hook:
- Path: `.githooks/pre-commit`
- Behavior: preview-only (`--check --diff` / `--fix --diff`) and blocks commit when changes are required.
- Enable once per clone:

```bash
git config core.hooksPath .githooks
```

## Security Notes

- Never commit secrets from `secrets/`.
- Treat `HA_TOKEN` like a password.
- If a token is exposed, revoke it in Home Assistant and create a new one immediately.

## Operational Notes

- Observer logs status/reconnect messages to stderr.
- Observer event data is newline-delimited JSON on stdout, suitable for piping into log processors or other services.
- If `OBSERVER_EVENT_LOG` is set, each emitted line is also appended to that file.
- Consumer prints incoming observer transitions and writes derived session events to `DERIVED_EVENT_LOG`.


## Continuous Integration (Ruff Lint)

A GitHub Actions workflow `Ruff Lint` was added to run Ruff (Python linter/formatter) on PRs and pushes to master. It checks formatting and lint rules for the observer and consumer services so style issues are caught by CI.

If you run locally, recommended steps to reproduce the CI checks:

1. Create a venv: `python3 -m venv .venv`
2. Activate: `source .venv/bin/activate`
3. Install ruff: `pip install ruff`
4. Run checks: `ruff format --check --diff services/observer/observer.py services/consumer/consumer.py`
   and `ruff check services/observer/observer.py services/consumer/consumer.py`

If the CI flags formatting issues, either apply fixes locally with `ruff format` and push, or let CI report the check failures for review.
