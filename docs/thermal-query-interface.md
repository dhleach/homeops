# Natural-language thermal query interface

[`scripts/thermal_query.py`](../scripts/thermal_query.py) builds a bounded,
provider-neutral context that an LLM caller can use to answer questions about
historical zone heating behavior. It is deliberately a composition layer over
the existing read-only analyses, not a new model or a Home Assistant control
path.

## Request contract

The exported `TOOL_DEFINITION` describes the LLM-callable shape. A caller can
pass the decoded argument object to `query_thermal_history(arguments)`; the
local log path and date range remain orchestration-owned keyword arguments:

| Field | Required | Meaning |
|---|---:|---|
| `question` | yes | Free-form homeowner question, up to 500 characters. |
| `zone` | yes | Primary zone: `floor_1`, `floor_2`, or `floor_3`. |
| `outdoor_temp_f` | yes | Outdoor temperature used for model lookup, bounded to -100–150°F. |
| `target_temp_f` | no | Desired temperature in °F. |
| `current_temp_f` | no | Current temperature in °F; pair with target to derive a positive rise. |
| `setpoint_delta_f` | no | Direct positive rise in °F, up to 80°F. |

The tool rejects blank or oversized questions, unknown zones, non-finite or
out-of-bound temperatures, non-positive rises, and conflicting target/current
versus direct-delta inputs. Historical date range, log path, and minimum sample
thresholds are local orchestration options rather than LLM-controlled fields.

## Response contract

The JSON response has these important sections:

- `request`: normalized query values and the inclusive UTC history range.
- `metadata`: the tool schema, source type, and the versioned schemas of each
  composed analysis.
- `answerability`: `ready`, `partial`, or `insufficient_data`, with grounded
  source count and reasons. The caller must not turn missing evidence into a
  confident claim.
- `model_outputs.time_to_temperature`: per-zone model metadata and an
  optional prediction. `extrapolated` is explicit when the query is outside
  the observed outdoor-temperature or setpoint-delta range.
- `model_outputs.heat_loss`: per-zone cooling-curve statistics and quality
  metadata.
- `model_outputs.runtime_per_degree`: per-zone demand-normalized furnace
  runtime statistics and quality metadata.
- `model_outputs.furnace_baseline`: descriptive completed-furnace-session
  duration statistics.
- `source_event_evidence`: a bounded, deterministic set of source events with
  only allowlisted telemetry fields. It never forwards arbitrary event-payload
  fields into the prompt context.
- `data_quality`: input and per-analysis counters, including malformed,
  duplicate, missing, and invalid records.
- `prompt_context`: bounded text suitable for inclusion in an LLM prompt.

## Data and safety boundary

The tool reads the derived consumer event log and writes no production state.
It does not call an LLM, emit a consumer event, change a thermostat, coordinate
zones, or make an “optimal” setpoint recommendation. Historical model values
are planning evidence only. The prompt context labels telemetry as data rather
than instructions and tells the downstream caller not to invent missing
values.

The public `POST /api/diagnostic` endpoint is a separate EC2 service and
currently supplies Gemini with a live Prometheus snapshot. This tool is not
automatically wired into that route because historical event access from the
Pi is a separate deployment and authorization boundary.

## Examples

```bash
# Qualitative historical question; sparse data remains visible.
python3 scripts/thermal_query.py \
  --question "Why did floor 2 take so long to heat last Tuesday?" \
  --zone floor_2 --outdoor 30 --days 90 \
  --log state/consumer/events.jsonl --format json

# A duration prediction with a reproducible range.
python3 scripts/thermal_query.py \
  --question "How long should this three-degree rise take?" \
  --zone floor_2 --outdoor 30 --delta 3 \
  --start 2026-03-20 --end 2026-08-25 \
  --log state/consumer/events.jsonl --format json
```

A sparse response is still a successful tool response, but it is not a license
to invent an answer:

```json
{
  "schema": "homeops.thermal_query_context.v1",
  "answerability": {
    "status": "insufficient_data",
    "can_answer_with_limitations": false,
    "reasons": ["time-to-temperature model: fewer than the configured minimum observations"]
  },
  "model_outputs": {
    "time_to_temperature": {"prediction": null},
    "heat_loss": {"zones": [{"zone": "floor_2", "status": "insufficient_data"}]},
    "runtime_per_degree": {"zones": [{"zone": "floor_2", "status": "insufficient_data"}]}
  },
  "source_event_evidence": {"events": []}
}
```
