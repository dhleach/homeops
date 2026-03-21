# Heating-Cycle Derived Event Schemas

This document specifies the schema design for three derived events that the homeops consumer
will emit when a per-zone heating cycle ends or reaches setpoint. They complement the
existing furnace-level `heating_session_started/ended.v1` and zone-level `floor_call_started/ended.v1`
events by surfacing **per-zone outcome quality** — whether a zone reached its setpoint, how fast,
and how much it overshot.

## Background

The furnace is shared across all zones (floor_1, floor_2, floor_3). Zones call for heat via
dampers; the furnace runs whenever any zone is calling. Multiple zones calling simultaneously
share the same air volume, reducing effective airflow per zone. The consumer tracks per-zone
climate state (setpoint, `current_temp`, `hvac_action`) via `thermostat_mode_changed.v1` events
and accumulates session context in `climate_state` (keyed by `entity_id`).

A **zone heating session** begins when `hvac_action` transitions to `"heating"` and ends when
`hvac_action` leaves `"heating"`. Setpoint-reached is the first event where
`current_temp >= setpoint` while `hvac_action == "heating"`.

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
| `furnace_active` | bool | `binary_sensor.furnace_active` HA helper at session start | This is a HA helper entity (true when ≥1 zone is calling), **not** a physical furnace sensor — it does not detect limit-switch lockout (Code 4/7). If `false`, no zone was calling at session start, which is anomalous and worth flagging; but a `true` value only guarantees at least one zone was calling, not that the burner was physically firing. A future physical furnace temperature sensor will enable real burner-state detection. |

**Excluded fields:**

- `time_of_day` — derivable from `ts`; not stored to avoid redundancy.
- `day_of_week` — derivable from `ts`.
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
    "other_zones_calling": ["binary_sensor.floor_3_heating_call"],
    "furnace_active": true
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
| `furnace_active` | bool | `binary_sensor.furnace_active` HA helper at session start | Same semantics as `zone_time_to_temp.v1`: true when ≥1 zone is calling per the HA helper, not a physical burner-on signal. |

**Excluded fields:**

- `time_of_day`, `day_of_week` — derivable from `ts`.
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
    "other_zones_calling": [],
    "furnace_active": true
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
| `furnace_active` | bool | `binary_sensor.furnace_active` HA helper at session start | Same semantics as `zone_time_to_temp.v1`: true when ≥1 zone is calling. A miss with `furnace_active: false` is anomalous (no zones calling yet session started), but this field cannot confirm lockout — that requires a future physical furnace sensor. |

**Excluded fields:**

- `time_of_day`, `day_of_week` — derivable from `ts`.
- `furnace_lockout_count` — deferred to v2; a single `furnace_active: false` flag is sufficient for v1 filtering.
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
    "furnace_active": true
  }
}
```

---

## Consumer State Requirements

These events require the consumer to track the following per-zone session state (in addition
to existing `climate_state`):

| State key | Type | Purpose |
|---|---|---|
| `session_start_temp[entity_id]` | float \| null | `current_temp` when `hvac_action` last became `"heating"`. |
| `session_start_ts[entity_id]` | datetime \| null | Timestamp when `hvac_action` last became `"heating"`. |
| `setpoint_reached_ts[entity_id]` | datetime \| null | Timestamp of first `current_temp >= setpoint` event during active session. |
| `session_peak_temp[entity_id]` | float \| null | Running max `current_temp` since `hvac_action = "heating"`. Used for `peak_temp` and `closest_temp`. |
| `session_furnace_active[entity_id]` | bool | Snapshot of `binary_sensor.furnace_active` state at session start (HA helper: true when ≥1 zone calling). |
| `session_other_zones[entity_id]` | list[string] | Snapshot of other calling zones at session start. |

All keys are reset to `null`/`[]` when `hvac_action` leaves `"heating"`.

## Notes on Future Work

- **Physical furnace sensor** — `binary_sensor.furnace_active` is a HA helper (true when ≥1
  zone is calling), not a real burner-on signal. A temperature sensor on the furnace heat
  exchanger or supply plenum would give us actual firing state, enable "limit timeout imminent"
  detection, and make the `furnace_active` field meaningful for lockout analysis. This is a
  planned hardware addition.
- **`furnace_lockout_count`** — once a physical furnace sensor exists, tracking Code 4/7 limit
  trips per session would let analysis distinguish "miss because of lockout" from "miss because
  of cold load." Targeted for v2 post-sensor.
- **Multi-zone session correlation** — a future `furnace_session_outcome.v1` event could
  aggregate all three zones' outcomes for a single furnace run, enabling whole-home efficiency
  scoring per blast.
