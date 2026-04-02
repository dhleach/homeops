# Consumer Event Schemas

This document is the reference for all consumer events emitted by
`services/consumer/consumer.py` into `state/consumer/events.jsonl`. It covers **12 implemented
events** and **3 planned events** (not yet implemented). All events are derived from raw
`homeops.observer.state_changed.v1` records and represent higher-level state transitions
(floor calls, furnace sessions, thermostat changes, outdoor temperature readings, daily
summaries, and per-zone heating-cycle outcomes).

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
| `outdoor_temp_avg_f` | float \| null | `mean(outdoor_temps)`, rounded to 1 decimal place | Average outdoor temperature for the day. `null` if no readings received. |
| `per_floor_session_count` | object | `{floor_name: int}` for `floor_1`, `floor_2`, `floor_3` | Count of completed floor heating sessions per zone for the day. Zones with no sessions have a value of `0`. |
| `warnings_triggered` | object | `{warning_type: int}` | Counts of each warning type fired during the day. Keys: `floor_2_long_call`, `floor_no_response`, `zone_slow_to_heat`, `observer_silence`, `setpoint_miss`. All zero on quiet days. |

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
    "outdoor_temp_max_f": 38.6,
    "outdoor_temp_avg_f": 30.4,
    "per_floor_session_count": {
      "floor_1": 4,
      "floor_2": 2,
      "floor_3": 1
    },
    "warnings_triggered": {
      "floor_2_long_call": 0,
      "floor_no_response": 0,
      "zone_slow_to_heat": 1,
      "observer_silence": 0,
      "setpoint_miss": 0
    }
  }
}
```

---

## Event: `homeops.consumer.floor_daily_summary.v1`

Fires three times per calendar day rollover (once per floor: `floor_1`, `floor_2`, `floor_3`),
emitted immediately after `furnace_daily_summary.v1` when the first observer event with a new
UTC date is processed. Summarises each floor's heating call activity for the day.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Wall-clock time of emission. |
| `floor` | string | `_FLOOR_ENTITIES[entity_id]` (e.g. `"floor_2"`) | Floor being summarised. |
| `date` | string (`YYYY-MM-DD`) | `current_date` at rollover | The date being summarised (the day that just ended). |
| `total_calls` | int | Count of `floor_call_ended.v1` events for this floor on this date | Number of completed heating calls. Zero on idle days. |
| `total_runtime_s` | int | Sum of `duration_s` from `floor_call_ended.v1` events for this floor | Total zone on-time in seconds. |
| `avg_duration_s` | float \| null | `total_runtime_s / total_calls`, rounded to 1 dp | Mean call duration. `null` if no completed calls. |
| `max_duration_s` | int \| null | Max `duration_s` across all calls for this floor on this date | Longest single call. `null` if no completed calls; useful for detecting sustained floor-2 calls. |
| `outdoor_temp_avg_f` | float \| null | `mean(outdoor_temps)` from `daily_state`, rounded to 1 dp | Average outdoor temperature for the day, shared across all three floor events. `null` if no outdoor readings. |

### JSON Example

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

## Planned Events (Not Yet Implemented)

The three events below are designed but not yet implemented. They surface per-zone heating-cycle
outcome quality — whether a zone reached its setpoint, how fast, and how much it overshot.
They complement the existing furnace-level `heating_session_started/ended.v1` and zone-level
`floor_call_started/ended.v1` events.

The furnace is shared across all zones (floor_1, floor_2, floor_3). Zones call for heat via
dampers; the furnace runs whenever any zone is calling. A **zone heating session** begins when
`hvac_action` transitions to `"heating"` and ends when `hvac_action` leaves `"heating"`.
Setpoint-reached is the first event where `current_temp >= setpoint` while
`hvac_action == "heating"`.

### Consumer State Requirements

These events require the consumer to track the following per-zone session state (in addition
to existing `climate_state`):

| State key | Type | Purpose |
|---|---|---|
| `session_start_temp[entity_id]` | float \| null | `current_temp` when `hvac_action` last became `"heating"`. |
| `session_start_ts[entity_id]` | datetime \| null | Timestamp when `hvac_action` last became `"heating"`. |
| `setpoint_reached_ts[entity_id]` | datetime \| null | Timestamp of first `current_temp >= setpoint` event during active session. |
| `session_peak_temp[entity_id]` | float \| null | Running max `current_temp` since `hvac_action = "heating"`. Used for `peak_temp` and `closest_temp`. |
| `session_other_zones[entity_id]` | list[string] | Snapshot of other calling zones at session start. |

All keys are reset to `null`/`[]` when `hvac_action` leaves `"heating"`.

### Notes on Future Work

- **Physical furnace sensor** — `binary_sensor.furnace_active` is a HA helper (true when ≥1
  zone is calling), not a real burner-on signal. A temperature sensor on the furnace heat
  exchanger or supply plenum would give us actual firing state and enable "limit timeout
  imminent" detection. This is a planned hardware addition.
- **`furnace_lockout_count`** — once a physical furnace sensor exists, tracking Code 4/7 limit
  trips per session would let analysis distinguish "miss because of lockout" from "miss because
  of cold load." Targeted for v2 post-sensor.
- **Multi-zone session correlation** — a future `furnace_session_outcome.v1` event could
  aggregate all three zones' outcomes for a single furnace run, enabling whole-home efficiency
  scoring per blast.

---

## Event: `homeops.consumer.zone_time_to_temp.v1`

Fires when a zone **reaches its setpoint** during an active heating session — the moment
`current_temp` first crosses `setpoint` while `hvac_action` is `"heating"`.

This is the primary heating-performance metric: how long did it take to satisfy the call,
under what outdoor conditions, and with how many competing zones?

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier; required on all consumer events. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Wall-clock time of emission; used for log ordering and time-series storage. |
| `entity_id` | string | `CLIMATE_ENTITIES` key (e.g. `climate.floor_1_thermostat`) | Ties event back to the HA entity; needed for joins with raw observer log. |
| `zone` | string | `CLIMATE_ENTITIES[entity_id]` (e.g. `floor_1`) | Human-readable zone name; primary grouping key for dashboards. |
| `start_temp` | float | `current_temp` at session start (when `hvac_action` first became `"heating"`) | Baseline temperature; together with `setpoint` characterises difficulty of the cycle. |
| `setpoint` | float | `attributes["temperature"]` at session end | Target temperature. |
| `setpoint_delta` | float | `setpoint - start_temp` | Pre-computed: how many degrees the zone needed to gain. Avoids recalculation downstream. |
| `duration_s` | int | `(ts_setpoint_reached - ts_session_start).total_seconds()` | Core KPI. Time from `hvac_action="heating"` to `current_temp >= setpoint`. |
| `end_temp` | float | `current_temp` at setpoint-reached event | Actual temperature at the moment the setpoint was crossed; may be fractionally above `setpoint` due to sensor resolution. |
| `degrees_gained` | float | `end_temp - start_temp` | Total rise observed; useful when comparing cycles where setpoint also changed mid-session. |
| `degrees_per_min` | float | `degrees_gained / (duration_s / 60)` | Normalised rise rate; primary metric for comparing efficiency across outdoor conditions and zone contention. |
| `outdoor_temp_f` | float \| null | `daily_state["last_outdoor_temp_f"]` | Last known outdoor reading at emission time. `null` if no reading received yet that day. Key covariate: cold days drive lower values. |
| `other_zones_calling` | list[string] | `floor_on_since` keys where value is not `None`, excluding this zone's entity | Zones simultaneously calling at session start. Shared furnace means fewer concurrent zones → more airflow per zone → faster rise. |

**Excluded fields:**

- `time_of_day` — derivable from `ts`; not stored to avoid redundancy.
- `day_of_week` — derivable from `ts`.
- `furnace_active` — redundant with `other_zones_calling`; it is simply `OR(zone_1, zone_2, zone_3)` and adds no information not already present in the list.
- `furnace_lockout_count` — tracking the number of lockout events per session requires additional state plumbing; deferred to v2.

### JSON Example

```json
{
  "schema": "homeops.consumer.zone_time_to_temp.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T07:43:12.004821+00:00",
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

## Event: `homeops.consumer.zone_overshoot.v1`

Fires when a heating session ends (`hvac_action` leaves `"heating"`) and setpoint was
**already reached before the session ended**. This captures the lag between the thermostat
satisfying its call and the furnace/damper actually shutting off — a normal but measurable
property of forced-air systems.

Overshoot is expected and generally benign, but large values can indicate a poorly tuned
anticipator, a slow-responding damper, or a zone that continued receiving heat after its
damper closed because other zones were still calling.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `entity_id` | string | `CLIMATE_ENTITIES` key | HA entity linkage. |
| `zone` | string | `CLIMATE_ENTITIES[entity_id]` | Zone grouping key. |
| `start_temp` | float | `current_temp` at session start | Baseline for full-cycle context. |
| `setpoint` | float | `attributes["temperature"]` | Target; needed to interpret `end_temp` magnitude. |
| `setpoint_delta` | float | `setpoint - start_temp` | How hard the zone had to work; covariate when comparing overshoot across cycles. |
| `end_temp` | float | `current_temp` at session-end event | Final temperature when `hvac_action` left `"heating"`. The difference `end_temp - setpoint` is the raw overshoot magnitude. |
| `overshoot_s` | int | `(ts_session_end - ts_setpoint_reached).total_seconds()` | Time the zone continued heating after reaching setpoint. Primary metric for this event. |
| `peak_temp` | float \| null | Highest `current_temp` observed between setpoint-reached and session-end | Best available approximation of peak overshoot. `null` if only one `current_temperature` reading was received in that window (sensor resolution too coarse to determine true peak vs. end_temp). |
| `outdoor_temp_f` | float \| null | `daily_state["last_outdoor_temp_f"]` | Environmental covariate. |
| `other_zones_calling` | list[string] | `floor_on_since` at session start | Other active zones; relevant because a zone sharing the furnace with others may keep receiving warm air after its own damper closes. |

**Excluded fields:**

- `time_of_day`, `day_of_week` — derivable from `ts`.
- `furnace_active` — redundant with `other_zones_calling`; simply `OR(zone_1, zone_2, zone_3)`.
- `furnace_lockout_count` — deferred to v2.
- `duration_s` (total session length) — the full cycle duration is already captured in `zone_time_to_temp.v1` for the same session; avoid duplicating it here. Analysts can join on `entity_id` + session start time if needed.

### JSON Example

```json
{
  "schema": "homeops.consumer.zone_overshoot.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T08:03:54.118400+00:00",
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

## Event: `homeops.consumer.zone_setpoint_miss.v1`

Fires when a heating session ends (`hvac_action` leaves `"heating"`) and setpoint was
**never reached** during the session. This is the anomaly / failure signal: the zone called
for heat but the system could not satisfy the demand before the call ended.

Causes include: a zone call cut short by manual thermostat adjustment, a furnace lockout
(Code 4/7 limit trip) aborting delivery mid-cycle, or an unusually long recovery needed in
extreme cold that triggered an early shutoff.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `entity_id` | string | `CLIMATE_ENTITIES` key | HA entity linkage. |
| `zone` | string | `CLIMATE_ENTITIES[entity_id]` | Zone grouping key. |
| `start_temp` | float | `current_temp` at session start | Baseline; together with `setpoint` shows how much work was needed. |
| `setpoint` | float | `attributes["temperature"]` | Target the zone never reached. |
| `setpoint_delta` | float | `setpoint - start_temp` | How ambitious the call was; large deltas in mild weather are a stronger anomaly signal than large deltas in extreme cold. |
| `duration_s` | int | `(ts_session_end - ts_session_start).total_seconds()` | How long the zone tried before the session ended. A very short `duration_s` suggests an aborted call (setpoint lowered, or furnace locked out immediately). |
| `closest_temp` | float | Highest `current_temp` observed during the session | How close the zone got. The gap `setpoint - closest_temp` is the shortfall. |
| `delta` | float | `setpoint - closest_temp` | Pre-computed shortfall in degrees. Positive means the zone fell short; zero would indicate setpoint was just barely touched (this event should not fire in that case). |
| `outdoor_temp_f` | float \| null | `daily_state["last_outdoor_temp_f"]` | Critical covariate for misses: extreme cold is an expected driver; mild-weather misses are the real alert signal. |
| `other_zones_calling` | list[string] | `floor_on_since` at session start | Competing zones reduce per-zone airflow and can contribute to a miss, especially on floor_3 (top floor, longest duct run). |
| `likely_cause` | `"thermostat_adjustment" \| "unknown"` | `"thermostat_adjustment"` if setpoint changed during the heating session, else `"unknown"` | Provides a first-pass triage signal: a thermostat adjustment during heating is the most common non-pathological cause of a miss; `"unknown"` warrants further investigation (e.g. furnace lockout, extreme cold load). |

**Excluded fields:**

- `time_of_day`, `day_of_week` — derivable from `ts`.
- `furnace_active` — redundant with `other_zones_calling`; simply `OR(zone_1, zone_2, zone_3)`.
- `furnace_lockout_count` — deferred to v2.
- `end_temp` — equivalent to `closest_temp` in a miss scenario if no cooling occurred during the session; using `closest_temp` is clearer about intent.

### JSON Example

```json
{
  "schema": "homeops.consumer.zone_setpoint_miss.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T05:22:44.903100+00:00",
  "data": {
    "entity_id": "climate.floor_3_thermostat",
    "zone": "floor_3",
    "start_temp": 62.0,
    "setpoint": 68.0,
    "setpoint_delta": 6.0,
    "duration_s": 2880,
    "closest_temp": 66.5,
    "delta": 1.5,
    "outdoor_temp_f": 14.2,
    "other_zones_calling": ["binary_sensor.floor_1_heating_call", "binary_sensor.floor_2_heating_call"],
    "likely_cause": "unknown"
  }
}
```

---

## Event: `homeops.consumer.observer_silence_warning.v1`

Fires when the consumer has received no `homeops.observer.state_changed.v1` events for longer
than `OBSERVER_SILENCE_THRESHOLD_S` seconds (default: 600 s / 10 min). This indicates the
observer service may have disconnected from Home Assistant, the WebSocket may have hung, or
the Pi may have lost network connectivity.

**Deduplication:** only one warning fires per silence episode. The flag resets when a new
observer event arrives, allowing the watchdog to re-arm for subsequent episodes.

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `data.last_event_ts` | ISO 8601 string | `last_observer_event_ts.isoformat()` | Timestamp of the last event received from the observer, for triage. |
| `data.silence_s` | int | `(now - last_event_ts).total_seconds()` | Observed silence duration in seconds at the time of emission. |
| `data.threshold_s` | int | `OBSERVER_SILENCE_THRESHOLD_S` env var (default 600) | Configured threshold that was exceeded. |

### Telegram Alert

When `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, the consumer sends:

```
⚠️ Observer silence detected!
No events received for <N> min.
Last event: <last_event_ts>
Check observer service on Pi.
```

### Configuration

| Env var | Default | Description |
|---|---|---|
| `OBSERVER_SILENCE_THRESHOLD_S` | `600` | Seconds of silence before alert fires. |

### JSON Example

```json
{
  "schema": "homeops.consumer.observer_silence_warning.v1",
  "source": "consumer.v1",
  "ts": "2026-03-26T19:10:00.000000+00:00",
  "data": {
    "last_event_ts": "2026-03-26T18:58:00.000000+00:00",
    "silence_s": 720,
    "threshold_s": 600
  }
}
```

---


## Event: `homeops.consumer.zone_slow_to_heat_warning.v1`

Fires during an active heating session when a zone has been calling for heat longer than a
per-floor expected window without reaching its setpoint. This is distinct from
`floor_no_response_warning.v1` (which fires when temperature isn't rising at all) — this event
fires even if temperature IS rising, but too slowly to reach setpoint within the expected window.

Thresholds are configurable via environment variables and default to:
- `floor_1`: 900 s (15 min) — `SLOW_TO_HEAT_THRESHOLD_FLOOR1_S`
- `floor_2`: 1800 s (30 min) — `SLOW_TO_HEAT_THRESHOLD_FLOOR2_S`
- `floor_3`: 600 s (10 min) — `SLOW_TO_HEAT_THRESHOLD_FLOOR3_S`

The warning fires **at most once per heating session** (suppressed by `slow_to_heat_sent` flag,
reset when a new session starts).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `zone` | string | `CLIMATE_ENTITIES[entity_id]` | Zone grouping key (e.g. `"floor_2"`). |
| `entity_id` | string | HA climate entity | Ties event back to the thermostat. |
| `elapsed_s` | int | `(now - heating_start_ts).total_seconds()` | How long the zone has been calling. |
| `threshold_s` | int | `SLOW_TO_HEAT_THRESHOLDS_S[zone]` | The per-floor threshold that was exceeded. |
| `start_temp` | float \| null | `heating_start_temp` from session state | Temperature when heating began. |
| `current_temp` | float \| null | `current_temperature` attribute | Temperature at warning time. |
| `setpoint` | float \| null | `temperature` attribute | Target setpoint not yet reached. |
| `setpoint_delta` | float \| null | `setpoint - start_temp` | How much total warming was needed. |
| `degrees_gained` | float \| null | `current_temp - start_temp` | How much warming has occurred so far. |
| `outdoor_temp_f` | float \| null | `daily_state["last_outdoor_temp_f"]` | Outdoor context; cold weather is an expected driver of slow heat. |

### JSON Example

```json
{
  "schema": "homeops.consumer.zone_slow_to_heat_warning.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T07:45:12.003100+00:00",
  "data": {
    "zone": "floor_2",
    "entity_id": "climate.floor_2_thermostat",
    "elapsed_s": 1850,
    "threshold_s": 1800,
    "start_temp": 64.0,
    "current_temp": 65.0,
    "setpoint": 68.0,
    "setpoint_delta": 4.0,
    "degrees_gained": 1.0,
    "outdoor_temp_f": 28.5
  }
}
```

---

## Event: `homeops.consumer.floor_runtime_anomaly.v1`

Fires at end-of-day when a floor's daily heating runtime significantly exceeds its rolling
historical baseline. Evaluated after `furnace_daily_summary.v1` is written, once per floor
per day.

**Telegram alert:** A Telegram message is sent when this event fires (once per floor per
day at rollover). Message includes floor label, date, runtime vs. baseline, and severity.
Fires in both the live main loop and the playback phase.

**Guards:**
- Requires at least **3 prior data points** in the lookback window — skips if history is
  insufficient (new install, data gap, etc.).
- Does not fire if the floor's baseline mean is **< 300 s** — avoids noise from floors
  that rarely run.

**Lookback:** defaults to the last 14 days of `furnace_daily_summary.v1` events, excluding
today (prevents circular reference with the current-day summary).

**Threshold:** `runtime_s > baseline_mean_s × threshold_multiplier` (default 1.5×).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `source` | string | hardcoded `"consumer.v1"` | Emitting service. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `data.floor` | string | caller | Floor identifier, e.g. `"floor_2"`. |
| `data.runtime_s` | int | today's daily summary | Today's total heating runtime in seconds. |
| `data.baseline_mean_s` | float | rolling history | Mean daily runtime over the lookback window. |
| `data.threshold_multiplier` | float | config (default 1.5) | Multiplier applied to mean to compute threshold. |
| `data.threshold_s` | float | computed | `baseline_mean_s × threshold_multiplier` — the value `runtime_s` exceeded. |
| `data.lookback_days` | int | config (default 14) | Number of prior days included in baseline. |
| `data.history_count` | int | computed | Actual number of data points used (≤ `lookback_days`). |
| `data.date` | string | caller | Date of the anomaly, `"YYYY-MM-DD"`. |

### JSON Example

```json
{
  "schema": "homeops.consumer.floor_runtime_anomaly.v1",
  "source": "consumer.v1",
  "ts": "2026-03-30T05:00:01.123456+00:00",
  "data": {
    "floor": "floor_2",
    "runtime_s": 5400,
    "baseline_mean_s": 2800.0,
    "threshold_multiplier": 1.5,
    "threshold_s": 4200.0,
    "lookback_days": 14,
    "history_count": 12,
    "date": "2026-03-29"
  }
}
```

---

## Event: `homeops.consumer.floor_2_long_call_escalation.v1`

Fires when floor 2 triggers a long-call warning for the **second or subsequent time** in the
same calendar day. The first long-call warning is emitted as `floor_2_long_call_warning.v1`;
this escalation event fires on the 2nd, 3rd, etc. occurrence so that ongoing furnace issues
remain visible rather than being silently suppressed.

**Trigger:** `long_call_count_today >= 2` (count is incremented before the check, so this fires
on the 2nd warning and every warning after).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `source` | string | hardcoded `"consumer.v1"` | Emitting service. |
| `ts` | ISO 8601 string | `utc_ts()` at emission | Emission timestamp. |
| `data.floor` | string | hardcoded `"floor_2"` | Always floor 2 — this rule is floor-2-specific. |
| `data.long_call_count_today` | int | `daily_state["warnings_triggered"]["floor_2_long_call"]` | How many long-call warnings have fired today (≥ 2 when this event emits). |
| `data.threshold_s` | int | `FLOOR_2_WARN_THRESHOLD_S` env var | The long-call duration threshold in seconds that was exceeded. |
| `data.current_temp` | float \| null | `climate_state["climate.floor_2_thermostat"]["current_temp"]` | Current floor 2 temperature at escalation time; null if climate state unavailable. |
| `data.setpoint` | float \| null | `climate_state["climate.floor_2_thermostat"]["setpoint"]` | Floor 2 setpoint at escalation time; null if climate state unavailable. |

### JSON Example

```json
{
  "schema": "homeops.consumer.floor_2_long_call_escalation.v1",
  "source": "consumer.v1",
  "ts": "2026-01-15T09:32:44.001200+00:00",
  "data": {
    "floor": "floor_2",
    "long_call_count_today": 2,
    "threshold_s": 2700,
    "current_temp": 65.5,
    "setpoint": 68.0
  }
}
```

---

## Event: `homeops.consumer.furnace_short_call_warning.v1`

Fires when a furnace heating session ends in under a configurable threshold. Rapid cycling
(the furnace starting and stopping in quick succession) is a precursor to equipment stress
and lockout conditions.

**Trigger:** `heating_session_ended.v1` where `duration_s < FURNACE_SHORT_CALL_THRESHOLD_S`
and `duration_s > 0`.

**Env var:** `FURNACE_SHORT_CALL_THRESHOLD_S` (default: `120` seconds / 2 minutes).

**Telegram alert:** Sent immediately when the event fires (in both live loop and playback phase).

### Field Table

| Field | Type | Source | Rationale |
|---|---|---|---|
| `schema` | string | hardcoded | Event type identifier. |
| `source` | string | hardcoded `"consumer.v1"` | Emitting service. |
| `ts` | ISO 8601 string | observer event ts | Processing timestamp. |
| `data.duration_s` | int | `heating_session_ended.v1.data.duration_s` | Session duration that triggered the warning. |
| `data.threshold_s` | int | `FURNACE_SHORT_CALL_THRESHOLD_S` env var | The threshold the session duration was below. |
| `data.ended_at` | ISO 8601 string \| null | `heating_session_ended.v1.data.ended_at` | When the session ended. |

### JSON Example

```json
{
  "schema": "homeops.consumer.furnace_short_call_warning.v1",
  "source": "consumer.v1",
  "ts": "2026-04-01T14:23:01.000000+00:00",
  "data": {
    "duration_s": 45,
    "threshold_s": 120,
    "ended_at": "2026-04-01T14:23:00+00:00"
  }
}
```
