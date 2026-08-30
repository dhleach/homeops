# Thermal prediction target contract

This document freezes the labels for the first HomeOps thermal-prediction
workstream. It is a design gate for the data pipeline; it does not add a
model, change the consumer, add an event schema, or grant any thermostat
control capability.

The canonical training unit is one continuous active climate session for one
floor and one HVAC mode. Every timestamp is ISO 8601 UTC, and every duration
is elapsed wall-clock time in seconds. Durations remain unrounded until a
report renders them.

## Scope and mode availability

The contract applies to the three canonical floors:

- `floor_1`
- `floor_2`
- `floor_3`

The normalized `mode` value is either `heat` or `cool`. The exporter
materializes both modes from their respective observer and derived event
contracts:

- `heat` uses raw climate-action boundaries plus the existing heating
  outcome events, including `homeops.consumer.zone_time_to_temp.v1`.
- `cool` uses explicit cooling session boundaries and cooling outcome events.
- Heating records must not be relabeled as cooling records, and `off`/`idle`
  intervals must not be used as synthetic cooling sessions.

The existing `zone_time_to_temp.v1` event remains useful historical evidence,
but it is not by itself the final mode-aware training row: it has no normalized
`mode` field and records the setpoint captured at session end. The exporter
snapshots the starting setpoint from the observer boundary and applies the
rules below rather than silently reusing a possibly changed end value.

## Session boundary contract

One row represents one continuous active climate interval for one floor. The
canonical boundary is the climate entity's action transition:

| Field | Definition |
|---|---|
| `zone` | `floor_1`, `floor_2`, or `floor_3`, resolved from the climate entity. |
| `mode` | `heat` when the active action is `heating`; `cool` when it is `cooling`. |
| `active_start_ts` | First observed timestamp at which the zone's `hvac_action` enters the mode's active action. |
| `active_end_ts` | First observed timestamp at which that action leaves the active action, or the mode changes/off state ends it. |
| `prediction_ts` | Equal to `active_start_ts`; this is the information boundary for a prediction made at call start. |
| `start_temp_f` | Current temperature observed at `active_start_ts`. |
| `start_setpoint_f` | Setpoint observed at `active_start_ts`; it is the target for this session. |
| `target_crossing_ts` | First in-session temperature observation that satisfies the directional target rule. Null when the target is not reached. |

The raw event timestamp is authoritative. Do not infer a boundary between two
observations, backdate it from a later event, or replace it with consumer
processing time. The existing heating floor-call events can corroborate a
session, but a missing climate boundary is not repaired by guessing from a
binary sensor.

## Targets

### Time to setpoint

`time_to_setpoint_s` measures how long the zone takes to reach the setpoint
from the start of the active session:

```text
time_to_setpoint_s = target_crossing_ts - active_start_ts
```

The first qualifying temperature sample wins. The directional crossing rules
are:

| Mode | Reached when |
|---|---|
| `heat` | `current_temp_f >= start_setpoint_f` while the active heating action is still in progress. |
| `cool` | `current_temp_f <= start_setpoint_f` while the active cooling action is still in progress. |

The value must be positive and is measured in seconds. A zone already at or
beyond its target at `active_start_ts` receives the status
`already_at_target`, not a fabricated zero-second learning target.

The target is valid only when the starting boundary, starting temperature,
starting setpoint, and crossing observation are valid and the setpoint has not
changed before the crossing. A setpoint change makes the original target
ambiguous; the row may retain an observed runtime, but it is not eligible for
the time-to-setpoint target.

### Zone runtime

`zone_runtime_s` measures the complete active interval for that floor and
mode:

```text
zone_runtime_s = active_end_ts - active_start_ts
```

This is zone/climate runtime, not shared-furnace runtime. The whole-home
`heating_session_ended.v1` duration must not be substituted for a per-floor
target. A valid runtime requires both observed boundaries, a matching mode,
and a positive elapsed duration. Whether the zone reached its setpoint is a
separate outcome and does not determine runtime eligibility.

## Incomplete and censored sessions

The label builder must preserve the reason a row is unusable instead of
silently dropping it or converting an unknown value to zero.

| Situation | `time_to_setpoint_s` | `zone_runtime_s` | Required treatment |
|---|---|---|---|
| Target reached with valid start data | Eligible | Eligible only if an end is also observed | Keep the crossing duration; runtime may remain pending. |
| Active action ends before target crossing | Right-censored | Eligible if the end boundary is valid | Keep the observed active duration as censoring evidence, but do not train time-to-setpoint on it. |
| Active session is still open at the log boundary | Eligible only if a valid crossing was already observed | Right-censored | Do not invent an end timestamp. |
| Start boundary missing, including a consumer restart mid-session | Ineligible | Ineligible | Mark `missing_start_boundary`; do not reconstruct a start from processing time. |
| End boundary missing because of restart or truncated history | Eligible only if crossing data is independently valid | Right-censored/ineligible | Mark `missing_end_boundary` for runtime. |
| Setpoint changes before crossing | Censored/ineligible | Eligible if both active boundaries are valid, with a change flag | Never compare the final setpoint to the starting temperature for the original target. |
| Mode changes or action does not match normalized mode | Ineligible | Ineligible for the combined interval | Split at the transition only when the event stream supplies a valid new start. |
| Missing/non-finite temperature, setpoint, or invalid timestamps | Ineligible | Ineligible if the boundary is affected | Mark `invalid_measurement` or `invalid_timestamp`; never coerce it. |
| Already at/beyond setpoint at start | `already_at_target`, not a training target | Runtime may still be eligible | Keep it for behavior analysis, not as a positive time-to-setpoint example. |

Right-censored records are valuable for survival-style analysis and data
quality reporting, but they must not be presented to an ordinary regression
model as if the observed duration were the time to setpoint.

## Worked examples

For a heating session on `floor_2`:

```text
active_start_ts     = 2026-08-27T06:00:00Z
start_temp_f        = 65.0
start_setpoint_f    = 68.0
target_crossing_ts  = 2026-08-27T06:18:30Z
active_end_ts       = 2026-08-27T06:21:00Z
```

The row has `time_to_setpoint_s = 1110` and `zone_runtime_s = 1260`. If the
same session ended at 06:10:00 without crossing 68°F, runtime is 600 seconds
and the time-to-setpoint target is right-censored; 600 must not be treated as
the target duration.

## Explicit non-decisions

This child task freezes target labels only. The following remain separate
roadmap decisions and must not be smuggled into a label implementation:

- the complete feature schema and null policy;
- the minimum sample count and time-aware train/test split;
- transparent baseline and learned-model selection;
- confidence, calibration, and evaluation thresholds;
- recommendations, mitigation, or thermostat write access.

Those later tasks consume this contract. The exporter can now produce cooling
rows, but an eligible label still requires valid cooling boundaries and
measurements; a row marked censored or incomplete must not be presented as
evidence that the model has learned cooling behavior.
