# Thermal prediction dataset export

This document defines the deterministic JSONL export produced by
scripts/export_thermal_dataset.py. It materializes the point-in-time feature
contract and the mode-aware target contract into training rows. It is an
offline read-only transformation; it does not change the observer, consumer,
Home Assistant, Prometheus, or thermostat behavior.

## The row boundary

Each row represents one observed floor HVAC session. The row separates:

- features: facts available at the prediction boundary
- labels: outcomes observed after that boundary
- provenance: the source JSONL lines, schemas, timestamps, and event IDs

The exporter can retain a row whose start or end boundary is incomplete, but it
marks the affected label as ineligible or right-censored instead of inventing a
timestamp or duration.

## Output schema

Every output line has schema homeops.thermal.training_row.v1 and this shape:

~~~json
{
  "schema": "homeops.thermal.training_row.v1",
  "row_id": "floor_2:heat:2024-01-15T10:00:05+00:00",
  "zone": "floor_2",
  "mode": "heat",
  "prediction_ts": "2024-01-15T10:00:05+00:00",
  "active_start_ts": "2024-01-15T10:00:05+00:00",
  "active_end_ts": "2024-01-15T10:07:05+00:00",
  "target_crossing_ts": "2024-01-15T10:05:05+00:00",
  "features": {
    "start_temp_f": 69.0,
    "start_setpoint_f": 70.0,
    "setpoint_delta_f": 1.0,
    "outdoor_temp_f": 30.0,
    "outdoor_temp_age_s": 305.0,
    "other_zones_calling": ["floor_1"],
    "concurrent_zone_count": 1,
    "start_minute_of_day_local": 600,
    "prior_zone_runtime_24h_s": null,
    "prior_zone_runtime_history_complete": false
  },
  "labels": {
    "time_to_setpoint_s": 300.0,
    "zone_runtime_s": 420.0
  },
  "label_status": {
    "time_to_setpoint": "eligible",
    "zone_runtime": "eligible"
  },
  "observations": {
    "end_temp_f": 70.2,
    "observed_duration_s": null,
    "outcome_types": ["target_reached"]
  },
  "quality_flags": [],
  "provenance": {
    "start_boundary": "observed",
    "source_events": []
  }
}
~~~

The example omits source-event details only to keep it short; real rows retain
line-level references under provenance.source_events.

time_to_setpoint_s and zone_runtime_s are null whenever their corresponding
label_status is not eligible. right_censored means the event stream ended
before the outcome was observed. missing_start_boundary and invalid_timestamp
are never repaired by subtracting a derived duration from an outcome timestamp.

## Source and mode rules

- Heating rows use raw observer climate-action transitions and the existing
  heating outcome events: zone_time_to_temp.v1, zone_setpoint_miss.v1, and
  zone_overshoot.v1.
- Cooling rows use the explicit cooling session start/end and sibling outcome
  events: thermostat_cooling_session_started.v1,
  thermostat_cooling_session_ended.v1, zone_time_to_cool.v1,
  zone_cooling_setpoint_miss.v1, and zone_cooling_undershoot.v1.
- Aggregate whole-home cooling_session_started.v1 and
  cooling_session_ended.v1 events are not converted into floor rows.
- Existing heating history is never relabeled as cooling.
- No cooling row is synthesized from an idle interval, an off interval, or a
  heating event.
- The start setpoint and temperature are retained from the session start. A
  later setpoint is not substituted for the original target.
- The exporter retains outcome-only rows when a source log begins after the
  session start, but those rows have no eligible start-based label.

## Leakage boundary

The exporter puts only as-of information under features: start temperature,
start setpoint, outdoor readings at or before the start, the concurrent-zone
snapshot, local time, and an optional prior-runtime aggregate. Session end,
target crossing, final temperature, total duration, and outcome status are not
features. They are labels, observations, or provenance.

By default the source log is treated as incomplete for the prior-runtime
feature, so prior_zone_runtime_24h_s is null and
prior_zone_runtime_history_complete is false. The optional --history-complete
flag enables the deterministic lookback calculation for a caller that has
explicitly supplied complete history.

## Provenance and experiments

Each source reference includes the source log (observer or derived), JSONL
line, event schema, original event ID when present, and source timestamp. The
exporter also preserves experiment metadata when an event contains fields such
as experiment_id, operation_type, or intervention. This keeps deliberate
interventions distinguishable from routine operation for the future coupled
thermal-model and what-if work.

The current exporter does not reject or quarantine malformed rows. The
separate data-quality task will add those policies after this stable row shape
is available.

## Running the export

From the repository root:

~~~bash
python3 scripts/export_thermal_dataset.py \
  --observer-log state/observer/events.jsonl \
  --derived-log state/consumer/events.jsonl \
  --out state/thermal-training.jsonl
~~~

Use --out - (the default) to write JSONL to stdout. A compact read summary is
written to stderr. Re-running with the same input files produces byte-for-byte
identical JSONL.

The exporter consumes existing logs only. It does not train a model, call an
LLM, make a recommendation, or write to Home Assistant.
