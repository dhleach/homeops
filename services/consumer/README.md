# Consumer Service

The consumer is a Python daemon that tails the observer's JSONL event stream in real time and emits higher-level **derived events** — floor heating-call sessions, whole-home heating sessions, and in-flight overheating warnings. It is the second stage in the homeops data pipeline.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Event Schema](#event-schema)
- [In-Flight Floor-2 Warning](#in-flight-floor-2-warning)
- [Bootstrap Behavior](#bootstrap-behavior)
- [Configuration Reference](#configuration-reference)
- [Quickstart](#quickstart)

---

## Overview

```
observer
  events.jsonl  (append-only JSONL)
       │
       ▼
  consumer.py  ──►  stdout (derived JSONL)
               ──►  DERIVED_EVENT_LOG (append-only JSONL file)
               ──►  Telegram alert  (floor-2 long-call only)
```

The consumer reads the observer's raw `state_changed` events and produces semantically richer records: when a floor starts or ends a heating call, when the furnace starts or ends a heating session, and when floor 2 has been calling for longer than the configured threshold (a sign that the furnace may overheat).

---

## Architecture

### Tail loop

The consumer uses a non-blocking `follow()` generator backed by `select.select` to tail the observer log file. The generator yields:

- A JSON string whenever a new line is appended to the file.
- `None` on each timeout interval (default 60 s), which allows the in-flight warning check to run even when no new events arrive.

This approach avoids busy-polling and works correctly even when the observer and consumer run as separate systemd services on the same Pi.

### Event consumption

The consumer filters for `schema == "homeops.observer.state_changed.v1"` and ignores everything else. It then routes each event by `entity_id`:

| Entity ID | Derived events produced |
|---|---|
| `binary_sensor.floor_1_heating_call` | `floor_call_started.v1`, `floor_call_ended.v1` |
| `binary_sensor.floor_2_heating_call` | `floor_call_started.v1`, `floor_call_ended.v1` |
| `binary_sensor.floor_3_heating_call` | `floor_call_started.v1`, `floor_call_ended.v1` |
| `binary_sensor.furnace_heating` | `heating_session_started.v1`, `heating_session_ended.v1` |
| `sensor.outdoor_temperature` | `outdoor_temp_updated.v1` |

### Derived event emission

Every derived event is:

1. Printed to **stdout** with `flush=True` for real-time visibility.
2. Appended to `DERIVED_EVENT_LOG` via `append_jsonl()`, which creates parent directories if they do not exist.

---

## Event Schema

The consumer emits six derived event types. All share a common envelope.

### Common envelope

| Field | Type | Description |
|---|---|---|
| `schema` | string | Event type identifier (see below) |
| `source` | string | Always `"consumer.v1"` |
| `ts` | string (ISO 8601 UTC) | Timestamp when the consumer emitted the event |
| `data` | object | Event-specific payload (see each type below) |

---

### `homeops.consumer.floor_call_started.v1`

Emitted when a floor transitions from `off` → `on`.

| Field | Type | Description |
|---|---|---|
| `data.floor` | string | Floor identifier: `"floor_1"`, `"floor_2"`, or `"floor_3"` |
| `data.started_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | Home Assistant entity ID |

**Example:**

```json
{
  "schema": "homeops.consumer.floor_call_started.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T14:00:00.123456+00:00",
  "data": {
    "floor": "floor_2",
    "started_at": "2026-03-19T14:00:00.000000+00:00",
    "entity_id": "binary_sensor.floor_2_heating_call"
  }
}
```

---

### `homeops.consumer.floor_call_ended.v1`

Emitted when a floor transitions from `on` → `off`.

| Field | Type | Description |
|---|---|---|
| `data.floor` | string | Floor identifier: `"floor_1"`, `"floor_2"`, or `"floor_3"` |
| `data.ended_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | Home Assistant entity ID |
| `data.duration_s` | integer \| null | Call duration in seconds, or `null` if the start was not observed in this run |

**Example:**

```json
{
  "schema": "homeops.consumer.floor_call_ended.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T15:12:30.456789+00:00",
  "data": {
    "floor": "floor_2",
    "ended_at": "2026-03-19T15:12:30.000000+00:00",
    "entity_id": "binary_sensor.floor_2_heating_call",
    "duration_s": 4350
  }
}
```

---

### `homeops.consumer.heating_session_started.v1`

Emitted when the furnace transitions from `off` → `on`.

| Field | Type | Description |
|---|---|---|
| `data.started_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | Always `"binary_sensor.furnace_heating"` |

**Example:**

```json
{
  "schema": "homeops.consumer.heating_session_started.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T14:00:05.000000+00:00",
  "data": {
    "started_at": "2026-03-19T14:00:05.000000+00:00",
    "entity_id": "binary_sensor.furnace_heating"
  }
}
```

---

### `homeops.consumer.heating_session_ended.v1`

Emitted when the furnace transitions from `on` → `off`.

| Field | Type | Description |
|---|---|---|
| `data.ended_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | Always `"binary_sensor.furnace_heating"` |
| `data.duration_s` | integer \| null | Furnace run duration in seconds, or `null` if the start was not observed in this run |

**Example:**

```json
{
  "schema": "homeops.consumer.heating_session_ended.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T14:08:15.000000+00:00",
  "data": {
    "ended_at": "2026-03-19T14:08:15.000000+00:00",
    "entity_id": "binary_sensor.furnace_heating",
    "duration_s": 490
  }
}
```

---

### `homeops.consumer.floor_2_long_call_warning.v1`

Emitted once per floor-2 call when the elapsed call duration exceeds `FLOOR_2_WARN_THRESHOLD_S`. See [In-Flight Floor-2 Warning](#in-flight-floor-2-warning) for full details.

| Field | Type | Description |
|---|---|---|
| `data.floor` | string | Always `"floor_2"` |
| `data.elapsed_s` | integer | Seconds floor 2 has been calling at the time of the warning |
| `data.threshold_s` | integer | Configured threshold in seconds |
| `data.entity_id` | string | Always `"binary_sensor.floor_2_heating_call"` |

**Example:**

```json
{
  "schema": "homeops.consumer.floor_2_long_call_warning.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T14:45:10.000000+00:00",
  "data": {
    "floor": "floor_2",
    "elapsed_s": 2703,
    "threshold_s": 2700,
    "entity_id": "binary_sensor.floor_2_heating_call"
  }
}
```

---

### `homeops.consumer.outdoor_temp_updated.v1`

Emitted on every state change from `sensor.outdoor_temperature`. Events with an `unavailable`, `unknown`, or non-numeric state are logged as warnings and skipped.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Always `"sensor.outdoor_temperature"` |
| `data.temperature_f` | float | Current outdoor temperature in °F |
| `data.timestamp` | string (ISO 8601 UTC) | Timestamp from the original observer event |

**Example:**

```json
{
  "schema": "homeops.consumer.outdoor_temp_updated.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T14:00:00.123456+00:00",
  "data": {
    "entity_id": "sensor.outdoor_temperature",
    "temperature_f": 38.6,
    "timestamp": "2026-03-19T14:00:00.000000+00:00"
  }
}
```

---

## In-Flight Floor-2 Warning

Floor 2 has only 3 vents. When it calls for heat for an extended period the furnace blasts through a small number of open vents, which can trip the furnace high-limit switch (Code 4) and eventually trigger a lockout (Code 7, 3-hour auto-reset).

The consumer checks the elapsed duration of an active floor-2 call on **every loop iteration** — both when a new event arrives and on each `select.select` timeout. This means the check fires even during quiet periods with no new sensor events.

**Logic:**

1. When floor 2 starts a new call, `floor_2_warn_sent` is reset to `False`.
2. On each loop iteration, if floor 2 is currently active, the consumer computes `elapsed_s = now - floor_on_since["binary_sensor.floor_2_heating_call"]`.
3. If `elapsed_s >= FLOOR_2_WARN_THRESHOLD_S` and `floor_2_warn_sent` is `False`:
   - Emits a `floor_2_long_call_warning.v1` derived event.
   - Sends a Telegram message (if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set).
   - Sets `floor_2_warn_sent = True` to prevent duplicate alerts for the same call.

The warning fires **at most once per floor-2 call session**, regardless of how long the call continues.

---

## Bootstrap Behavior

When the consumer starts it calls `last_furnace_on_since()` to scan the observer log in reverse and determine whether the furnace is currently mid-session. This prevents a spurious `heating_session_started` event if the furnace was already on when the consumer (re)started.

`last_furnace_on_since()` returns:

- The `datetime` of the most recent `off → on` furnace transition if the furnace appears to be on.
- `None` if the most recent furnace event was an `on → off` transition, or if the log is empty or unreadable.

Floor call start times are **not** bootstrapped — if the consumer restarts mid-call, `duration_s` for that call will be `null` in the `floor_call_ended` event.

---

## Configuration Reference

All configuration is via environment variables.

| Variable | Default | Description |
|---|---|---|
| `EVENT_LOG` | `state/observer/events.jsonl` | Path to the observer's output JSONL file to tail |
| `DERIVED_EVENT_LOG` | `state/consumer/events.jsonl` | Path to write derived events (created if absent) |
| `FLOOR_2_WARN_THRESHOLD_S` | `2700` | Seconds a floor-2 call must be active before a warning is emitted (default: 45 min) |
| `TELEGRAM_BOT_TOKEN` | _(unset)_ | Telegram Bot API token for overheating alerts |
| `TELEGRAM_CHAT_ID` | _(unset)_ | Telegram chat ID to receive overheating alerts |

---

## Quickstart

**Prerequisites:** Python 3.11+, `python-dateutil` (`pip install -r requirements.txt`), observer service running and writing to `state/observer/events.jsonl`.

**Run directly:**

```bash
cd /home/leachd/repos/homeops

# Minimal — no Telegram alerts
EVENT_LOG=state/observer/events.jsonl \
DERIVED_EVENT_LOG=state/consumer/events.jsonl \
python3 services/consumer/consumer.py

# With floor-2 Telegram alerts
EVENT_LOG=state/observer/events.jsonl \
DERIVED_EVENT_LOG=state/consumer/events.jsonl \
FLOOR_2_WARN_THRESHOLD_S=2700 \
TELEGRAM_BOT_TOKEN=<bot-token> \
TELEGRAM_CHAT_ID=<chat-id> \
python3 services/consumer/consumer.py
```

**As a systemd service:** See the Pi Baseline setup documentation for the `homeops-consumer.service` unit file.
