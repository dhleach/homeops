# Consumer Service

The design policy for future short-cycle mitigation is documented in
[docs/mitigation-policy.md](../../docs/mitigation-policy.md); it is not an
active Home Assistant control path.

The consumer is a Python daemon that tails the observer's JSONL event stream in real time and emits higher-level **derived events** — floor heating- and cooling-call sessions, whole-home heating and inferred-AC sessions, thermostat/climate state changes, per-zone heating and cooling performance metrics, mitigation decisions, automatic mitigation rollbacks, in-flight overheating warnings, and (when explicitly enabled) bounded LLM explanations for validated runtime anomalies. It is the second stage in the homeops data pipeline.

For the host/network boundary around this service, see the repository-level
[`docs/architecture.md`](../../docs/architecture.md) and
[`docs/deployment.md`](../../docs/deployment.md).

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Event Schema](#event-schema)
- [Data model reference](#data-model-reference)
- [In-Flight Floor-2 Warning](#in-flight-floor-2-warning)
- [Proactive Anomaly Insight](#proactive-anomaly-insight)
- [Thermal prediction target contract](#thermal-prediction-target-contract)
- [Thermal prediction feature schema](#thermal-prediction-feature-schema)
- [Thermal prediction evaluation protocol](#thermal-prediction-evaluation-protocol)
- [Thermal prediction dataset export](#thermal-prediction-dataset-export)
- [Read-only multi-zone scheduling query](#read-only-multi-zone-scheduling-query)
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
               ──►  Telegram alerts  (warnings, anomaly insights, and rollback)
```

The consumer reads the observer's raw `state_changed` events and explicit Home Assistant event records. It produces semantically richer records: when a floor starts or ends a heating or cooling call, when the furnace or inferred AC demand starts or ends a whole-home session, when a thermostat's setpoint, current temperature, or HVAC mode changes, when a zone reaches its directional heating or cooling target (along with how long it took), when a zone overshoots after heating or undershoots after cooling, when a heating or cooling call misses its target, when floor 2 has been calling for longer than the configured threshold (a sign that the furnace may overheat), when a staged mitigation decision was applied or skipped, when repeated short cycling causes the mitigation guard to roll back, and a daily summary of heating/cooling runtime and outdoor temperatures. A validated floor-runtime anomaly can optionally trigger a separate plain-English explanation without allowing the model to control Home Assistant.

---

## Architecture


### Module structure

The consumer is split across ten focused modules:

| Module | Responsibility |
|---|---|
| `consumer.py` | Lean entry point: tail loop, event routing, signal handling, daily rollover |
| `constants.py` | Entity ID maps, env-var defaults, shared configuration constants |
| `utils.py` | `utc_ts`, `follow` (select-based tail generator), `append_jsonl`, `_parse_dt`, `_get_version` |
| `state.py` | `last_furnace_on_since` / `last_ac_cooling_on_since` bootstrap scans, `_load_state` / `_save_state` persistence, `_empty_daily_state` initialiser, and forward-compatible daily-state backfill |
| `processors.py` | `process_floor_event`, `process_cooling_floor_event`, `process_furnace_event`, `process_cooling_session_event`, `process_climate_event`, `process_outdoor_temp_event`, `process_mitigation_event`, `process_mitigation_rollback_event` — pure event-to-derived-event transforms |
| `alerts.py` | `check_floor_2_warning`, `check_floor_2_escalation`, `check_observer_silence`, `write_zone_temp_snapshot` — in-flight periodic checks |
| `reporting.py` | `emit_daily_summary`, `emit_floor_daily_summaries`, `format_daily_summary_message` — end-of-day heating/cooling summary generation and Telegram formatting |
| `metrics.py` | `HvacMetrics` — Prometheus heating/cooling gauge definitions, update helpers, and HTTP server (port 8001); foundation for the homeops.now public dashboard data pipeline |
| `hvac_context.py` | HVAC context summarizer — reads `state.json` + `events.jsonl` and outputs a structured plain-text summary of current conditions, zone runtimes, recent sessions, and warnings for LLM input; lookback and daily-summary dates share an explicit UTC reference time |
| `proactive_insight.py` | Provider-neutral anomaly explanation coordinator — validates/allowlists triggers, bounds context/output/provider calls, delivers through Telegram, and persists successful insight IDs for replay deduplication |

---

### Tail loop

The consumer uses a non-blocking `follow()` generator backed by `select.select` to tail the observer log file. The generator yields:

- A JSON string whenever a new line is appended to the file.
- `None` on each timeout interval (default 60 s), which allows the in-flight warning check to run even when no new events arrive.

This approach avoids busy-polling and works correctly even when the observer and consumer run as separate systemd services on the same Pi.

### Event consumption

The consumer routes `homeops.observer.state_changed.v1` records by `entity_id`
and translates `homeops.observer.event.v1` mitigation records by their
`event_type`:

| Entity ID | Derived events produced |
|---|---|
| `binary_sensor.floor_1_heating_call` | `floor_call_started.v1`, `floor_call_ended.v1` |
| `binary_sensor.floor_2_heating_call` | `floor_call_started.v1`, `floor_call_ended.v1` |
| `binary_sensor.floor_3_heating_call` | `floor_call_started.v1`, `floor_call_ended.v1` |
| `binary_sensor.furnace_heating` | `heating_session_started.v1`, `heating_session_ended.v1` |
| `binary_sensor.floor_1_cooling_call` | `cooling_call_started.v1`, `cooling_call_ended.v1` |
| `binary_sensor.floor_2_cooling_call` | `cooling_call_started.v1`, `cooling_call_ended.v1` |
| `binary_sensor.floor_3_cooling_call` | `cooling_call_started.v1`, `cooling_call_ended.v1` |
| `binary_sensor.ac_cooling` | `cooling_session_started.v1`, `cooling_session_ended.v1` |
| `sensor.outdoor_temperature` | `outdoor_temp_updated.v1` |
| `climate.floor_1_thermostat` | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1`, `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_setpoint_miss.v1`, `zone_slow_to_heat_warning.v1`, `thermostat_cooling_session_started.v1`, `thermostat_cooling_session_ended.v1`, `thermostat_cooling_setpoint_reached.v1`, `zone_time_to_cool.v1`, `zone_cooling_setpoint_miss.v1`, `zone_cooling_undershoot.v1` |
| `climate.floor_2_thermostat` | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1`, `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_setpoint_miss.v1`, `zone_slow_to_heat_warning.v1`, `thermostat_cooling_session_started.v1`, `thermostat_cooling_session_ended.v1`, `thermostat_cooling_setpoint_reached.v1`, `zone_time_to_cool.v1`, `zone_cooling_setpoint_miss.v1`, `zone_cooling_undershoot.v1` |
| `climate.floor_3_thermostat` | `thermostat_setpoint_changed.v1`, `thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`, `thermostat_setpoint_reached.v1`, `zone_time_to_temp.v1`, `zone_overshoot.v1`, `zone_setpoint_miss.v1`, `zone_slow_to_heat_warning.v1`, `thermostat_cooling_session_started.v1`, `thermostat_cooling_session_ended.v1`, `thermostat_cooling_setpoint_reached.v1`, `zone_time_to_cool.v1`, `zone_cooling_setpoint_miss.v1`, `zone_cooling_undershoot.v1` |
| `homeops.observer.event.v1` (`event_type=homeops.mitigation.zone_stagger_applied.v1`) | `homeops.mitigation.zone_stagger_applied.v1` |
| `homeops.observer.event.v1` (`event_type=homeops.mitigation.rollback.v1`) | `homeops.mitigation.rollback.v1` plus an urgent Telegram alert when configured |

Additionally, `furnace_daily_summary.v1` is emitted once per UTC calendar day at the first event after midnight, followed immediately by three `floor_daily_summary.v1` events (one per floor). Both summary schemas retain their heating fields and add cooling runtime/count fields; cooling values represent inferred thermostat demand, not compressor telemetry.

### Derived event emission

Every derived event is:

1. Printed to **stdout** with `flush=True` for real-time visibility.
2. Appended to `DERIVED_EVENT_LOG` via `append_jsonl()`, which creates parent directories if they do not exist.

---

## Event Schema

> **Full authoritative schema reference:** [`docs/event-schemas/consumer-events.md`](../../docs/event-schemas/consumer-events.md)
>
> That document contains complete field tables with source/rationale columns and design notes. The sections below are the working reference for the currently implemented event types.

The consumer emits 38 derived event types. All share a common envelope. The
authoritative list is maintained in `docs/event-schemas/consumer-events.md`; the
working sections below cover the most frequently inspected event payloads.

### Common envelope

| Field | Type | Description |
|---|---|---|
| `schema` | string | Event type identifier (see below) |
| `source` | string | Always `"consumer.v1"` |
| `ts` | string (ISO 8601 UTC) | Timestamp when the consumer emitted the event |
| `data` | object | Event-specific payload (see each type below) |

### `homeops.mitigation.zone_stagger_applied.v1`

Emitted when the staged Home Assistant zone-stagger automation records an
applied or skipped resume decision. The top-level `event_type` repeats the
Home Assistant event name so consumers that do not key on `schema` can route
the record directly.

| Field | Type | Description |
|---|---|---|
| `event_type` | string | Always `"homeops.mitigation.zone_stagger_applied.v1"` |
| `data.event_type` | string | Same stable Home Assistant event name |
| `data.zone` | string | `floor_1`, `floor_2`, or `floor_3` |
| `data.reason` | string | Decision reason, such as `secondary_zone_call_during_furnace_warmup` or `resume_gate_failed` |
| `data.delay_minutes` | number | Configured stagger delay captured before the pause |
| `data.trigger_event_id` | string | Home Assistant state-trigger context ID, or the trigger ID fallback |
| `data.incident_id` | string (optional) | Durable HA incident identifier for the active storm window |
| `data.attempt_number` | int (optional) | 1-based zone-stagger attempt number, capped at 3 |
| `data.outcome` | string | `applied` or `skipped` |

Invalid mitigation payloads are logged and skipped; they never enter the
derived event log. Observer playback handles the same event shape, so a
consumer restart can recover a decision from `state/observer/events.jsonl`.

**Example:**

```json
{
  "schema": "homeops.mitigation.zone_stagger_applied.v1",
  "event_type": "homeops.mitigation.zone_stagger_applied.v1",
  "source": "consumer.v1",
  "ts": "2026-08-25T13:00:00.000000+00:00",
  "data": {
    "event_type": "homeops.mitigation.zone_stagger_applied.v1",
    "zone": "floor_2",
    "reason": "secondary_zone_call_during_furnace_warmup",
    "delay_minutes": 5,
    "trigger_event_id": "0123456789abcdef",
    "outcome": "applied"
  }
}
```

### `homeops.mitigation.rollback.v1`

Emitted when the staged Home Assistant overlay disables its mitigation guard
after a continued short-cycle event follows three zone-stagger attempts in
one incident window. The observer preserves the HA event, and the consumer
validates/appends it before sending an urgent Telegram alert through the
configured `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The durable event is
written even when Telegram is unavailable or not configured.

| Field | Type | Description |
|---|---|---|
| `event_type` | string | Always `homeops.mitigation.rollback.v1` |
| `data.event_type` | string | Same stable rollback event name |
| `data.incident_id` | string | Durable incident identifier captured by the HA overlay |
| `data.failed_attempts` | int | Number of recorded attempts at rollback; must be at least `3` |
| `data.reason` | string | Why the continued short cycle caused rollback |
| `data.trigger_event_id` | string | Unique short-cycle event reference used for deduplication |
| `data.storm_window_started_at` | ISO 8601 string | Start of the active mitigation incident window |
| `data.mitigation_enabled` | boolean | Always `false` after rollback |
| `data.rollback_state` | string | Always `rolled_back` |
| `data.source_event_type` | string | Always `homeops.mitigation.short_cycle_detected.v1` |
| `data.short_cycle_duration_s` | number (optional) | Duration from the triggering short-cycle event |
| `data.short_cycle_threshold_s` | number (optional) | Threshold used by the triggering detector |

Invalid rollback records are rejected. Duplicate rollback events with the
same `trigger_event_id` are not appended or alerted twice during playback.

**Example:**

```json
{
  "schema": "homeops.mitigation.rollback.v1",
  "event_type": "homeops.mitigation.rollback.v1",
  "source": "consumer.v1",
  "ts": "2026-08-25T13:00:00.000000+00:00",
  "data": {
    "event_type": "homeops.mitigation.rollback.v1",
    "incident_id": "0123456789abcdef",
    "failed_attempts": 3,
    "reason": "short_cycle_after_three_mitigation_attempts",
    "trigger_event_id": "fedcba9876543210",
    "storm_window_started_at": "2026-08-25T12:00:00+00:00",
    "mitigation_enabled": false,
    "rollback_state": "rolled_back",
    "source_event_type": "homeops.mitigation.short_cycle_detected.v1"
  }
}
```

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

### `homeops.consumer.cooling_call_started.v1`

Emitted when a per-floor cooling-call helper transitions from `off` → `on`.
The helper represents inferred thermostat demand, not direct compressor telemetry.

| Field | Type | Description |
|---|---|---|
| `data.floor` | string | Floor identifier: `"floor_1"`, `"floor_2"`, or `"floor_3"` |
| `data.started_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | One of the `binary_sensor.floor_*_cooling_call` entities |

**Example:**

```json
{
  "schema": "homeops.consumer.cooling_call_started.v1",
  "source": "consumer.v1",
  "ts": "2026-07-15T14:00:00.123456+00:00",
  "data": {
    "floor": "floor_1",
    "started_at": "2026-07-15T14:00:00.000000+00:00",
    "entity_id": "binary_sensor.floor_1_cooling_call"
  }
}
```

---

### `homeops.consumer.cooling_call_ended.v1`

Emitted when a per-floor cooling-call helper transitions from `on` → `off`.

| Field | Type | Description |
|---|---|---|
| `data.floor` | string | Floor identifier |
| `data.ended_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | One of the `binary_sensor.floor_*_cooling_call` entities |
| `data.duration_s` | integer \| null | Call duration, or `null` when the matching start was not observed in this run |

**Example:**

```json
{
  "schema": "homeops.consumer.cooling_call_ended.v1",
  "source": "consumer.v1",
  "ts": "2026-07-15T14:42:15.123456+00:00",
  "data": {
    "floor": "floor_1",
    "ended_at": "2026-07-15T14:42:15.000000+00:00",
    "entity_id": "binary_sensor.floor_1_cooling_call",
    "duration_s": 2535
  }
}
```

---

### `homeops.consumer.cooling_session_started.v1`

Emitted when the aggregate `binary_sensor.ac_cooling` helper transitions from `off` → `on`.
This is a whole-home inferred cooling-demand session, not proof that the compressor is running.

| Field | Type | Description |
|---|---|---|
| `data.started_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | Always `"binary_sensor.ac_cooling"` |

**Example:**

```json
{
  "schema": "homeops.consumer.cooling_session_started.v1",
  "source": "consumer.v1",
  "ts": "2026-07-15T14:00:01.123456+00:00",
  "data": {
    "started_at": "2026-07-15T14:00:01.000000+00:00",
    "entity_id": "binary_sensor.ac_cooling"
  }
}
```

---

### `homeops.consumer.cooling_session_ended.v1`

Emitted when the aggregate `binary_sensor.ac_cooling` helper transitions from `on` → `off`.

| Field | Type | Description |
|---|---|---|
| `data.ended_at` | string (ISO 8601 UTC) | Timestamp from the original observer event |
| `data.entity_id` | string | Always `"binary_sensor.ac_cooling"` |
| `data.duration_s` | integer \| null | Session duration, or `null` when the matching start was not observed in this run |
| `data.outdoor_temp_f` | number \| null | Most recent outdoor temperature at session end, when available |

**Example:**

```json
{
  "schema": "homeops.consumer.cooling_session_ended.v1",
  "source": "consumer.v1",
  "ts": "2026-07-15T14:42:16.123456+00:00",
  "data": {
    "ended_at": "2026-07-15T14:42:16.000000+00:00",
    "entity_id": "binary_sensor.ac_cooling",
    "duration_s": 2535,
    "outdoor_temp_f": 88.5
  }
}
```

---

### `homeops.consumer.floor_2_long_call_warning.v1`

Emitted once per floor-2 call when the elapsed call duration exceeds
`rules.floor_2_long_call.threshold_minutes × 60`. See [In-Flight Floor-2 Warning](#in-flight-floor-2-warning)
for full details.

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

### Thermostat cooling session and outcome events

Cooling thermostat events are the additive counterparts to the heating session
and performance events. A per-zone cooling session begins when
`hvac_action` transitions to `"cooling"` and ends when it leaves that value.
The session captures the setpoint at start; later setpoint changes are emitted
through the existing shared thermostat events and do not retarget the active
session. Cooling reaches its target when `current_temp <=` that captured
setpoint after the previous reading was above it.

These per-zone events are distinct from `cooling_session_started.v1` and
`cooling_session_ended.v1`, which describe the whole-home
`binary_sensor.ac_cooling` helper. The heating event names and comparators are
unchanged.

#### `homeops.consumer.thermostat_cooling_session_started.v1`

Emitted on a climate entity's transition to `hvac_action == "cooling"`.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.started_at` | string (ISO 8601 UTC) \| null | Original observer timestamp of the action transition |
| `data.mode` | string | Always `"cool"` |
| `data.hvac_mode` | string \| null | Top-level HA climate mode |
| `data.hvac_action` | string | Always `"cooling"` |
| `data.setpoint` | float \| null | Setpoint captured at cooling-session start |
| `data.current_temp` | float \| null | Temperature captured at cooling-session start |
| `data.other_zones_calling` | array[string] | Other cooling-call helper entities active at session start |

#### `homeops.consumer.thermostat_cooling_setpoint_reached.v1`

Emitted once when the active cooling session crosses its captured target
downward: the current reading is at or below the target and the previous
reading was above it. It uses the shared thermostat payload plus `mode: "cool"`
and the session-start target in `data.setpoint`.

#### `homeops.consumer.zone_time_to_cool.v1`

Emitted alongside the cooling setpoint-reached event when a valid session-start
temperature and timestamp are available. `setpoint_delta` and
`degrees_cooled` are positive when the zone had to cool, and
`other_zones_calling` reflects the cooling-call helpers active at the crossing.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.mode` | string | Always `"cool"` |
| `data.start_temp` | float | Temperature at cooling-session start |
| `data.setpoint` | float | Captured cooling target |
| `data.setpoint_delta` | float | `start_temp - setpoint` |
| `data.duration_s` | integer | Seconds from cooling-session start to target crossing |
| `data.end_temp` | float | Temperature at target crossing |
| `data.degrees_cooled` | float | `start_temp - end_temp` |
| `data.degrees_per_min` | float | `degrees_cooled / (duration_s / 60)` |
| `data.outdoor_temp_f` | float \| null | Last known outdoor temperature |
| `data.other_zones_calling` | array[string] | Other cooling-call helpers active at target crossing |

#### `homeops.consumer.zone_cooling_setpoint_miss.v1`

Emitted when cooling ends without a downward target crossing, provided the zone
started above its captured target and remained above it. `closest_temp` is the
lowest observed temperature during the session and `delta` is the positive
remaining gap (`closest_temp - setpoint`).

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.mode` | string | Always `"cool"` |
| `data.start_temp` | float | Temperature at cooling-session start |
| `data.setpoint` | float | Captured cooling target |
| `data.setpoint_delta` | float | `start_temp - setpoint` |
| `data.duration_s` | integer | Seconds from cooling-session start to action end |
| `data.closest_temp` | float | Lowest temperature observed during the session |
| `data.delta` | float | `closest_temp - setpoint`; positive for a miss |
| `data.outdoor_temp_f` | float \| null | Last known outdoor temperature |
| `data.other_zones_calling` | array[string] | Other cooling-call helpers active at session start |
| `data.likely_cause` | string | `"thermostat_adjustment"` when the setpoint changed during cooling; otherwise `"unknown"` |

#### `homeops.consumer.zone_cooling_undershoot.v1`

Emitted when a cooling session reached its target and then ended. It measures
the post-target tail: `undershoot_s` is the time from target crossing to action
end, and `trough_temp` is the lowest post-target reading. `trough_temp` is
`null` when only the crossing reading was available.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.mode` | string | Always `"cool"` |
| `data.start_temp` | float \| null | Temperature at cooling-session start |
| `data.setpoint` | float \| null | Captured cooling target |
| `data.setpoint_delta` | float \| null | `start_temp - setpoint` |
| `data.end_temp` | float \| null | Temperature at action end |
| `data.undershoot_s` | integer | Seconds from target crossing to action end |
| `data.trough_temp` | float \| null | Lowest post-target temperature observed |
| `data.outdoor_temp_f` | float \| null | Last known outdoor temperature |
| `data.other_zones_calling` | array[string] | Other cooling-call helpers active at session start |

#### `homeops.consumer.thermostat_cooling_session_ended.v1`

Emitted when a climate entity leaves `hvac_action == "cooling"`. The payload
contains the captured session target, the end reading, total duration, and a
`target_reached` boolean. It is emitted after the miss/undershoot outcome when
one applies.

| Field | Type | Description |
|---|---|---|
| `data.entity_id` | string | Climate entity ID |
| `data.zone` | string | Zone identifier |
| `data.ended_at` | string (ISO 8601 UTC) \| null | Original observer timestamp of the action transition |
| `data.mode` | string | Always `"cool"` |
| `data.hvac_mode` | string \| null | Top-level HA climate mode after the transition |
| `data.hvac_action` | string \| null | Action after the transition |
| `data.start_temp` | float \| null | Temperature at cooling-session start |
| `data.setpoint` | float \| null | Captured cooling target |
| `data.current_temp` | float \| null | Temperature at action end |
| `data.duration_s` | integer \| null | Seconds from session start to action end |
| `data.target_reached` | boolean | Whether the captured target was crossed |
| `data.other_zones_calling` | array[string] | Other cooling-call helpers active at session start |

**Example — target crossing and time-to-cool:**

```json
{
  "schema": "homeops.consumer.zone_time_to_cool.v1",
  "source": "consumer.v1",
  "ts": "2026-07-15T14:30:00.000000+00:00",
  "data": {
    "entity_id": "climate.floor_1_thermostat",
    "zone": "floor_1",
    "mode": "cool",
    "start_temp": 78.0,
    "setpoint": 72.0,
    "setpoint_delta": 6.0,
    "duration_s": 1800,
    "end_temp": 72.0,
    "degrees_cooled": 6.0,
    "degrees_per_min": 0.2,
    "outdoor_temp_f": 88.5,
    "other_zones_calling": ["binary_sensor.floor_2_cooling_call"]
  }
}
```

---

### `homeops.consumer.furnace_daily_summary.v1`

Emitted once per UTC calendar day at the first observer event with a new date (i.e. just after midnight UTC). Summarises the previous day's accumulated heating and inferred cooling runtime.

| Field | Type | Description |
|---|---|---|
| `data.date` | string (`YYYY-MM-DD`) | The day being summarised (the day that just ended) |
| `data.total_furnace_runtime_s` | integer | Total furnace on-time for the day in seconds |
| `data.session_count` | integer | Number of complete furnace runs recorded |
| `data.total_cooling_runtime_s` | integer | Total inferred whole-home AC-demand runtime for the day in seconds |
| `data.cooling_session_count` | integer | Number of complete inferred whole-home cooling sessions recorded |
| `data.per_floor_runtime_s` | object | `{"floor_1": int, "floor_2": int, "floor_3": int}` — total floor call duration per zone in seconds; zones with no calls have value `0` |
| `data.per_floor_cooling_runtime_s` | object | `{"floor_1": int, "floor_2": int, "floor_3": int}` — total cooling-call duration per zone in seconds; zones with no calls have value `0` |
| `data.per_floor_cooling_session_count` | object | Completed cooling-call count per zone; zones with no calls have value `0` |
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
    "total_cooling_runtime_s": 7200,
    "cooling_session_count": 3,
    "per_floor_runtime_s": {
      "floor_1": 12600,
      "floor_2": 9000,
      "floor_3": 5400
    },
    "per_floor_cooling_runtime_s": {
      "floor_1": 2400,
      "floor_2": 3600,
      "floor_3": 1200
    },
    "per_floor_cooling_session_count": {
      "floor_1": 1,
      "floor_2": 1,
      "floor_3": 1
    },
    "outdoor_temp_min_f": 22.1,
    "outdoor_temp_max_f": 38.6
  }
}
```

---

### `homeops.consumer.floor_daily_summary.v1`

Emitted three times per UTC calendar day rollover (once per floor: `floor_1`, `floor_2`, `floor_3`), immediately after `furnace_daily_summary.v1`. Summarises each floor's heating and cooling-call activity for the previous day.

| Field | Type | Description |
|---|---|---|
| `data.floor` | string | Floor name: `"floor_1"`, `"floor_2"`, or `"floor_3"` |
| `data.date` | string (`YYYY-MM-DD`) | The day being summarised |
| `data.total_calls` | integer | Number of completed heating calls for this floor |
| `data.total_runtime_s` | integer | Sum of all call durations in seconds |
| `data.avg_duration_s` | float \| null | Mean call duration in seconds; `null` if no calls |
| `data.max_duration_s` | integer \| null | Longest single call duration in seconds; `null` if no calls |
| `data.cooling_total_calls` | integer | Number of completed cooling calls for this floor |
| `data.cooling_total_runtime_s` | integer | Sum of completed cooling-call durations in seconds |
| `data.cooling_avg_duration_s` | float \| null | Mean cooling-call duration in seconds; `null` if no calls |
| `data.cooling_max_duration_s` | integer \| null | Longest single cooling-call duration in seconds; `null` if no calls |
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
    "cooling_total_calls": 2,
    "cooling_total_runtime_s": 3600,
    "cooling_avg_duration_s": 1800.0,
    "cooling_max_duration_s": 2100,
    "outdoor_temp_avg_f": 30.4
  }
}
```

---

## Data model reference

The normalized contracts for completed floor calls and per-floor aggregates
are documented in [`docs/data-model.md`](../../docs/data-model.md). The
document defines `FloorCallSession` and `FloorStats`, including UTC window
boundaries, restart-related unknown durations, pairing rules, and the
distinction between completed calls and in-flight state. The daily summary
projection in `reporting.py` follows those semantics.

---

## In-Flight Floor-2 Warning

Floor 2 has only 3 vents. When it calls for heat for an extended period the furnace blasts through a small number of open vents, which can trip the furnace high-limit switch (Code 4) and eventually trigger a lockout (Code 7, 3-hour auto-reset).

The consumer checks the elapsed duration of an active floor-2 call on **every loop iteration** — both when a new event arrives and on each `select.select` timeout. This means the check fires even during quiet periods with no new sensor events.

**Logic:**

1. When floor 2 starts a new call, `floor_2_warn_sent` is reset to `False`.
2. On each loop iteration, if floor 2 is currently active, the consumer computes `elapsed_s = now - floor_on_since["binary_sensor.floor_2_heating_call"]`.
3. If `elapsed_s >= rules.floor_2_long_call.threshold_minutes × 60` and
   `floor_2_warn_sent` is `False`:
   - Emits a `floor_2_long_call_warning.v1` derived event.
   - Sends a Telegram message (if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set).
   - Sets `floor_2_warn_sent = True` to prevent duplicate alerts for the same call.

The warning fires **at most once per floor-2 call session**, regardless of how long the call continues.

---

## Proactive Anomaly Insight

When `floor_runtime_anomaly.v1` fires, the consumer can build a bounded
48-hour HVAC context, ask the configured provider for a concise explanation,
and send the result to the configured Telegram chat. This path is deliberately
staged and **disabled by default**; it never writes thermostat state or calls a
Home Assistant service. The provider boundary is injected and provider-neutral,
so CI uses fakes and makes no live LLM or Telegram calls.

The trigger is strict: only a fully validated
`homeops.consumer.floor_runtime_anomaly.v1` event is eligible. Arbitrary event
fields are discarded before prompt construction, and the prompt labels both
the anomaly and HVAC context as untrusted evidence. Empty/sparse context,
malformed configuration, provider failures, and Telegram failures all fail
closed while leaving the deterministic event pipeline running.

Successful deliveries emit a
`homeops.consumer.proactive_anomaly_insight.v1` audit event containing the
stable anomaly/insight ID, provider/model, bounded character counts, delivery
status, and the bounded provider result. Failed and disabled attempts are
audited with an error/status; replay duplicates are suppressed. Successful
insight IDs and the UTC-day provider-call count are stored atomically in
`HOMEOPS_PROACTIVE_INSIGHT_STATE` (default
`state/consumer/proactive-insight-state.json`). The default budget is three
provider calls per UTC day.

Enable the staged path only after setting all required secrets/configuration:

```dotenv
HOMEOPS_PROACTIVE_INSIGHT_ENABLED=true
GEMINI_API_KEY=<provider-key>
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_CHAT_ID=<chat-id>
```

The complete variable list and bounds are in the Configuration Reference
below. The default limits are a 48-hour lookback, 8,000 context characters,
1,200 output characters, a 10-second timeout, and three provider calls per UTC
day.

---

## Bootstrap Behavior

When the consumer starts it calls `last_furnace_on_since()` and `last_ac_cooling_on_since()` to scan the observer log in reverse and determine whether the furnace or inferred AC is currently mid-session. This prevents a spurious `heating_session_started` or `cooling_session_started` event if either aggregate helper was already on when the consumer (re)started.

`last_furnace_on_since()` returns:

- The `datetime` of the most recent `off → on` furnace transition if the furnace appears to be on.
- `None` if the most recent furnace event was an `on → off` transition, or if the log is empty or unreadable.

`last_ac_cooling_on_since()` applies the same boundary rules to
`binary_sensor.ac_cooling`. Per-floor cooling-call start times are not
bootstrapped, matching the existing heating floor-call behavior; a cooling
call that ends after a restart therefore has `duration_s: null` unless its
start was observed in the current run.

Floor call start times are **not** bootstrapped — if the consumer restarts mid-call, `duration_s` for that call will be `null` in the `floor_call_ended` event.

When a recent state file is available, the consumer restores the cooling daily
runtime/count gauges and the active cooling-call/aggregate gauges before
playback. Playback then replays only the observer events after the saved
cursor; the final gauge values are reconciled from the persisted daily state,
so a restart cannot fabricate or double-count cooling sessions.

---

## Prometheus Metrics (`/metrics`)

The consumer exposes live HVAC telemetry in Prometheus exposition format at
`GET http://127.0.0.1:8001/metrics` on the Pi. Prometheus on EC2 reaches the
same listener at `http://100.115.21.72:8001/metrics` over Tailscale; it is not a
public Internet endpoint. This is the data pipeline source for the
[homeops.now](https://homeops.now) public dashboard.

| Metric | Type | Labels | Description |
|---|---|---|---|
| `furnace_heating_active` | Gauge | — | 1 if furnace is currently in a heating session, 0 if idle |
| `heating_session_duration_seconds` | Gauge | — | Duration of the most recently completed heating session |
| `ac_cooling_active` | Gauge | — | 1 if inferred whole-home AC demand is active, 0 if idle; not compressor feedback |
| `cooling_session_duration_seconds` | Gauge | — | Duration of the most recently completed inferred whole-home cooling session |
| `cooling_runtime_today_seconds` | Gauge | — | Accumulated inferred whole-home cooling runtime today (seconds) |
| `cooling_session_count_today` | Gauge | — | Number of completed inferred whole-home cooling sessions today |
| `floor_temperature_fahrenheit` | Gauge | `floor` | Latest thermostat current temperature per floor (°F) |
| `outdoor_temperature_fahrenheit` | Gauge | — | Latest outdoor temperature reading (°F) |
| `floor_call_active` | Gauge | `floor` | 1 if the floor is currently calling for heat |
| `zone_runtime_today_seconds` | Gauge | `floor` | Accumulated floor heating call runtime today (seconds) |
| `cooling_floor_call_active` | Gauge | `floor` | 1 if the floor is currently calling for cooling |
| `cooling_zone_runtime_today_seconds` | Gauge | `floor` | Accumulated floor cooling-call runtime today (seconds) |
| `cooling_zone_call_count_today` | Gauge | `floor` | Number of completed floor cooling calls today |
| `floor_runtime_anomaly_total` | Counter | `floor` | Cumulative count of `floor_runtime_anomaly.v1` events |

Configure the port via `METRICS_PORT` env var (default: `8001`).

## Historical Anomaly Validation

The repository-level [`scripts/validate_anomalies.py`](../../scripts/validate_anomalies.py)
replays the production anomaly rules against a complete derived-event JSONL
history without writing to the event log or consumer state. It evaluates
`floor_runtime_anomaly.v1`, `heating_short_session_warning.v1`, and
`heating_long_session_warning.v1`, compares replayed results with warnings
already emitted, and records data-quality and evidence limitations.

Run it from the repository root:

```bash
PYTHONPATH=services/insights \
  python3 scripts/validate_anomalies.py --log state/consumer/events.jsonl
```

If no `--baseline` file is supplied, furnace long-session evaluation uses the
rule's documented absolute fallback thresholds. A baseline can be supplied
explicitly with `--baseline state/consumer/baseline_constants.json`.

The repository-level [`scripts/analyze_multi_zone_impact.py`](../../scripts/analyze_multi_zone_impact.py)
performs a separate read-only analysis of `zone_time_to_temp.v1` events. It
compares sessions with and without other zones calling at session start, but
requires a minimum sample size before reporting a scheduling effect. It does
not change thermostat settings or HA automations.

The repository-level [`scripts/floor_hourly_heatmap.py`](../../scripts/floor_hourly_heatmap.py)
counts `floor_call_started.v1` events by local hour for each floor over an
explicit inclusive date range. It defaults to `America/New_York`, accepts an
IANA timezone with `--timezone`, and reports input quality plus calls outside
the selected range. This is a read-only frequency report: it does not change
consumer state, thermostat settings, or HA automations.

```bash
python3 scripts/floor_hourly_heatmap.py \
  --log state/consumer/events.jsonl \
  --start 2026-05-11 --end 2026-05-17
```

See [`docs/hourly-zone-call-heatmap-2026-08.md`](../../docs/hourly-zone-call-heatmap-2026-08.md)
for the latest Pi-history snapshot and its data limitations.

The repository-level [`scripts/furnace_temp_scatter.py`](../../scripts/furnace_temp_scatter.py)
exports daily whole-furnace scatter data as CSV. It averages raw
`outdoor_temp_updated.v1` readings by UTC date, prefers the canonical
`furnace_daily_summary.v1` runtime (including zero-runtime days), and uses
completed `heating_session_ended.v1` events as a fallback when no daily summary
exists. Rows with missing measurements stay blank so downstream plots can
exclude incomplete points explicitly.

```bash
python3 scripts/furnace_temp_scatter.py \
  --log state/consumer/events.jsonl \
  --start 2026-03-20 \
  --end 2026-08-21 \
  --out state/furnace_temp_scatter.csv
```

The repository-level [`scripts/generate_report.py`](../../scripts/generate_report.py)
composes the floor-summary and furnace-scatter readers into a single
self-contained HTML artifact. It renders inline SVG charts for per-floor daily
runtime and outdoor temperature versus whole-furnace runtime, keeps missing
measurements explicit, and includes the daily source table. It is a read-only
analysis command; it does not start the consumer or modify consumer state.

```bash
python3 scripts/generate_report.py \
  --start 2026-03-20 \
  --end 2026-08-21 \
  --log state/consumer/events.jsonl \
  --out reports/hvac_trend.html
```

The repository-level [`scripts/runtime_temp_anomalies.py`](../../scripts/runtime_temp_anomalies.py)
uses the same `floor_daily_summary.v1` history to identify unusually high
per-floor runtime after accounting for average outdoor temperature. It fits a
separate model per floor, requires a configurable minimum history, and uses a
robust residual score. Results are review candidates only; the script does not
claim an equipment fault or modify the consumer's state.

```bash
python3 scripts/runtime_temp_anomalies.py \
  --log state/consumer/events.jsonl \
  --start 2026-04-01 \
  --end 2026-08-21
```

The repository-level [`scripts/zone_heat_loss.py`](../../scripts/zone_heat_loss.py)
replays floor-call and thermostat history to estimate cooling-curve heat-loss
rates. It uses only samples from known furnace-off, thermostat-idle intervals,
splits long telemetry gaps, and marks zones with insufficient curves rather than
inventing a baseline. The output is a read-only measurement and not a fault
diagnosis.

```bash
python3 scripts/zone_heat_loss.py \
  --log state/consumer/events.jsonl \
  --start 2026-03-20 \
  --end 2026-05-31
```

The repository-level [`scripts/runtime_per_degree.py`](../../scripts/runtime_per_degree.py)
computes a demand-normalized efficiency ratio for completed zone calls:
furnace on-time seconds divided by the positive zone temperature rise in °F.
It uses overlapping completed furnace sessions rather than treating a zone's
call duration as furnace runtime, brackets each call with nearby thermostat
readings, assigns outdoor-temperature buckets, and exposes missing/incomplete
measurements in `data_quality`. This is a read-only report artifact; it does
not add an event schema or write thermostat settings.

```bash
python3 scripts/runtime_per_degree.py \
  --log state/consumer/events.jsonl \
  --start 2026-03-20 \
  --end 2026-05-31 \
  --format json
```

The repository-level [`scripts/time_to_temp.py`](../../scripts/time_to_temp.py)
builds a read-only per-zone time-to-temperature model from completed
`zone_time_to_temp.v1` events. It fits seconds per degree against outdoor
temperature, scales the result by a requested positive setpoint delta, and
keeps sparse history, missing measurements, and extrapolated queries explicit.
The companion `thermostat_setpoint_reached.v1` event is not used directly
because it records the crossing but not the completed duration required by the
model.

```bash
python3 scripts/time_to_temp.py \
  --zone floor_2 \
  --outdoor 30 \
  --delta 3 \
  --start 2026-03-20 \
  --end 2026-08-23 \
  --log state/consumer/events.jsonl \
  --format json
```

This analysis does not add a consumer event schema, change thermostat
settings, or modify consumer state. See
[`docs/time-to-temp-2026-08.md`](../../docs/time-to-temp-2026-08.md) for the
latest Pi-history validation and its telemetry limitations.

## Thermal prediction target contract

The canonical target definitions for future mode-aware ML work live in
[`docs/thermal-prediction-targets.md`](../../docs/thermal-prediction-targets.md).
That contract defines per-zone `time_to_setpoint_s` and `zone_runtime_s`
labels, the call-start prediction boundary, directional heat/cool crossing
rules, and explicit treatment for incomplete or censored sessions. It does
not change the consumer. The offline dataset exporter now materializes both
heating and explicitly instrumented cooling rows without relabeling heating
history or synthesizing cooling sessions.

## Thermal prediction feature schema

The point-in-time feature contract for future thermal-model training lives in
[`docs/thermal-prediction-features.md`](../../docs/thermal-prediction-features.md).
It defines the mode-aware start snapshot, directional setpoint gap, outdoor
temperature freshness, concurrent-zone snapshot, local time-of-day, prior
same-zone runtime, optional humidity/occupancy/weather fields, provenance, and
null behavior. Every feature is sourced from information available at
`prediction_ts` (`active_start_ts`); completed-session outcomes and future
readings are explicitly excluded. Heat and cool share one schema, while
cooling call snapshots use the separate cooling-call sensors and may remain
null when the source history is incomplete. This document does not change
consumer events or thermostat behavior.

## Thermal prediction evaluation protocol

The repository-level [`docs/thermal-prediction-evaluation.md`](../../docs/thermal-prediction-evaluation.md) defines the v1 baseline ladder and evaluation rules for future mode-aware thermal models. It selects a historical-median reference, a transparent degree-minute/thermal-response baseline, and a small regularized linear model for comparison. It requires chronological splits, point-in-time feature fitting, per-floor/per-mode reporting, uncertainty coverage, and explicit sparse-data behavior.

This is a design contract only. It does not add consumer events, train a model, make a recommendation, invoke an LLM, or control Home Assistant. The future cross-zone thermal model and what-if query layer remain downstream work validated against deliberate experiments.

## Thermal prediction dataset export

The repository-level `scripts/export_thermal_dataset.py` materializes
the target and feature contracts into deterministic
`homeops.thermal.training_row.v1` JSONL rows from raw observer and
consumer-derived logs. Each row keeps point-in-time features separate from
later labels, preserves incomplete/censored status, and records source
timestamps and line-level provenance. See
[`docs/thermal-prediction-dataset.md`](../../docs/thermal-prediction-dataset.md)
for the schema, source-event rules, and command examples. This is an offline
export only; it does not train a model or change Home Assistant.

The repository-level [`scripts/thermal_query.py`](../../scripts/thermal_query.py)
is the LLM-facing composition layer for these read-only reports. It accepts a
natural-language question plus an explicit primary zone and outdoor
temperature, then returns compact model metadata, furnace-session baseline
statistics, data-quality counters, and bounded allowlisted source-event
evidence. It does not invoke a provider or control Home Assistant. A positive
setpoint delta may be supplied directly; alternatively, the tool derives it
from a target and current temperature. A target without a current temperature
is retained as context but is explicitly marked unable to produce a duration
prediction.

```bash
python3 scripts/thermal_query.py \
  --question "How long should floor 2 take to reach 68°F?" \
  --zone floor_2 \
  --outdoor 30 \
  --target 68 \
  --current 65 \
  --log state/consumer/events.jsonl \
  --format json
```

The tool is a prompt-context boundary, not a replacement for the public
`POST /api/diagnostic` route. That route currently uses its own live
Prometheus snapshot on EC2; wiring historical Pi event access into that
provider path remains a separate deployment decision.

## Read-only multi-zone scheduling query

The repository-level [`scripts/scheduling_query.py`](../../scripts/scheduling_query.py)
composes the time-to-temperature model, conservative cooling-curve rates for
floors 1 and 3, and the validated `rules.floor_2_long_call` threshold. It can
estimate a floor-2 start time for a requested deadline and calculate bounded
secondary-zone setpoint ceilings that are intended to prevent a concurrent
secondary call during that window.

```bash
python3 scripts/scheduling_query.py \
  --target 68 \
  --current 65 \
  --outdoor 28 \
  --by 2026-01-02T07:00:00-05:00 \
  --floor-1-current 70 \
  --floor-3-current 69 \
  --log state/consumer/events.jsonl \
  --format text
```

The query is strictly read-only. It does not call Home Assistant, change
thermostat state, emit a consumer event, or activate the staged mitigation
automation. It returns no schedule when model history is sparse, a temperature
snapshot is stale, the primary prediction is extrapolated/invalid, or the
predicted call reaches the configured long-call threshold reserve. The
`SCHEDULING_SCHEMA` value is an offline report schema, not a consumer event
schema. See [`docs/multi-zone-scheduling-query.md`](../../docs/multi-zone-scheduling-query.md)
for the full contract.

## Staged mitigation replay

[`scripts/mitigation_e2e.py`](../../scripts/mitigation_e2e.py) replays three
fresh furnace sessions for one incident, verifies the staged zone-stagger
decision events, injects the explicit short-cycle rollback trigger, and sends
the resulting observer records through the real consumer persistence and
Telegram-alert path. The alert sink is patched in memory, so the command makes
no Home Assistant or Telegram network calls and writes no production state.

```bash
python3 scripts/mitigation_e2e.py
```

The replay is an offline HA-compatible contract test, not a substitute for an
isolated Home Assistant instance. It does not render Jinja or invoke real
`climate.set_hvac_mode` services. The checked-in mitigation overlay remains
disabled by default until the isolated-HA test and human safety review pass;
see [`docs/mitigation-test-results.md`](../../docs/mitigation-test-results.md).

---

## Configuration Reference

Rule thresholds and enablement live in the repository-level
[`services/insights/rules.yaml`](../insights/rules.yaml). The consumer loads and
validates that file once at startup; set `enabled: false` for a rule to stop its
findings/alerts while leaving event collection and state tracking intact. Use
`HOMEOPS_RULES_CONFIG` for a separately validated test or rollback file.

The YAML file is the source of truth for `overrun_ratio`,
`no_response_minutes`, `storm_count`, `storm_window_hours`, and the other
existing warning/insight thresholds. The `mitigation` section is also the
validated source for the timing values projected into the staged Home
Assistant overlay under [`homeassistant/`](../../homeassistant/); the
`input_boolean.mitigation_enabled` guard remains off until isolated
end-to-end testing is complete. Missing, unknown, non-finite, or
out-of-range values fail startup rather than silently changing safety behavior.

Non-rule service configuration remains environment-based:

| Variable | Default | Description |
|---|---|---|
| `EVENT_LOG` | `state/observer/events.jsonl` | Path to the observer's output JSONL file to tail |
| `DERIVED_EVENT_LOG` | `state/consumer/events.jsonl` | Path to write derived events (created if absent) |
| `HOMEOPS_RULES_CONFIG` | `services/insights/rules.yaml` | Rules YAML path; loaded and validated once at startup |
| `METRICS_PORT` | `8001` | Prometheus metrics HTTP port |
| `TELEGRAM_COMMAND_CHECK_INTERVAL_S` | `30` | Polling interval for Telegram commands |
| `TELEGRAM_BOT_TOKEN` | _(unset)_ | Telegram Bot API token for overheating, rollback, and proactive insight alerts |
| `TELEGRAM_CHAT_ID` | _(unset)_ | Telegram chat ID to receive overheating, rollback, and proactive insight alerts |
| `GEMINI_API_KEY` | _(unset)_ | Provider credential required when proactive insight is enabled; never written to event data |
| `HOMEOPS_PROACTIVE_INSIGHT_ENABLED` | `false` | Enable provider-backed explanations for validated runtime anomalies |
| `HOMEOPS_PROACTIVE_INSIGHT_PROVIDER` | `gemini` | Provider adapter name; unsupported values fail closed |
| `HOMEOPS_PROACTIVE_INSIGHT_MODEL` | `gemini-2.5-flash` | Model passed to the provider adapter |
| `HOMEOPS_PROACTIVE_INSIGHT_LOOKBACK_HOURS` | `48` | HVAC context lookback; valid range is 1–168 hours |
| `HOMEOPS_PROACTIVE_INSIGHT_MAX_CONTEXT_CHARS` | `8000` | Maximum context sent to the provider; valid range is 512–16,000 |
| `HOMEOPS_PROACTIVE_INSIGHT_MAX_OUTPUT_CHARS` | `1200` | Maximum provider result retained/delivered; valid range is 128–4,000 |
| `HOMEOPS_PROACTIVE_INSIGHT_TIMEOUT_S` | `10` | Provider/Telegram timeout; valid range is 1–10 seconds |
| `HOMEOPS_PROACTIVE_INSIGHT_DAILY_BUDGET` | `3` | Maximum provider calls per UTC day; valid range is 1–20 |
| `HOMEOPS_PROACTIVE_INSIGHT_STATE` | `state/consumer/proactive-insight-state.json` | Atomic successful-ID and daily-budget state file |

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
TELEGRAM_BOT_TOKEN=<bot-token> \
TELEGRAM_CHAT_ID=<chat-id> \
python3 services/consumer/consumer.py
```

**As a systemd service:** The production unit is `homeops-consumer.service` on
the Pi. See the repository-level [`docs/deployment.md`](../../docs/deployment.md)
for the deployment/restart contract and [`docs/architecture.md`](../../docs/architecture.md)
for the network boundary. The consumer bootstraps its sibling insights package
when executed directly by systemd, so this unit does not require a
`PYTHONPATH` override.
