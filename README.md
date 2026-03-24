# homeops

A Raspberry Pi system that monitors a 3-zone HVAC setup and prevents furnace overheating — with real-time Telegram alerts and a structured event pipeline built on top of Home Assistant.

## The Problem

Floor 2 has only 3 vents. When it calls for heat for an extended period, the furnace blasts through too few open vents, overheats, and trips the high-limit switch (Code 4/7). The result: a multi-hour furnace lockout and a cold house.

Home Assistant alone can't prevent this. It sees state changes; it doesn't reason about them. **homeops** does.

## What It Does

- **Real-time overheating prevention** — tracks how long floor 2 has been calling for heat; fires a Telegram alert before the limit switch trips (configurable threshold, default 45 min)
- **14 derived event types** from raw HA state changes — floor call sessions, furnace sessions, thermostat setpoint/mode/temp changes, outdoor temperature, and a daily furnace summary
- **Heating cycle analytics** — `zone_time_to_temp` (how fast each zone heats), `zone_overshoot` (how far past setpoint the zone runs), `zone_undershoot` (calls that fail to reach setpoint, with a `likely_cause` field)
- **Thermostat entity tracking** — setpoint changes, mode changes, current temp updates, and setpoint-reached events per zone
- **Event-driven pipeline** — observer writes raw `state_changed` events to JSONL; consumer tails that file and emits semantically rich derived events downstream
- **Schema-versioned events** — every event carries a `schema` field (e.g. `homeops.consumer.floor_2_long_call_warning.v1`) for safe downstream evolution
- **Production-grade operations** — runs as `systemd` services on the Pi, log rotation via `logrotate`, exponential-backoff reconnects on the WebSocket
- **95+ pytest tests**, GitHub Actions CI, Ruff lint/format enforcement on every PR

## Architecture

```
Home Assistant
  WebSocket API
       │
       ▼
  observer.py  ──► state/observer/events.jsonl  (raw JSONL, append-only)
                               │
                               ▼
                         consumer.py
                               │
                 ┌─────────────┼─────────────┐
                 ▼             ▼             ▼
        state/consumer/   stdout        Telegram alert
        events.jsonl                  (floor-2 long call)
       (derived events)
```

**Observer** connects to the Home Assistant WebSocket API, subscribes to `state_changed` events for configured entities, and writes one JSON line per event to a JSONL log. It reconnects automatically with exponential backoff.

**Consumer** tails the observer log in real time using a non-blocking `select`-based follow loop. It routes each event by entity ID, maintains per-zone heating session state, and emits higher-level derived events to its own JSONL log and stdout. The timeout-driven loop ensures the floor-2 warning fires even during quiet periods with no sensor events.

Both services run as independent `systemd` units on the same Pi and communicate only through the shared JSONL file — no message broker, no database.

## Event Types

The consumer emits **14 derived event types**:

| Category | Events |
|---|---|
| Floor heating calls | `floor_call_started.v1`, `floor_call_ended.v1` (×3 zones) |
| Furnace sessions | `heating_session_started.v1`, `heating_session_ended.v1` |
| Thermostat state | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1` |
| Heating performance | `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_undershoot.v1` |
| Environmental | `outdoor_temp_updated.v1` |
| Alerting | `floor_2_long_call_warning.v1` |
| Summaries | `furnace_daily_summary.v1` |

All events share a common envelope (`schema`, `source`, `ts`, `data`) and are written as newline-delimited JSON.

Full schema reference: [`docs/event-schemas/consumer-events.md`](docs/event-schemas/consumer-events.md)
Consumer service detail: [`services/consumer/README.md`](services/consumer/README.md)

**Example — floor-2 overheating warning:**

```json
{
  "schema": "homeops.consumer.floor_2_long_call_warning.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T09:32:18.005600+00:00",
  "data": {
    "floor": "floor_2",
    "elapsed_s": 2714,
    "threshold_s": 2700,
    "entity_id": "binary_sensor.floor_2_heating_call"
  }
}
```

**Example — zone heating performance:**

```json
{
  "schema": "homeops.consumer.zone_time_to_temp.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T07:43:12.004821+00:00",
  "data": {
    "zone": "floor_1",
    "start_temp": 64.5,
    "setpoint": 68.0,
    "setpoint_delta": 3.5,
    "duration_s": 1140,
    "degrees_per_min": 0.189,
    "outdoor_temp_f": 28.4,
    "other_zones_calling": ["binary_sensor.floor_3_heating_call"]
  }
}
```

## Repository Layout

```text
homeops/
├── compose/
│   └── docker-compose.yml        # Home Assistant container
├── services/
│   ├── observer/
│   │   ├── observer.py
│   │   ├── requirements.txt
│   │   └── README.md
│   └── consumer/
│       ├── consumer.py
│       ├── requirements.txt
│       └── README.md             # full consumer reference
├── docs/
│   └── event-schemas/
│       └── consumer-events.md    # authoritative event schema reference
├── deploy/
│   └── logrotate/                # logrotate config for JSONL files
├── state/
│   ├── observer/events.jsonl     # runtime output, gitignored
│   └── consumer/events.jsonl    # derived events, gitignored
├── .githooks/
│   └── pre-commit
├── pyproject.toml                # Ruff lint/format config
└── secrets/
    └── ha.env                    # local only, gitignored
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

