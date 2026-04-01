# Consumer Service

The consumer is a Python daemon that tails the observer's JSONL event stream in real time and emits higher-level **derived events** — floor heating-call sessions, whole-home heating sessions, thermostat/climate state changes, per-zone heating performance metrics, and in-flight overheating warnings. It is the second stage in the homeops data pipeline.

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

The consumer reads the observer's raw `state_changed` events and produces semantically richer records: when a floor starts or ends a heating call, when the furnace starts or ends a heating session, when a thermostat's setpoint, current temperature, or HVAC mode changes, when a zone reaches its setpoint (along with how long it took), when a zone overshoots or undershoots its setpoint after heating ends, when floor 2 has been calling for longer than the configured threshold (a sign that the furnace may overheat), and a daily summary of furnace runtime and outdoor temperatures.

---

## Architecture


### Module structure

The consumer is split across seven focused files:

| Module | Responsibility |
|---|---|
| `consumer.py` | Lean entry point: tail loop, event routing, signal handling, daily rollover |
| `constants.py` | Entity ID maps, env-var defaults, shared configuration constants |
| `utils.py` | `utc_ts`, `follow` (select-based tail generator), `append_jsonl`, `_parse_dt`, `_get_version` |
| `state.py` | `last_furnace_on_since` bootstrap scan, `_load_state` / `_save_state` persistence, `_empty_daily_state` initialiser |
| `processors.py` | `process_floor_event`, `process_furnace_event`, `process_climate_event`, `process_outdoor_temp_event` — pure event-to-derived-event transforms |
| `alerts.py` | `check_floor_2_warning`, `check_floor_2_escalation`, `check_observer_silence`, `write_zone_temp_snapshot` — in-flight periodic checks |
| `reporting.py` | `emit_daily_summary`, `format_daily_summary_message` — end-of-day summary generation and Telegram formatting |

