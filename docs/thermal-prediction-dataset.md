# Thermal prediction dataset export

This document defines the deterministic JSONL export produced by
scripts/export_thermal_dataset.py. It materializes the point-in-time feature
contract and the mode-aware target contract into training rows. It is an
offline read-only transformation; it does not change the observer, consumer,
Home Assistant, Prometheus, or thermostat behavior.

## The row boundary

Each routine row represents one observed floor HVAC session. A marked primary
row may instead represent the intervention-bounded slice of one observed
session. Every row separates:

- features: facts available at the prediction boundary
- labels: outcomes observed after that boundary
- provenance: the source JSONL lines, schemas, timestamps, and event IDs

The exporter can retain a row whose start or end boundary is incomplete, but it
marks the affected label as ineligible or right-censored instead of inventing a
timestamp or duration.

For an exact deliberate experiment marker, the primary row may use the
observed thermostat setpoint transition as its prediction boundary when the
HVAC call was already active. The original HVAC-session start remains in
`provenance.experiment.boundary.session_start_ts`; the row's active interval,
features, and labels begin at the intervention transition. Later HVAC sessions
inside the same marker interval are retained as follow-on rows with
`provenance.experiment.boundary.role: "continuation"` and are not separate
experiments.

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
- Routine rows retain the start setpoint and temperature from the HVAC session
  start. For an exact marked experiment, a directional setpoint transition
  inside that session is an explicit exception: the primary row is rebased to
  the transition and records both boundaries in experiment provenance.
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
line, event schema, original event ID when present, and source timestamp. When
the source event explicitly reports an active HVAC action, the reference also
preserves canonical `hvac_action` evidence (`heating` or `cooling`); an idle
end-state is not treated as active-action evidence. The exporter also preserves
experiment metadata when an event contains fields such as experiment_id,
operation_type, or intervention. This keeps deliberate
interventions distinguishable from routine operation for the future coupled
thermal-model and what-if work.

### Natural-language experiment marker sidecar

The first deliberate cooling suite uses the separate append-only marker log
`state/experiments/markers.jsonl`, with schema
`homeops.thermal.experiment_marker.v1`. The marker recorder accepts the
operator's plain-language start/end/abort message and preserves a unique
`experiment_id`, stable configuration ID, mode, active/suppressed zones,
timestamps, planned duration, confidence, raw message, and source-message
identity. Source-message identity makes Telegram retries idempotent. It is a
data-only path: it does not call Home Assistant or change a thermostat.

The exporter's optional `--experiment-log` input defaults to that sidecar and
is ignored when the file does not yet exist. A bounded marker interval is joined
to overlapping sessions for its active zones and copied to
`provenance.experiment`; marker source references are retained under
`provenance.experiment_marker_events`. For an exact live marker, a directional
setpoint transition inside an active session becomes the primary row boundary;
the boundary record retains marker, intervention, and original HVAC-session
timestamps. A session that starts inside the marker can be the primary row
when the intervention is already reflected at session start. Additional
sessions in the same interval are marked as continuations, so their rows are
not mistaken for independent experiments. The experiment ID is provenance and
whole-experiment split metadata, never a model feature. A marker does not
manufacture passive-floor rows or turn an absent response into evidence.

The canonical six-configuration protocol and repeat-run policy are documented
in [`docs/thermal-experiment-checklist.md`](thermal-experiment-checklist.md).

## Validation and quarantine

The exporter and validator are separate offline steps. The exporter preserves
the source evidence and materializes the stable row shape; the validator reads
that JSONL without changing it and writes three artifacts:

1. validated rows, unchanged from the input;
2. quarantine records containing the unchanged original row (or the original
   malformed line) and sorted machine-readable `reason_codes`; and
3. a deterministic report with reason counts and eligible-label coverage for
   every floor/mode slice.

Run the validator after exporting:

~~~bash
python3 scripts/validate_thermal_dataset.py \
  --input state/thermal-training.jsonl \
  --valid-out state/thermal-training.valid.jsonl \
  --quarantine-out state/thermal-training.quarantine.jsonl \
  --report-out state/thermal-training.validation.json
