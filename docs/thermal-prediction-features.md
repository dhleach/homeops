# Thermal prediction feature schema

This document defines the v1 feature contract for HomeOps thermal-response
training rows. It complements the [thermal prediction target
contract](thermal-prediction-targets.md): that document defines what the model
predicts, while this document defines what was knowable when the prediction was
made.

This is a data contract only. It does not add a consumer event, change live
Home Assistant behavior, or make cooling history available. A future dataset
builder may materialize this shape from observer and derived logs.

## Canonical row and information boundary

The canonical row is one continuous active climate session for one floor and
one mode. Its feature snapshot is taken at the target contract's
`prediction_ts`, which is equal to `active_start_ts`:

```json
{
  "schema": "homeops.thermal.features.v1",
  "zone": "floor_2",
  "mode": "heat",
  "prediction_ts": "2026-08-27T06:00:00+00:00",
  "features": {
    "start_temp_f": 65.0,
    "start_setpoint_f": 68.0,
    "setpoint_delta_f": 3.0,
    "outdoor_temp_f": 28.4,
    "outdoor_temp_age_s": 42,
    "other_zones_calling": ["floor_1"],
    "concurrent_zone_count": 1,
    "start_minute_of_day_local": 360,
    "prior_zone_runtime_24h_s": 5400,
    "prior_zone_runtime_history_complete": true,
    "indoor_humidity_pct": null,
    "occupancy_state": null,
    "weather_humidity_pct": null,
    "weather_wind_speed_mph": null,
    "weather_cloud_cover_pct": null
  },
  "provenance": {
    "climate_observed_at": "2026-08-27T06:00:00+00:00",
    "outdoor_observed_at": "2026-08-27T05:59:18+00:00",
    "zone_call_observed_at": "2026-08-27T06:00:00+00:00",
    "history_window_start_ts": "2026-08-26T06:00:00+00:00",
    "history_cutoff_ts": "2026-08-27T06:00:00+00:00"
  }
}
```

`zone`, `mode`, and `prediction_ts` are row metadata and model inputs. The
`provenance` object is audit metadata, not a feature. Each source timestamp
must be at or before `prediction_ts`; a later consumer emission timestamp is
never a substitute for the source event timestamp.

## Feature field contract

| Field | Type / units | Source | Timing rule | Missing / null policy | Leakage status |
|---|---|---|---|---|---|
| `zone` | enum: `floor_1`, `floor_2`, `floor_3` | Climate entity mapping | Session boundary | Required; quarantine an unknown zone | Safe metadata |
| `mode` | enum: `heat` or `cool` | Climate `new_state`, validated against the active action | Session boundary | Required; `off`, `idle`, or a mode/action mismatch is not a training row | Safe discriminator |
| `prediction_ts` | ISO 8601 UTC timestamp | Climate action transition timestamp | Exactly `active_start_ts` | Required; never use processing time | Safe boundary |
| `start_temp_f` | finite float, °F | Climate `current_temperature` attribute | Boundary observation at `prediction_ts` | Required; do not forward-fill an unknown start measurement | Safe |
| `start_setpoint_f` | finite float, °F | Climate `temperature` attribute | Boundary observation at `prediction_ts` | Required; do not replace it with the session-end setpoint | Safe |
| `setpoint_delta_f` | finite float, °F | Derived from the two start fields: heat = setpoint − current, cool = current − setpoint | Computed at `prediction_ts` | Required when both start fields are valid; zero/negative means already at target or an invalid directional row, not a fabricated positive gap | Safe derived feature |
| `outdoor_temp_f` | finite float, °F \| null | `sensor.outdoor_temperature` | Latest valid sample at or before `prediction_ts`, no older than 3 hours | Null when absent, invalid, or stale; never impute from a future sample | Safe as-of feature |
| `outdoor_temp_age_s` | non-negative integer seconds \| null | Difference between `prediction_ts` and the selected outdoor sample | Computed at `prediction_ts` | Null whenever `outdoor_temp_f` is null | Safe derived feature |
| `other_zones_calling` | sorted array of zone IDs \| null | Per-zone call signals at the session boundary | Snapshot at or immediately before `prediction_ts` | `[]` means all relevant call states were known and none were active; null means the call state was unavailable. Do not use heating-call sensors as cooling calls | Safe boundary snapshot |
| `concurrent_zone_count` | non-negative integer \| null | Count of `other_zones_calling` | Computed from the same boundary snapshot | Null when the call snapshot is null; otherwise equal to the array length | Safe derived feature |
| `start_minute_of_day_local` | integer `0`–`1439` | Derived from `prediction_ts` | Convert using the fixed dataset timezone (`America/New_York` in production) | Required when the timestamp is valid; do not change timezone within a dataset | Safe derived feature |
| `prior_zone_runtime_24h_s` | non-negative integer seconds \| null | Valid completed sessions for the same zone and mode | Sum sessions in `[prediction_ts − 24h, prediction_ts)` using only information already observed | Zero when a complete lookback has no qualifying sessions; null when the source history is incomplete | Safe as-of aggregate; excludes the current session |
| `prior_zone_runtime_history_complete` | boolean | Dataset coverage and boundary-quality check | Evaluated at `prediction_ts` | Required; false means the runtime aggregate must be treated as unavailable/null | Safe quality flag |
| `indoor_humidity_pct` | float `0`–`100` \| null | Climate `current_humidity` attribute | Boundary observation; no stale carry-forward | Null when the attribute is absent, invalid, or unavailable | Safe boundary feature |
| `occupancy_state` | enum: `occupied` or `unoccupied` \| null | Explicit configured Home Assistant occupancy source | Latest valid sample at or before `prediction_ts` under the source's declared freshness limit | Null when no source is configured or the sample is stale; never infer occupancy from temperature or mode | Safe as-of feature |
| `weather_humidity_pct` | float `0`–`100` \| null | Versioned weather sensor/import | Latest valid sample at or before `prediction_ts` under the dataset's declared freshness limit | Null when the feed or field is unavailable; no future backfill | Safe as-of feature |
| `weather_wind_speed_mph` | non-negative float \| null | Versioned weather sensor/import | Same as `weather_humidity_pct` | Null when unavailable or invalid | Safe as-of feature |
| `weather_cloud_cover_pct` | float `0`–`100` \| null | Versioned weather sensor/import | Same as `weather_humidity_pct` | Null when unavailable or invalid | Safe as-of feature |