---

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
| `climate.floor_1_thermostat` | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1`, `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_undershoot.v1` |
| `climate.floor_2_thermostat` | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1`, `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_undershoot.v1` |
| `climate.floor_3_thermostat` | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1`, `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_undershoot.v1` |

Additionally, `furnace_daily_summary.v1` is emitted once per UTC calendar day at the first event after midnight, followed immediately by three `floor_daily_summary.v1` events (one per floor).

### Derived event emission

Every derived event is:

1. Printed to **stdout** with `flush=True` for real-time visibility.
2. Appended to `DERIVED_EVENT_LOG` via `append_jsonl()`, which creates parent directories if they do not exist.

---

## Event Schema

> **Full authoritative schema reference:** [`docs/event-schemas/consumer-events.md`](../../docs/event-schemas/consumer-events.md)
>
> That document contains complete field tables with source/rationale columns, design notes, and planned (not-yet-implemented) events. The sections below are the working reference for the 16 currently implemented event types.

The consumer emits fifteen derived event types. All share a common envelope.

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

### `homeops.consumer.thermostat_setpoint_changed.v1`

Emitted when a climate entity's `temperature` attribute (the heating setpoint) changes from its previously observed value.

The three thermostat change events (`thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`) share the same `data` payload.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID (e.g. `"climate.floor_2_thermostat"`) |
| `data.zone` | string | Zone identifier: `"floor_1"`, `"floor_2"`, or `"floor_3"` |
| `data.ts` | string (ISO 8601 UTC) | Timestamp from the original observer event (distinct from top-level `ts`) |
| `data.hvac_mode` | string \| null | Top-level HA climate mode (e.g. `"heat"`, `"off"`) |
| `data.hvac_action` | string \| null | Current HVAC action (e.g. `"heating"`, `"idle"`) |
| `data.setpoint` | float \| null | The new setpoint value that triggered this event |
| `data.current_temp` | float \| null | Measured temperature at the time of the change |

**Example:**

```json
{
  "schema": "homeops.consumer.thermostat_setpoint_changed.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T06:30:00.221400+00:00",
  "data": {
    "entity_id": "climate.floor_2_thermostat",
    "zone": "floor_2",
    "ts": "2026-03-19T06:30:00.000000+00:00",
    "hvac_mode": "heat",
    "hvac_action": "heating",
    "setpoint": 69.0,
    "current_temp": 65.5
  }
}
```

---

### `homeops.consumer.thermostat_current_temp_updated.v1`

Emitted when a climate entity's `current_temperature` attribute changes from its previously observed value. Uses the same `data` payload as `thermostat_setpoint_changed.v1`.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.ts` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.hvac_mode` | string \| null | Top-level HA climate mode |
| `data.hvac_action` | string \| null | Current HVAC action |
| `data.setpoint` | float \| null | Current setpoint at time of update |
| `data.current_temp` | float \| null | The new temperature value that triggered this event |

**Example:**

```json
{
  "schema": "homeops.consumer.thermostat_current_temp_updated.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T06:45:22.774900+00:00",
  "data": {
    "entity_id": "climate.floor_1_thermostat",
    "zone": "floor_1",
    "ts": "2026-03-19T06:45:22.500000+00:00",
    "hvac_mode": "heat",
    "hvac_action": "heating",
    "setpoint": 68.0,
    "current_temp": 66.0
  }
}
```

---

### `homeops.consumer.thermostat_mode_changed.v1`

Emitted when a climate entity's `hvac_mode` (top-level HA state) or `hvac_action` attribute changes from its previously observed values. Uses the same `data` payload as the other thermostat events.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.ts` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.hvac_mode` | string \| null | The (possibly changed) top-level HA climate mode |
| `data.hvac_action` | string \| null | The (possibly changed) current HVAC action |
| `data.setpoint` | float \| null | Current setpoint at time of mode change |
| `data.current_temp` | float \| null | Current measured temperature at time of mode change |

**Example:**

```json
{
  "schema": "homeops.consumer.thermostat_mode_changed.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T08:10:04.339200+00:00",
  "data": {
    "entity_id": "climate.floor_3_thermostat",
    "zone": "floor_3",
    "ts": "2026-03-19T08:10:04.100000+00:00",
    "hvac_mode": "off",
    "hvac_action": "idle",
    "setpoint": 65.0,
    "current_temp": 68.5
  }
}
```

---

### `homeops.consumer.thermostat_setpoint_reached.v1`

Emitted the first time `current_temperature >= setpoint` is observed for a zone while `hvac_action` is `"heating"` and the previous reading was below setpoint. This is the "zone satisfied" signal and also triggers `zone_time_to_temp.v1` (see below).

Uses the same `data` payload as the other thermostat events.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.ts` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.hvac_mode` | string \| null | Top-level HA climate mode at crossing time |
| `data.hvac_action` | string \| null | Current HVAC action (always `"heating"` when this fires) |
| `data.setpoint` | float \| null | The setpoint that was reached |
| `data.current_temp` | float \| null | The temperature at the moment of crossing |

**Example:**

```json
{
  "schema": "homeops.consumer.thermostat_setpoint_reached.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T07:43:12.004821+00:00",
  "data": {
    "entity_id": "climate.floor_1_thermostat",
    "zone": "floor_1",
    "ts": "2026-03-19T07:43:11.800000+00:00",
    "hvac_mode": "heat",
    "hvac_action": "heating",
    "setpoint": 68.0,
    "current_temp": 68.1
  }
}
```

---

### `homeops.consumer.zone_time_to_temp.v1`

Emitted alongside `thermostat_setpoint_reached.v1` when the consumer has a tracked heating session start for the zone (i.e. it observed the `hvac_action` transition to `"heating"`). This is the primary per-zone heating performance metric.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.start_temp` | float | Temperature when `hvac_action` first became `"heating"` this session |
| `data.setpoint` | float | Target temperature |
| `data.setpoint_delta` | float | `setpoint - start_temp`: how many degrees the zone needed to gain |
| `data.duration_s` | integer | Seconds from session start to setpoint crossed |
| `data.end_temp` | float | Actual temperature at the moment of setpoint crossing |
| `data.degrees_gained` | float | `end_temp - start_temp` |
| `data.degrees_per_min` | float | `degrees_gained / (duration_s / 60)`: normalised rise rate |
| `data.outdoor_temp_f` | float \| null | Last known outdoor temperature at emission time; `null` if no reading yet |
| `data.other_zones_calling` | array[string] | Floor-call entity IDs of other zones that were calling at session start |

