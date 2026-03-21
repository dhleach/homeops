# Consumer Event Schemas

This document is the reference for all **existing, already-implemented** consumer events emitted by
`services/consumer/consumer.py` into `state/consumer/events.jsonl`. These events are derived from
raw `homeops.observer.state_changed.v1` records and represent higher-level state transitions
(floor calls, furnace sessions, thermostat changes, outdoor temperature readings, and daily
summaries).

For the three newer **heating-cycle outcome** events (`zone_time_to_temp.v1`,
`zone_overshoot.v1`, `zone_setpoint_miss.v1`) see [heating-cycle.md](./heating-cycle.md).

---

## Event: `homeops.consumer.floor_call_started.v1`

Fires when a zone floor-heating-call sensor transitions from `off` → `on` (zone begins demanding
heat).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier; required on all consumer events. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Wall-clock time of emission; used for log ordering. |
| `floor` | string | `_FLOOR_ENTITIES[entity_id]` (e.g. `"floor_1"`) | Human-readable zone key; primary grouping key for dashboards. |
| `started_at` | ISO 8601 string | `ts` field from the observer event | Timestamp of the HA state change that triggered this event. |
| `entity_id` | string | HA entity (e.g. `"binary_sensor.floor_1_heating_call"`) | Ties event back to the raw observer log. |

### JSON Example

```json
{
  "schema": "homeops.consumer.floor_call_started.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T06:12:03.441200+00:00",
  "data": {
    "floor": "floor_1",
    "started_at": "2026-01-15T06:12:03.100000+00:00",
    "entity_id": "binary_sensor.floor_1_heating_call"
  }
}
```

---

## Event: `homeops.consumer.floor_call_ended.v1`

Fires when a zone floor-heating-call sensor transitions from `on` → `off` (zone stops demanding
heat).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `floor` | string | `_FLOOR_ENTITIES[entity_id]` | Zone key. |
| `ended_at` | ISO 8601 string | `ts` field from the observer event | Timestamp of the HA state change. |
| `entity_id` | string | HA entity | Raw log linkage. |
| `duration_s` | int \| null | `(ts_ended - ts_started).total_seconds()` | Call duration in seconds. `null` if the consumer did not observe the corresponding `floor_call_started` event (e.g. restart mid-call). |

### JSON Example

```json
{
  "schema": "homeops.consumer.floor_call_ended.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T07:04:51.882300+00:00",
  "data": {
    "floor": "floor_1",
    "ended_at": "2026-01-15T07:04:51.500000+00:00",
    "entity_id": "binary_sensor.floor_1_heating_call",
    "duration_s": 3168
  }
}
```

---

## Event: `homeops.consumer.heating_session_started.v1`

Fires when `binary_sensor.furnace_heating` transitions from `off` → `on` (furnace begins a
heating run).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `started_at` | ISO 8601 string | `ts` field from the observer event | Timestamp of the HA state change. |
| `entity_id` | string | `"binary_sensor.furnace_heating"` | Ties event back to the raw observer log. |

### JSON Example

```json
{
  "schema": "homeops.consumer.heating_session_started.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T06:12:05.003100+00:00",
  "data": {
    "started_at": "2026-01-15T06:12:04.750000+00:00",
    "entity_id": "binary_sensor.furnace_heating"
  }
}
```

---

## Event: `homeops.consumer.heating_session_ended.v1`

Fires when `binary_sensor.furnace_heating` transitions from `on` → `off` (furnace completes a
heating run).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `ended_at` | ISO 8601 string | `ts` field from the observer event | Timestamp of the HA state change. |
| `entity_id` | string | `"binary_sensor.furnace_heating"` | Raw log linkage. |
| `duration_s` | int \| null | `(ts_ended - ts_started).total_seconds()` | Session run time in seconds. `null` if the consumer did not observe the matching `heating_session_started` event (e.g. restart mid-session; also recovered from observer log on startup). |

### JSON Example

```json
{
  "schema": "homeops.consumer.heating_session_ended.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T07:04:53.114500+00:00",
  "data": {
    "ended_at": "2026-01-15T07:04:52.800000+00:00",
    "entity_id": "binary_sensor.furnace_heating",
    "duration_s": 3168
  }
}
```

