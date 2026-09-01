# Cooling-coupling experiment checklist

This is the canonical first suite for collecting deliberate, reversible
cooling evidence across the three HomeOps floors. It is a data-collection
protocol, not a thermostat-control feature. The marker recorder only records
operator intent and never calls Home Assistant.

## Protocol

Run a test when the household schedule makes it practical. The initial
screening window is:

1. 30 minutes of ordinary operation for a baseline;
2. a marker followed by the agreed cooling setpoint change;
3. 30 minutes of intervention; and
4. restoration of ordinary setpoints followed by 30 minutes of recovery.

The intervention does not need to reach thermal steady state. The transient
response and the temperature drift on the other floors are useful evidence.
Do not use an extreme setpoint solely to force a larger label; use a consistent
small step from the normal target and stop if the household or equipment gives
you a reason to stop. A 45-minute intervention is optional only after an
uneventful initial run whose response is still changing. There is no initial
two-hour requirement.

## Canonical configurations

The checklist is derived from the marker log, so a completed run is checked
automatically. The configuration ID is stable across repeated runs.

| Done | ID | Active floor(s) | Suppressed floor(s) |
|---|---|---|---|
| [ ] | `cool-s1-f1` | Floor 1 | Floors 2 and 3 |
| [ ] | `cool-s1-f2` | Floor 2 | Floors 1 and 3 |
| [ ] | `cool-s1-f3` | Floor 3 | Floors 1 and 2 |
| [ ] | `cool-p12` | Floors 1 and 2 | Floor 3 |
| [ ] | `cool-p13` | Floors 1 and 3 | Floor 2 |
| [ ] | `cool-p23` | Floors 2 and 3 | Floor 1 |

The recommended order is the three singleton screens first, followed by the
three pair screens after reviewing the singleton trajectories. A run is
screening evidence, not model validation.

## Plain-language operation

Derek can send ordinary messages; the main session supplies the structured
fields internally. These messages are recorded without waiting for a reply:

- `Starting a 30-minute cooling test on Floor 1.`
- `Starting a cooling test on Floors 1 and 3.`
- `Floor 1 test ended.`
- `Stop the test — the kids are getting cold.`

The duration defaults to 30 minutes when a live start omits it. The actual
setpoint is recovered from the subsequent Home Assistant event when available;
it does not need to be included in the message. An explicit target may still be
recorded as operator context.

If a live marker was missed, use an approximate retrospective message such as:

`I ran a Floor 1 cooling test last night around 11:30 pm for about 30 minutes.`

Retrospective markers retain the received timestamp separately, use the
declared approximate interval for correlation, and carry
`confidence: approximate`. They must not be presented as exact intervention
boundaries.

## Repeated runs

Repeated runs on one floor are allowed and useful. They measure repeatability
under different outdoor temperatures, starting temperatures, occupancy, and
thermal memory. The recorder creates a new `experiment_id` for every live
start, while the stable configuration ID groups the runs for the checklist.

There are two practical downsides:

- if Floor 1 is repeated many times before Floors 2 and 3 are tested, the first
  evidence review is unbalanced and tells us less about the other floors;
- if two runs of the same configuration overlap, `Floor 1 test ended` is
  intentionally ambiguous and must identify which run to close.

Do not discard useful repeats merely to balance the checklist. Run whichever
configuration is practical, avoid overlapping runs, and retain the run count.
The evaluator keeps each experiment together when making chronological train,
validation, and test partitions, so repetitions do not leak across the
evaluation boundary.

## Marker and export boundary

Markers are stored in the append-only sidecar
`state/experiments/markers.jsonl` with schema
`homeops.thermal.experiment_marker.v1`. A record includes the action/status,
unique experiment ID, stable configuration ID, mode, active and suppressed
floors, timestamps, planned duration, confidence, raw message, and source
message identity. Source-message identity makes retries idempotent.

The exporter joins a bounded marker interval to overlapping sessions for the
marked active floors and copies the marker under `provenance.experiment` plus
`provenance.experiment_marker_events`. The experiment ID is provenance and
grouping metadata, never a model feature. Markers do not manufacture passive
floor training rows or claim that a floor responded when no telemetry session
exists.

To inspect the current derived checklist from a deployed checkout:

```bash
python3 scripts/thermal_experiment_marker.py checklist
```

The offline export includes the sidecar automatically; an alternate path can
be supplied explicitly:

```bash
python3 scripts/export_thermal_dataset.py \
  --observer-log state/observer/events.jsonl \
  --derived-log state/consumer/events.jsonl \
  --experiment-log state/experiments/markers.jsonl
```