**Example:**

```json
{
  "schema": "homeops.consumer.zone_time_to_temp.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T07:43:12.004821+00:00",
  "data": {
    "entity_id": "climate.floor_1_thermostat",
    "zone": "floor_1",
    "start_temp": 64.5,
    "setpoint": 68.0,
    "setpoint_delta": 3.5,
    "duration_s": 1140,
    "end_temp": 68.1,
    "degrees_gained": 3.6,
    "degrees_per_min": 0.189,
    "outdoor_temp_f": 28.4,
    "other_zones_calling": ["binary_sensor.floor_3_heating_call"]
  }
}
```

---

### `homeops.consumer.zone_overshoot.v1`

Emitted when a heating session ends (`hvac_action` leaves `"heating"`) and setpoint was **already reached** before the session ended. Captures the lag between the thermostat satisfying its call and the furnace/damper fully shutting off.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.start_temp` | float \| null | Temperature when `hvac_action` became `"heating"` |
| `data.setpoint` | float \| null | Target temperature |
| `data.setpoint_delta` | float \| null | `setpoint - start_temp`; `null` if either is unavailable |
| `data.end_temp` | float \| null | Temperature when `hvac_action` left `"heating"` |
| `data.overshoot_s` | integer | Seconds from setpoint-reached to session end |
| `data.peak_temp` | float \| null | Highest temperature observed between setpoint-reached and session end; `null` if only one reading in that window |
| `data.outdoor_temp_f` | float \| null | Last known outdoor temperature at emission time |
| `data.other_zones_calling` | array[string] | Floor-call entity IDs of other zones that were calling at session start |

**Example:**

```json
{
  "schema": "homeops.consumer.zone_overshoot.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T08:03:54.118400+00:00",
  "data": {
    "entity_id": "climate.floor_2_thermostat",
    "zone": "floor_2",
    "start_temp": 63.0,
    "setpoint": 68.0,
    "setpoint_delta": 5.0,
    "end_temp": 69.5,
    "overshoot_s": 210,
    "peak_temp": 69.5,
    "outdoor_temp_f": 31.0,
    "other_zones_calling": []
  }
}
```

---

### `homeops.consumer.zone_undershoot.v1`

Emitted when a heating session ends (`hvac_action` leaves `"heating"`) and setpoint was **never reached** during the session. The `likely_cause` field distinguishes a deliberate setpoint adjustment from an unexplained early shutdown.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.start_temp_f` | float \| null | Temperature when `hvac_action` became `"heating"` |
| `data.final_temp_f` | float \| null | Temperature when `hvac_action` left `"heating"` |
| `data.setpoint_f` | float \| null | The target temperature that was not reached |
| `data.shortfall_f` | float | `setpoint_f - final_temp_f`: degrees below setpoint at session end |
| `data.call_duration_s` | integer | Seconds from session start to session end |
| `data.outdoor_temp_f` | float \| null | Last known outdoor temperature at emission time |
| `data.likely_cause` | string | `"thermostat_adjustment"` if the setpoint changed during the active heating session; `"unknown"` otherwise |

**Example:**

```json
{
  "schema": "homeops.consumer.zone_undershoot.v1",
  "source": "consumer.v1",
  "ts": "2026-03-19T05:22:44.903100+00:00",
  "data": {
    "entity_id": "climate.floor_3_thermostat",
    "zone": "floor_3",
    "start_temp_f": 62.0,
    "final_temp_f": 66.5,
    "setpoint_f": 68.0,
    "shortfall_f": 1.5,
    "call_duration_s": 2880,
    "outdoor_temp_f": 14.2,
    "likely_cause": "unknown"
  }
}
```