~~~

The validator uses a three-hour maximum outdoor-reading age and a conservative
seven-day maximum session/label duration by default. Both are configurable for
replay or fixture work. It detects malformed rows, missing boundaries,
timestamp-order errors, non-finite or impossible values, heat/cool directional
mismatches, duplicate row IDs, overlapping sessions, stale outdoor inputs,
feature target leakage, malformed experiment metadata, and incompatible
heating/cooling source schemas. A cooling row must carry explicit cooling
source evidence: either a mode-specific cooling schema or a source reference
that explicitly reports `hvac_action: cooling`. The row's mode alone is not
evidence, and an aggregate whole-home cooling event is never accepted as a
floor row.

The exporter stores duration labels (`time_to_setpoint_s` and
`zone_runtime_s`) to milliseconds. The validator therefore permits an absolute
timestamp/label difference of up to 1 millisecond for those two consistency
checks. This is a representation-precision boundary, not a general relaxation
of timestamp ordering, impossible-duration, overlap, provenance, directional,
or leakage checks; discrepancies beyond 1 millisecond remain quarantined.

Missing end boundaries are a quality warning when another target remains
eligible: for example, a row can train `time_to_setpoint_s` while its runtime
label is right-censored. A row with no eligible target is quarantined, as are
all rows sharing a duplicate ID or an overlapping interval. This preserves
useful censoring and behavior evidence for audit while ensuring ordinary
regression training receives only the target-eligible rows. Missing outdoor or
cross-zone features remain visible as non-fatal quality annotations because
the feature contract permits nulls for unavailable as-of inputs.

The quarantine record uses this shape:

~~~json
{
  "schema": "homeops.thermal.training_row_quarantine.v1",
  "source_line": 14,
  "row_id": "floor_2:heat:2026-08-27T06:00:00+00:00",
  "reason_codes": ["stale_outdoor_input"],
  "row": {"schema": "homeops.thermal.training_row.v1"}
}
~~~

The quality report has schema
`homeops.thermal.training_row_validation.v1`. Its `by_zone_mode` section
reports input, valid, quarantined, and eligible-label counts separately for
`floor_1`, `floor_2`, and `floor_3` in both modes, together with total and
fatal `reason_counts`. A target slice below `--minimum-eligible-rows` is
reported as `insufficient_data`; no score or replacement value is invented.

## Offline baseline evaluation

Pass the validator's valid-row output to
`scripts/evaluate_thermal_models.py` for the first model comparison. The
evaluator fits the historical-median reference, the transparent
degree-minute/thermal-response baseline, and Ridge regression only on the
earlier chronological partition. It writes a per-floor/per-mode/per-target
evaluation report and a separate reproducible model-artifact file; it never
rewrites the validated rows.

~~~bash
python3 scripts/evaluate_thermal_models.py \
  --input state/thermal-training.valid.jsonl \
  --report-out reports/thermal-training-evaluation.json \
  --artifacts-out reports/thermal-training-models.json \
  --code-version "$(git rev-parse HEAD)"
~~~

The report schema is `homeops.thermal.training_evaluation.v1` and the model
artifact schema is `homeops.thermal.model_artifacts.v1`. The evaluator keeps
session and experiment groups together, excludes all post-start labels from
features, preserves explicit `insufficient_data` results, and remains an
offline analysis step rather than a prediction service or thermostat-control
path.

## Running the export

From the repository root:

~~~bash
python3 scripts/export_thermal_dataset.py \
  --observer-log state/observer/events.jsonl \
  --derived-log state/consumer/events.jsonl \
  --experiment-log state/experiments/markers.jsonl \
  --out state/thermal-training.jsonl
~~~

Use --out - (the default) to write JSONL to stdout. A compact read summary is
written to stderr. Re-running with the same input files produces byte-for-byte
identical JSONL.

The exporter consumes existing logs only. It does not train a model, call an
LLM, make a recommendation, or write to Home Assistant.
