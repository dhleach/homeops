# Observer Service

The observer is a lightweight Python daemon that connects to the Home Assistant WebSocket API, filters for a configurable set of HVAC sensor entities, and streams structured JSONL events to stdout and an optional append-only log file. It is the first stage in the homeops data pipeline: raw sensor state changes become a durable, machine-readable event stream that downstream consumers can tail in real time.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Event Schema](#event-schema)
- [Entity Reference](#entity-reference)
- [Configuration Reference](#configuration-reference)
- [Quickstart](#quickstart)

---

## Overview

```
Home Assistant
   WebSocket API
        │
        ▼
  observer.py  ──►  stdout (JSONL)
                ──►  OBSERVER_EVENT_LOG (append-only JSONL file)
```

The observer subscribes to `state_changed` events on the Home Assistant WebSocket bus, applies an optional entity allowlist (`WATCH_ENTITIES`), and emits one JSON line per matching transition. If the connection drops for any reason the service reconnects automatically with exponential backoff (1 s → 2 s → … → 30 s cap).

Diagnostic messages (connection status, auth results, warnings) go to **stderr** so that stdout remains a clean, parseable event stream.

---

## Architecture

### WebSocket handshake

On each connection attempt the observer performs the standard Home Assistant WebSocket authentication sequence:

1. Wait for `{"type": "auth_required"}` from HA.
2. Send `{"type": "auth", "access_token": "<HA_TOKEN>"}`.
3. Confirm `{"type": "auth_ok"}` before subscribing.
4. Subscribe to `event_type: state_changed` with a single subscription message (`id: 1`).

WebSocket keepalives are sent every 20 seconds (`ping_interval=20`, `ping_timeout=20`) to detect silent TCP drops early.

### Entity filtering

If `WATCH_ENTITIES` is set, the observer maintains an in-memory allowlist (a Python `set`). Events whose `entity_id` is not in the set are silently dropped before emission. An empty or unset `WATCH_ENTITIES` passes **all** entity state changes through — useful for discovery but not recommended in production.

### Event emission

Each qualifying state change produces one JSON object written to stdout with `flush=True` so that downstream pipe consumers (including the consumer service) receive events without buffering delay. If `OBSERVER_EVENT_LOG` is configured, the same line is also appended to that file. File write failures are logged to stderr and do not interrupt the stdout stream.

### Reconnect logic

Any `ConnectionClosed` or `OSError` during the WebSocket session is caught and triggers a reconnect loop. General exceptions are also caught so that unexpected errors (malformed HA messages, auth rejections) do not silently kill the process. Backoff doubles on each failed attempt and is capped at 30 seconds; it resets to 1 second on a successful connection.

---

## Event Schema

The observer emits a single event type. All fields are present on every record.

### `homeops.observer.state_changed.v1`

Emitted whenever a watched entity transitions between states.

| Field | Type | Description |
|---|---|---|
| `schema` | string | Always `"homeops.observer.state_changed.v1"` |
| `source` | string | Always `"ha.websocket"` |
| `ts` | string (ISO 8601 UTC) | Timestamp when the observer received the event |
| `data.entity_id` | string | Home Assistant entity ID (e.g. `binary_sensor.furnace_heating`) |
| `data.old_state` | string \| null | Previous entity state (e.g. `"off"`) |
| `data.new_state` | string \| null | New entity state (e.g. `"on"`) |

**Example:**

```json
{
  "schema": "homeops.observer.state_changed.v1",
  "source": "ha.websocket",
  "ts": "2026-03-19T14:32:07.481234+00:00",
  "data": {
    "entity_id": "binary_sensor.floor_2_heating_call",
    "old_state": "off",
    "new_state": "on"
  }
}
```

> **Note:** The consumer service reads this stream and derives higher-level events (`floor_call_started.v1`, `floor_call_ended.v1`, `heating_session_started.v1`, `heating_session_ended.v1`, `floor_2_long_call_warning.v1`). See `services/consumer/` for details.

---

## Entity Reference

The default `WATCH_ENTITIES` set covers four binary sensors that together describe the state of a three-zone forced-air heating system.

| Entity ID | Friendly Name | Floor / Zone | Represents |
|---|---|---|---|
| `binary_sensor.furnace_heating` | Furnace Heating | Whole home | `on` when the furnace burner is actively firing |
| `binary_sensor.floor_1_heating_call` | Floor 1 Heating Call | Floor 1 | `on` when the floor-1 zone thermostat is calling for heat |
| `binary_sensor.floor_2_heating_call` | Floor 2 Heating Call | Floor 2 | `on` when the floor-2 zone thermostat is calling for heat |
| `binary_sensor.floor_3_heating_call` | Floor 3 Heating Call | Floor 3 | `on` when the floor-3 zone thermostat is calling for heat |

All four are `binary_sensor` entities with states `"on"` / `"off"`. The furnace sensor tracks the actual burner; the per-floor sensors track zone damper/thermostat calls. A floor can be calling for heat while the furnace is temporarily off (e.g. between cycles), so the two dimensions are independent.

---

## Configuration Reference

The observer loads environment variables from a dotenv file, then allows process-level environment variables to override them. The dotenv file path defaults to `secrets/ha.env` and can be changed with `HA_ENV_FILE`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `HA_WS_URL` | Yes | — | Home Assistant WebSocket endpoint, e.g. `ws://127.0.0.1:8123/api/websocket` |
| `HA_TOKEN` | Yes | — | Long-lived access token created in the HA user profile |
| `HA_ENV_FILE` | No | `secrets/ha.env` | Path to the dotenv file that contains `HA_WS_URL` and `HA_TOKEN` |
| `WATCH_ENTITIES` | No | `""` (all entities) | Comma-separated list of entity IDs to watch. Empty string passes all entities. |
| `OBSERVER_EVENT_LOG` | No | — (disabled) | Filesystem path for the append-only JSONL event log. Parent directories are created automatically. |

### Example `secrets/observer.env`

```dotenv
WATCH_ENTITIES=binary_sensor.furnace_heating,binary_sensor.floor_1_heating_call,binary_sensor.floor_2_heating_call,binary_sensor.floor_3_heating_call
HA_ENV_FILE=secrets/ha.env
OBSERVER_EVENT_LOG=state/observer/events.jsonl
```

### Example `secrets/ha.env`

```dotenv
HA_WS_URL=ws://127.0.0.1:8123/api/websocket
HA_TOKEN=<your_long_lived_ha_token>
```

---

## Quickstart

### Prerequisites

- Python 3.11+
- A running Home Assistant instance with WebSocket API access
- A long-lived access token (HA → Profile → Long-Lived Access Tokens)

### Install dependencies

```bash
cd services/observer
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure secrets

```bash
cp secrets/ha.env.example secrets/ha.env
cp secrets/observer.env.example secrets/observer.env
# Edit both files and fill in HA_WS_URL and HA_TOKEN
```

### Run

```bash
HA_ENV_FILE=secrets/observer.env python observer.py
```

Events are written to stdout as they arrive:

```
[2026-03-19T14:32:05.123456+00:00] Connecting to ws://127.0.0.1:8123/api/websocket
[2026-03-19T14:32:05.201234+00:00] Auth OK
[2026-03-19T14:32:05.210000+00:00] Subscribed. Watching: binary_sensor.floor_1_heating_call, binary_sensor.floor_2_heating_call, ...
{"schema": "homeops.observer.state_changed.v1", "source": "ha.websocket", "ts": "2026-03-19T14:32:07.481234+00:00", "data": {"entity_id": "binary_sensor.furnace_heating", "old_state": "off", "new_state": "on"}}
```

(Diagnostic lines appear on stderr; only JSONL events appear on stdout.)

### Pipe to the consumer

```bash
HA_ENV_FILE=secrets/observer.env python observer.py | python services/consumer/consumer.py
```

Or let each service manage its own log file and use `OBSERVER_EVENT_LOG` / `EVENT_LOG` to decouple them.
