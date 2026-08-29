# Thermal prediction baselines and evaluation protocol

This document defines the v1 comparison ladder and evaluation contract for
HomeOps thermal predictions. It consumes the target definitions in
thermal-prediction-targets.md and the point-in-time feature rules in
thermal-prediction-features.md.

This is a design contract. It does not train a model, add a consumer event,
change production telemetry, invoke an LLM, make a recommendation, or grant
thermostat write access.

## Decision summary

The v1 evaluation ladder is:

1. A historical-median reference baseline.
2. A transparent degree-minute/thermal-response baseline.
3. One small learned model family: regularized linear regression (Ridge).

The same chronological evaluation protocol is used for every candidate. The
primary targets are the mode-aware time_to_setpoint_s and zone_runtime_s
labels. Results are reported separately by zone, HVAC mode, and target; a
pooled score may summarize the results but cannot hide a sparse or unsafe
floor-specific result.

The future cross-zone thermal model is deliberately downstream of this v1
session-level comparison. It will use temperature trajectories and deliberate
experiments to learn cross-floor coupling and support bounded what-if queries.
This document keeps that path possible without pretending that the first
session model is already a causal simulator.

## What counts as a model

A model is any reproducible mapping from inputs to a prediction. It is not
synonymous with a neural network or an LLM:

~~~text
prediction = f(inputs; parameters)
~~~

A lookup table, a hand-written equation, a linear regression, a decision tree,
and a neural network are all models. Training or fitting means estimating
parameters from historical examples. A historical median has almost no
parameters; a linear model has a small number of coefficients; a neural
network has many learned weights.

For a HomeOps runtime prediction, the inputs can include the point-in-time
temperature, setpoint gap, outdoor temperature, concurrent-zone state, local
time, and prior runtime. The output is a numeric prediction such as seconds
until the target or seconds of zone runtime. The output must never use the
current session's end time or any other future value.

## Model ladder

### Reference baseline: historical median

For each eligible combination of zone, mode, and target, predict the median
label observed in the training partition:

~~~text
predicted_duration = median(training_labels for zone, mode, target)
~~~

The median is preferred to the mean because a few unusually long calls should
not move the reference as much. The value is fit independently inside each
training partition; validation and test labels cannot influence it.

This baseline answers, "What usually happens for this floor and mode?" It is
the minimum useful yardstick, not the intended thermal explanation.

### Primary v1 baseline: degree-minute/thermal response

The primary transparent baseline uses the directional setpoint gap and
weather-dependent response rate. The existing read-only time-to-temperature
analysis is the prototype for this form: it models seconds per degree as a
small ordinary-least-squares relationship to outdoor temperature, then scales
the result by the requested positive gap.

~~~text
seconds_per_degree = intercept + slope * outdoor_temperature
predicted_time_to_setpoint = seconds_per_degree * setpoint_gap
~~~

Ordinary least squares chooses the line whose squared residuals are smallest.
The important design property is not the particular fitting algorithm; it is
that the relationship remains small, inspectable, directional, and grounded
in the amount of temperature change requested.

For zone_runtime_s, the baseline remains target-specific: it may use a
training-only runtime-per-degree relationship or a simple non-negative
duration relationship to the starting gap and outdoor condition when those
measurements are available. It must not substitute shared whole-home furnace
or inferred-AC runtime for a zone target. If the required measurements are
missing or too sparse, the result is insufficient_data rather than a guessed
number.

The current scripts/time_to_temp.py report is useful planning evidence, but a
formal evaluator must refit the baseline inside each training partition. A
report made from the entire history is not itself a valid future test.

### First learned family: regularized linear regression

The first learned family is a small regularized linear regression model,
implemented as Ridge regression when the training pipeline is built. It uses
the v1 point-in-time features, with declared encodings for categorical zone
and mode fields. Separate target estimators may be used for time-to-setpoint
and runtime; the evaluation still reports every zone/mode slice separately.

Ridge is selected for the first comparison because it:

- works with relatively small tabular datasets;
- makes feature direction and approximate influence inspectable;
- provides a stable comparison against the degree-minute relationship;
- can include explicitly declared cross-zone terms without requiring a deep
  sequence model.

Gradient-boosted trees, sequence models, and neural networks remain possible
later. They are not justified merely by being more complicated. A later model
family must beat the v1 ladder on future data and preserve the data-quality,
uncertainty, and safety boundaries.

## Information and fitting boundary

Every candidate obeys the target and feature contracts:

- Features are sourced at or before prediction_ts, which equals the active
  session start.
- The current session's end, target crossing, final temperature, final
  setpoint, duration, and post-start readings are excluded.
- Censored or incomplete rows are retained for quality reporting but are not
  presented to ordinary regression as completed targets.
- Duplicate, invalid, or out-of-bound rows are quarantined according to the
  data-quality contract.
- Feature encoders, scalers, coefficients, medians, and uncertainty
  calibration are fit only on the training partition.
- Rows from the same session, overlapping interval, or deliberate experiment
  stay in the same split.

The learned model and the baselines receive the same eligible rows and the
same information boundary. A model cannot win by receiving features that the
baseline was forbidden to use.

## Time-aware evaluation

The primary split is chronological:

~~~text
earlier observations -> training
later observations   -> validation/calibration
latest observations  -> locked test
~~~

When the available history is too small for one fixed split, use
walk-forward/expanding-window evaluation. Each fold trains on the past and
tests on a later contiguous block. Do not randomly shuffle neighboring
sessions: adjacent sessions can share weather, house state, and an
experiment's thermal memory, which would make the test unrealistically easy.

Deliberate thermal experiments are held out by whole experiment, not split
row-by-row. This creates the right future test for the later cross-zone
thermal model: can it predict a response to an intervention it did not train
on?

The evaluator records the number of eligible, censored, invalid, and skipped
rows in every split. A target/floor/mode slice below the configured minimum
data requirement reports insufficient_data and does not manufacture a score.

## Metrics

### Numeric prediction metrics

All errors use the natural unit of the target: seconds or minutes for
durations, and degrees Fahrenheit for a future-temperature target.

| Metric | Definition | Why it matters |
|---|---|---|
| MAE | Mean of absolute prediction errors | Primary, homeowner-readable average accuracy. |
| P95 absolute error | 95th percentile of absolute errors | Exposes bad cases that an average can hide. |
| Signed bias | Mean predicted value minus actual value | Shows systematic overprediction or dangerous underprediction. |
| Interval coverage | Fraction of actual values inside the claimed interval | Tests whether uncertainty statements are honest. |
| Interval width | Width of the prediction interval | Prevents a model from achieving coverage only by being uselessly vague. |
| Eligible sample count | Number of valid test labels | Makes sparse results visible and comparable. |

The default uncertainty report is an 80% prediction interval when the
training pipeline can support one. Approximate 80% empirical coverage is the
calibration expectation; the interval width is reported beside coverage.
No confidence value is invented when the data is too sparse.

Metrics are emitted for every zone/mode/target slice. A single whole-house
average is never the only acceptance result, because it could hide Floor 2's
behavior or make cooling appear well-supported when it is not.

### Future recommendation metric

False-recommendation rate is a downstream metric, not a v1 predictor score.
Once a bounded what-if recommendation and an eligible follow-up experiment
exist, a recommendation is false when its validated outcome violates a
declared comfort/safety constraint or fails the claimed directional effect.
The denominator includes only recommendations with an eligible follow-up;
recommendations correctly abstained because evidence is insufficient are not
false recommendations.

Until that layer exists, false-recommendation rate is not applicable rather
than zero.

## Cross-zone and future thermal-model boundary

The v1 feature schema already permits point-in-time concurrent-zone state.
That lets the first session-level model account for what other zones were
calling at the prediction boundary. It does not, by itself, establish that a
setpoint change caused a later temperature response.

The future thermal model needs additional trajectory and intervention evidence:

- all-floor temperature state over time;
- active zones, setpoints, and mode transitions;
- routine versus scheduled versus deliberate-intervention provenance;
- experiment boundaries and starting conditions;
- outdoor conditions and other available environmental covariates;
- outcomes for every floor, including lagged temperature response and runtime.

Its future evaluation should score trajectory error and cross-floor effect
error on held-out experiments. A what-if query can then change an input,
simulate the resulting trajectory, and show the predicted effect and
uncertainty. The query remains read-only and must abstain outside the
observed evidence.

## Non-goals

This contract does not:

- train or deploy a production model;
- define a thermostat-control policy;
- optimize utility cost or claim kilowatt-hour savings without power data;
- treat per-zone demand runtime as shared-equipment runtime;
- make the LLM responsible for numeric HVAC prediction;
- claim that the future cross-zone recommendation layer is already available.

The next implementation task is the normalized dataset/export pipeline. It
must preserve this evaluation boundary, the mode-aware target semantics, and
the future experiment metadata needed for cross-zone thermal identification.