---

### `homeops.consumer.furnace_daily_summary.v1`

Emitted once per UTC calendar day at the first observer event with a new date (i.e. just after midnight UTC). Summarises the previous day's accumulated furnace and floor runtime.

| Field | Type | Description |
|---|---|---|
| `data.date` | string (`YYYY-MM-DD`) | The day being summarised (the day that just ended) |
| `data.total_furnace_runtime_s` | integer | Total furnace on-time for the day in seconds |
| `data.session_count` | integer | Number of complete furnace runs recorded |
| `data.per_floor_runtime_s` | object | `{"floor_1": int, "floor_2": int, "floor_3": int}` — total floor call duration per zone in seconds; zones with no calls have value `0` |
| `data.outdoor_temp_min_f` | float \| null | Coldest outdoor reading of the day; `null` if no readings received |
| `data.outdoor_temp_max_f` | float \| null | Warmest outdoor reading of the day; `null` if no readings received |

**Example:**

```json
{
  "schema": "homeops.consumer.furnace_daily_summary.v1",
  "source": "consumer.v1",
  "ts": "2026-03-20T00:00:04.112700+00:00",
  "data": {
    "date": "2026-03-19",
    "total_furnace_runtime_s": 18420,
    "session_count": 7,
    "per_floor_runtime_s": {
      "floor_1": 12600,
      "floor_2": 9000,
      "floor_3": 5400
    },
    "outdoor_temp_min_f": 22.1,
    "outdoor_temp_max_f": 38.6
  }
}
```

---

### `homeops.consumer.floor_daily_summary.v1`

Emitted three times per UTC calendar day rollover (once per floor: `floor_1`, `floor_2`, `floor_3`), immediately after `furnace_daily_summary.v1`. Summarises each floor's heating call activity for the previous day.

| Field | Type | Description |
|---|---|---|
| `data.floor` | string | Floor name: `"floor_1"`, `"floor_2"`, or `"floor_3"` |
| `data.date` | string (`YYYY-MM-DD`) | The day being summarised |
| `data.total_calls` | integer | Number of completed heating calls for this floor |
| `data.total_runtime_s` | integer | Sum of all call durations in seconds |
| `data.avg_duration_s` | float \| null | Mean call duration in seconds; `null` if no calls |
| `data.max_duration_s` | integer \| null | Longest single call duration in seconds; `null` if no calls |
| `data.outdoor_temp_avg_f` | float \| null | Average outdoor temperature for the day; `null` if no readings received |

**Example:**

```json
{
  "schema": "homeops.consumer.floor_daily_summary.v1",
  "source": "consumer.v1",
  "ts": "2026-01-16T00:00:04.882100+00:00",
  "data": {
    "floor": "floor_2",
    "date": "2026-01-15",
    "total_calls": 3,
    "total_runtime_s": 7200,
    "avg_duration_s": 2400.0,
    "max_duration_s": 2900,
    "outdoor_temp_avg_f": 30.4
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
| `FLOOR_2_TELEGRAM_RATE_LIMIT_S` | `3600` | Minimum seconds between floor-2 Telegram alerts. Suppresses duplicate Telegram messages within the window; events always emit to JSONL. (default: 1 hour) |
| `OBSERVER_SILENCE_THRESHOLD_S` | `600` | Seconds of no observer events before a silence warning is emitted (default: 10 min) |
| `TELEGRAM_BOT_TOKEN` | _(unset)_ | Telegram Bot API token for overheating alerts |
| `TELEGRAM_CHAT_ID` | _(unset)_ | Telegram chat ID to receive overheating alerts |

---

## Log Rotation

The consumer's JSONL derived event log (`state/consumer/events.jsonl`) will grow unbounded without rotation. A logrotate config is provided in `deploy/logrotate/` to handle this automatically.

See [`deploy/logrotate/README.md`](../../deploy/logrotate/README.md) for install and test instructions.

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