The feature builder must preserve nulls and quality flags into the exported
row. It may use a documented model-specific encoder later, but the encoder
must distinguish a real zero from an unavailable value and must be fit only on
the training partition.

## Source availability and mode symmetry

The three canonical climate entities already preserve mode, current
temperature, setpoint, and `hvac_action` in observer attributes. The outdoor
temperature sensor is also part of the current observer configuration. These
sources make the required start features and outdoor feature structurally
available for heating when the readings are valid.

The existing per-floor binary sensors are named
`binary_sensor.floor_*_heating_call`; they are valid concurrent-call sources
for heat only. Cooling rows require an explicitly instrumented normalized zone
call signal. Until that exists, a cooling row cannot claim that its concurrent
call feature is known, and cooling remains unavailable under the target
contract. Heating history must never be relabeled as cooling history.

Heat and cool use the same field names, types, null rules, and pipeline. The
`mode` value selects the directional interpretation of `setpoint_delta_f` and
the active call signal; it does not create a second feature schema or a second
set of field names. A model may still be trained per mode or with `mode` as a
categorical input, but that is an evaluation decision rather than a data
contract change.

## Point-in-time and leakage rules

The feature builder may read only observer/derived records whose source event
timestamp is `<= prediction_ts`. The following are explicitly prohibited as
inputs for the same row:

- `active_end_ts`, `target_crossing_ts`, `time_to_setpoint_s`, and
  `zone_runtime_s`;
- end temperature, degrees gained, overshoot, setpoint-miss outcome, or any
  post-start climate reading;
- current-session elapsed runtime or any value computed from the session end;
- future outdoor/weather/occupancy readings, even if they are more complete;
- target labels from `zone_time_to_temp.v1` or another completed-session event;
- a final setpoint or final mode substituted for the boundary snapshot.

The current session is excluded from all history aggregates. A source restart,
truncated log, or missing boundary is represented by a null/quality flag and a
quarantined row where required; it is never repaired with consumer processing
time or an invented zero.

## Relationship to targets and downstream work

This schema supplies features to the labels in
[`thermal-prediction-targets.md`](thermal-prediction-targets.md). It does not
decide minimum sample counts, train/test split policy, baseline/model family,
confidence calibration, or recommendations. Those remain separate design
tasks. The next dataset work may add a normalized exporter, but it must retain
this exact information boundary and the target contract's incomplete-session
semantics.