---

## Event: `homeops.consumer.thermostat_setpoint_changed.v1`

Fires when a climate entity's `temperature` attribute (the setpoint) changes from its last known
value.

All three thermostat climate events (`thermostat_setpoint_changed.v1`,
`thermostat_current_temp_updated.v1`, `thermostat_mode_changed.v1`) share the same `data`
payload, sourced from a common `common` dict built at processing time.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `entity_id` | string | `CLIMATE_ENTITIES` key (e.g. `"climate.floor_2_thermostat"`) | HA entity linkage; joins back to observer log. |
| `zone` | string | `CLIMATE_ENTITIES[entity_id]` (e.g. `"floor_2"`) | Zone grouping key. |
| `ts` (data field) | ISO 8601 string | `ts` field from the observer event | Timestamp of the HA state change (distinct from top-level emission `ts`). |
| `hvac_mode` | string \| null | `new_state` of the observer event (e.g. `"heat"`, `"off"`) | Top-level HA climate mode at time of change. |
| `hvac_action` | string \| null | `attributes["hvac_action"]` (e.g. `"heating"`, `"idle"`) | Actual current action of the climate entity. |
| `setpoint` | float \| null | `attributes["temperature"]` | Target temperature. The changed value triggering this event. |
| `current_temp` | float \| null | `attributes["current_temperature"]` | Actual measured temperature at time of change. |

### JSON Example

```json
{
  "schema": "homeops.consumer.thermostat_setpoint_changed.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T06:30:00.221400+00:00",
  "data": {
    "entity_id": "climate.floor_2_thermostat",
    "zone": "floor_2",
    "ts": "2026-01-15T06:30:00.000000+00:00",
    "hvac_mode": "heat",
    "hvac_action": "heating",
    "setpoint": 69.0,
    "current_temp": 65.5
  }
}
```

---

## Event: `homeops.consumer.thermostat_current_temp_updated.v1`

Fires when a climate entity's `current_temperature` attribute changes from its last known value.
Uses the same `data` payload as `thermostat_setpoint_changed.v1`.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `entity_id` | string | `CLIMATE_ENTITIES` key | HA entity linkage. |
| `zone` | string | `CLIMATE_ENTITIES[entity_id]` | Zone grouping key. |
| `ts` (data field) | ISO 8601 string | Observer event `ts` | Timestamp of the HA state change. |
| `hvac_mode` | string \| null | `new_state` | Top-level HA climate mode. |
| `hvac_action` | string \| null | `attributes["hvac_action"]` | Actual current action. |
| `setpoint` | float \| null | `attributes["temperature"]` | Current setpoint at time of update. |
| `current_temp` | float \| null | `attributes["current_temperature"]` | The changed temperature value triggering this event. |

### JSON Example

```json
{
  "schema": "homeops.consumer.thermostat_current_temp_updated.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T06:45:22.774900+00:00",
  "data": {
    "entity_id": "climate.floor_1_thermostat",
    "zone": "floor_1",
    "ts": "2026-01-15T06:45:22.500000+00:00",
    "hvac_mode": "heat",
    "hvac_action": "heating",
    "setpoint": 68.0,
    "current_temp": 66.0
  }
}
```

---

## Event: `homeops.consumer.thermostat_mode_changed.v1`

Fires when a climate entity's `hvac_mode` (top-level HA state) or `hvac_action` attribute
changes from its last known values. Uses the same `data` payload as the other thermostat events.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `entity_id` | string | `CLIMATE_ENTITIES` key | HA entity linkage. |
| `zone` | string | `CLIMATE_ENTITIES[entity_id]` | Zone grouping key. |
| `ts` (data field) | ISO 8601 string | Observer event `ts` | Timestamp of the HA state change. |
| `hvac_mode` | string \| null | `new_state` | The (possibly changed) top-level HA climate mode. |
| `hvac_action` | string \| null | `attributes["hvac_action"]` | The (possibly changed) actual current action. |
| `setpoint` | float \| null | `attributes["temperature"]` | Current setpoint at time of mode change. |
| `current_temp` | float \| null | `attributes["current_temperature"]` | Current measured temperature at time of mode change. |

