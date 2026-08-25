# Multi-zone scheduling query

[`scripts/scheduling_query.py`](../scripts/scheduling_query.py) is a
provider-neutral, deterministic planning tool for the three-zone HVAC system.
It answers a narrow question:

> If floor 2 must reach a target temperature by a deadline, when would its
> heating call need to start, and what secondary-zone setpoint ceilings avoid a
> known concurrent call during that window?

The tool is intentionally read-only. It reads the derived event log and the
validated rules configuration. It does not call Home Assistant, write
thermostat state, emit a consumer event, send Telegram, or enable the staged
mitigation overlay.

## Request contract

The exported `TOOL_DEFINITION` describes the provider-neutral function shape.
The decoded arguments can be passed to
`recommend_multi_zone_schedule(arguments)`. Log path, history range, rules
path, and safety tuning remain orchestration-owned keyword arguments.

| Field | Required | Meaning |
|---|---:|---|
| `target_temp_f` | yes | Desired floor-2 temperature, bounded to 0–120°F. |
| `outdoor_temp_f` | yes | Outdoor temperature for the time-to-temperature model, bounded to -100–150°F. |
| `deadline` | yes | ISO-8601 time by which floor 2 should reach the target. |
| `current_temp_f` | no | Explicit current floor-2 temperature. If omitted, the latest fresh snapshot at or before `as_of` is used. |
| `floor_1_current_temp_f` | no | Explicit current floor-1 temperature; otherwise inferred from a fresh snapshot. |
| `floor_3_current_temp_f` | no | Explicit current floor-3 temperature; otherwise inferred from a fresh snapshot. |
| `as_of` | no | Observation time used for snapshot freshness and history cutoff. Explicit values make replays deterministic. |

The CLI uses `--target`, `--current`, `--outdoor`, `--by`, `--floor-1-current`,
`--floor-3-current`, and `--as-of` for these fields. Naive timestamps are
normalized to UTC; timezone-aware timestamps are returned normalized to UTC.
The deadline must be after `as_of` and no more than 48 hours later.

## Model composition and safety math

1. The floor-2 rise is `target_temp_f - current_temp_f`.
2. `scripts/time_to_temp.py` supplies the historical floor-2 duration estimate
   for that rise at the requested outdoor temperature.
3. The configured `rules.floor_2_long_call.threshold_minutes` is loaded through
   the same validated rules loader used by the consumer. The checked-in value
   is 45 minutes. A five-minute reserve is subtracted by default, so the
   default maximum recommended continuous call is 40 minutes.
4. The candidate floor-2 start is `deadline - predicted_duration`.
5. For each secondary zone, the p75 observed cooling rate is preferred over its
   median. The projected temperature at the deadline is:

   ```text
   current_temperature - conservative_loss_rate × call_window_minutes
   ```

   The candidate setpoint ceiling is that projection minus a 0.5°F planning
   margin, rounded down to the nearest 0.5°F. The recommendation also says not
   to allow the secondary zone to call during the primary window.

The result is `ready` only when the primary model is in-range and positive, the
call fits below the threshold reserve, the deadline leaves time to start, both
secondary zones have fresh temperatures, and both have qualifying heat-loss
rates. No number is presented as an “optimal” recommendation when these
conditions are not met.

## Response and failure states

The JSON response includes:

- `answerability.status`: `ready`, `insufficient_data`, or
  `unsafe_to_recommend`;
- `recommendation`: the schedule and secondary-zone ceilings only when status
  is `ready`;
- `analysis`: the primary prediction, candidate start, heat-loss projections,
  and safety threshold details, including missingness;
- `model_outputs`: the compact outputs from the existing thermal query layer;
- `source_event_evidence` and `data_quality`: bounded evidence and malformed,
  duplicate, invalid, and stale-input accounting;
- `limitations`: explicit boundaries for downstream callers.

`insufficient_data` means the required evidence is absent or too sparse. The
tool uses `unsafe_to_recommend` when a safety boundary is disabled/unavailable,
the primary model is extrapolated or invalid, the deadline is too soon, or the
predicted duration reaches the configured reserve. Both states have
`recommendation: null`.

## Example output shape

```json
{
  "schema": "homeops.multi_zone_schedule.v1",
  "answerability": {
    "status": "ready",
    "can_recommend": true,
    "reasons": []
  },
  "recommendation": {
    "primary_zone": "floor_2",
    "predicted_duration_min": 20.0,
    "recommended_start": "2026-01-02T06:40:00+00:00",
    "deadline": "2026-01-02T07:00:00+00:00",
    "secondary_zones": {
      "floor_1": {"candidate_max_setpoint_f": 68.0},
      "floor_3": {"candidate_max_setpoint_f": 67.0}
    }
  }
}
```

The example uses synthetic, sufficiently populated history. The current Pi
history has only one eligible completed time-to-temperature event, so a real
floor-2 schedule remains `insufficient_data` until more qualifying history is
available. This is deliberate: a schedule that looks precise but is based on
one observation would be worse than no schedule.

The report schema in this document is offline tooling metadata; no new
`homeops.consumer.*` event schema is added.
