# Zone time-to-temperature model — Pi history snapshot

Generated on 2026-08-23 from the Pi-derived consumer event history with
`scripts/time_to_temp.py`:

```bash
python3 scripts/time_to_temp.py \
  --log state/consumer/events.jsonl \
  --start 2026-03-20 \
  --end 2026-08-23
```

The history contained **1** completed `zone_time_to_temp.v1` event. That is
not enough to fit a per-zone outdoor-temperature model, so all three zone
statuses remain explicitly `insufficient_data` under the default minimum of
five observations.

## Zone models

| Zone | Observations | Outdoor slope | R² | Status |
|---|---:|---:|---:|---|
| floor_1 | 1 | — | — | insufficient_data |
| floor_2 | 0 | — | — | insufficient_data |
| floor_3 | 0 | — | — | insufficient_data |

The one eligible observation was for floor 1 in the `[80, 90)°F` bucket. The
event recorded a 3°F setpoint delta and 687 seconds to reach the setpoint;
this single warm-weather point cannot establish a trend or a prediction
baseline. The source log contained 28,537 lines and no malformed JSON,
duplicate completed events, missing measurements, or invalid measurements.

The report is a historical, read-only planning aid. It does not emit a
production event, change thermostat settings, or modify Pi state. A future
model should be re-evaluated after the consumer accumulates multiple completed
time-to-temperature events across outdoor conditions for each zone.
