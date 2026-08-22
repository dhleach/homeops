# HomeOps hourly zone-call frequency — August 2026 review

This is a read-only report from the Pi's derived event log using
[`scripts/floor_hourly_heatmap.py`](../scripts/floor_hourly_heatmap.py). It
counts `homeops.consumer.floor_call_started.v1` events by local hour; it does
not change consumer state, thermostat settings, or Home Assistant automations.

## Command

The current Pi log's matching `floor_call_started.v1` history runs from
2026-02-23 through 2026-05-17. The latest seven-calendar-day window available
in that history is 2026-05-11 through 2026-05-17, interpreted in
`America/New_York`:

```bash
ssh bob@raspberrypi 'cat /home/leachd/repos/homeops/state/consumer/events.jsonl' \
  | python3 scripts/floor_hourly_heatmap.py --log - \
      --start 2026-05-11 --end 2026-05-17
```

## Result

```text
HomeOps hourly zone-call frequency
Range: 2026-05-11 → 2026-05-17 (7 inclusive days)
Timezone: America/New_York

Floor      | 00 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 | Total | Peak hours
-----------------------------------------------------------------------------------------------------------
floor_1    | 04 07 06 04 08 08 05 04 01 02 02 01 02 01 01 01 00 02 01 03 05 03 06 05 |    82 | 04, 05
floor_2    | 02 01 00 02 02 03 03 02 02 05 03 01 02 01 01 01 02 01 02 00 01 01 00 02 |    40 | 09
floor_3    | 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 |     0 | —

Included calls: 122 of 1343 valid matching payloads; 1221 outside range
Data quality: 28256 valid JSON objects; 0 invalid JSON lines; 0 non-object lines; 0 invalid call payloads
Read-only report: no consumer state, thermostat setting, or HA automation is changed.
```

## Interpretation and limits

- Floor 1 had 82 calls in the window, with equal peaks at local hours 04 and
  05. Floor 2 had 40 calls, peaking at hour 09.
- No floor-3 calls occurred in this seven-day slice. That is a property of the
  available log window, not evidence that floor 3 is permanently inactive.
- The report describes call-start frequency only. It does not measure call
  duration, time-to-temperature, simultaneous calls, or causal scheduling
  effects. The separate multi-zone analysis remains the gate for any
  scheduling conclusion.
- The log contains no malformed JSON, non-object records, or invalid matching
  payloads. The 1,221 valid matching calls outside the selected range are
  retained in the input-quality accounting and excluded from the table.

The report schema (`homeops.floor-hourly-heatmap.v1`) is an offline analysis
format, not a new consumer event schema.