### JSON Example

```json
{
  "schema": "homeops.consumer.thermostat_mode_changed.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T08:10:04.339200+00:00",
  "data": {
    "entity_id": "climate.floor_3_thermostat",
    "zone": "floor_3",
    "ts": "2026-01-15T08:10:04.100000+00:00",
    "hvac_mode": "off",
    "hvac_action": "idle",
    "setpoint": 65.0,
    "current_temp": 68.5
  }
}
```

---

## Event: `homeops.consumer.outdoor_temp_updated.v1`

Fires each time `sensor.outdoor_temperature` reports a new numeric state. Skipped for
`unavailable`, `unknown`, empty, or non-numeric values.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `entity_id` | string | `"sensor.outdoor_temperature"` | HA entity linkage. |
| `temperature_f` | float | `float(new_state)` | Outdoor temperature in °F as reported by the sensor. |
| `timestamp` | ISO 8601 string | `ts` field from the observer event | Timestamp of the HA state change. |

### JSON Example

```json
{
  "schema": "homeops.consumer.outdoor_temp_updated.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T07:00:01.554800+00:00",
  "data": {
    "entity_id": "sensor.outdoor_temperature",
    "temperature_f": 28.4,
    "timestamp": "2026-01-15T07:00:01.300000+00:00"
  }
}
```

---

## Event: `homeops.consumer.floor_2_long_call_warning.v1`

Fires (at most once per floor-2 call) when `binary_sensor.floor_2_heating_call` has been `on`
for longer than `FLOOR_2_WARN_THRESHOLD_S` seconds (default: 2700 s / 45 min). Also triggers a
Telegram alert if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured. The warning is
re-armed each time floor 2 starts a new call.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp; the moment the threshold was crossed (detected at the next event or timeout). |
| `floor` | string | hardcoded `"floor_2"` | Always floor 2; this warning is floor-2-specific due to overheating risk (Code 4/7 limit). |
| `elapsed_s` | int | `(now - floor_on_since["binary_sensor.floor_2_heating_call"]).total_seconds()` | How long floor 2 has been calling at time of emission. |
| `threshold_s` | int | `FLOOR_2_WARN_THRESHOLD_S` env var (default 2700) | The configured threshold that was exceeded. |
| `entity_id` | string | `"binary_sensor.floor_2_heating_call"` | HA entity linkage. |

### JSON Example

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

---

## Event: `homeops.consumer.furnace_daily_summary.v1`

Fires once per calendar day (UTC) when the first observer event with a new date is processed
(i.e. at the first event after midnight UTC). Summarises the previous day's furnace activity
accumulated in `daily_state`.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp (first event of the new day). |
| `date` | string (`YYYY-MM-DD`) | `current_date` at rollover | The date being summarised (the day that just ended). |
| `total_furnace_runtime_s` | int | Sum of `duration_s` from all `heating_session_ended.v1` events on this date | Total furnace on-time for the day in seconds. |
| `session_count` | int | Count of `heating_session_ended.v1` events on this date | Number of complete furnace runs recorded. |
| `per_floor_runtime_s` | object | `{floor_name: int}` for `floor_1`, `floor_2`, `floor_3` | Total call duration per zone in seconds, keyed by floor name. Zones with no calls have a value of `0`. |
| `outdoor_temp_min_f` | float \| null | `min(outdoor_temps)` across all `outdoor_temp_updated.v1` events on this date | Coldest outdoor reading of the day. `null` if no outdoor temperature readings were received. |
| `outdoor_temp_max_f` | float \| null | `max(outdoor_temps)` | Warmest outdoor reading of the day. `null` if no readings received. |

### JSON Example

```json
{
  "schema": "homeops.consumer.furnace_daily_summary.v1",
  "source": "consumer.v1",
  "ts": "2026-01-16T00:00:04.112700+00:00",
  "data": {
    "date": "2026-01-15",
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