2. Create a Python virtualenv and install dependencies:

```bash
cd services/observer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r ../consumer/requirements.txt
pip install ruff
```

Both services share one virtualenv for local development. Runtime dependencies are split across service-level `requirements.txt` files for clearer deployment ownership.

3. Create `secrets/ha.env` (already gitignored):

```dotenv
HA_BASE_URL=http://127.0.0.1:8123
HA_WS_URL=ws://127.0.0.1:8123/api/websocket
HA_TOKEN=<your_long_lived_token>
WATCH_ENTITIES=binary_sensor.furnace_heating,binary_sensor.floor_1_heating_call,binary_sensor.floor_2_heating_call,binary_sensor.floor_3_heating_call,climate.floor_1_thermostat,climate.floor_2_thermostat,climate.floor_3_thermostat,sensor.outdoor_temperature
OBSERVER_EVENT_LOG=state/observer/events.jsonl
```

4. Enable the pre-commit hook:

```bash
git config core.hooksPath .githooks
```

## Running

**Observer** (from repo root):

```bash
HA_ENV_FILE=secrets/ha.env services/observer/.venv/bin/python services/observer/observer.py
```

**Consumer** (from repo root):

```bash
EVENT_LOG=state/observer/events.jsonl \
DERIVED_EVENT_LOG=state/consumer/events.jsonl \
FLOOR_2_WARN_THRESHOLD_S=2700 \
TELEGRAM_BOT_TOKEN=<bot-token> \
TELEGRAM_CHAT_ID=<chat-id> \
services/observer/.venv/bin/python services/consumer/consumer.py
```

Omit the `TELEGRAM_*` variables to run without alerts.

Run both services in separate terminals or deploy as `systemd` units.

## Environment Variables

### Observer

| Variable | Description |
|---|---|
| `HA_WS_URL` | Home Assistant WebSocket endpoint |
| `HA_TOKEN` | Long-lived Home Assistant access token |
| `WATCH_ENTITIES` | Comma-separated entity IDs to filter (empty = all entities) |
| `HA_ENV_FILE` | Path to dotenv file (default: `secrets/ha.env`) |
| `OBSERVER_EVENT_LOG` | JSONL append path for observer output |

### Consumer

| Variable | Default | Description |
|---|---|---|
| `EVENT_LOG` | `state/observer/events.jsonl` | Observer output file to tail |
| `DERIVED_EVENT_LOG` | `state/consumer/events.jsonl` | Derived event output path |
| `FLOOR_2_WARN_THRESHOLD_S` | `2700` | Seconds before floor-2 overheating alert fires (45 min) |
| `TELEGRAM_BOT_TOKEN` | _(unset)_ | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | _(unset)_ | Telegram chat ID for alerts |

## Development

### Linting and Formatting

Ruff is configured via `pyproject.toml`.

Check:

```bash
services/observer/.venv/bin/ruff check services/
services/observer/.venv/bin/ruff format --check services/
```

Apply fixes:

```bash
services/observer/.venv/bin/ruff format services/
services/observer/.venv/bin/ruff check --fix services/
```

### Tests

```bash
cd services
../services/observer/.venv/bin/python -m pytest
```

95+ tests cover observer reconnect logic, consumer event derivation, floor-2 warning timing, thermostat tracking, and heating cycle analytics.

### CI

GitHub Actions runs Ruff lint and format checks on every PR and push to `master`. PRs that fail lint are blocked from merging.

### Branching

- `master` — stable, production-ready. All PRs merge here.
- `feature/<short-description>` — new features
- `fix/<short-description>` — bug fixes
- `docs/<short-description>` — documentation

All commits must pass Ruff checks (enforced by `.githooks/pre-commit` and CI).

## Security Notes

- Never commit secrets from `secrets/`.
- Treat `HA_TOKEN` like a password. If one is exposed, revoke it in Home Assistant immediately and generate a new one.
