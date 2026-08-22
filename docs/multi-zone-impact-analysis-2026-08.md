# HomeOps multi-zone call impact analysis — August 2026

Snapshot of the Raspberry Pi derived-event log read on 2026-08-22. The source
log is runtime data and is not committed. The analysis was produced with
[`scripts/analyze_multi_zone_impact.py`](../scripts/analyze_multi_zone_impact.py),
which is read-only.

## Method

The analysis uses `homeops.consumer.zone_time_to_temp.v1` events. Each event
records the zone's time from `hvac_action="heating"` to setpoint reached and
the other floor-call entities that were active at session start. Records are
grouped by the target zone and the exact set of other zones calling. A
contended-versus-uncontended comparison requires at least five records in both
groups for the same zone.

Command used:

```bash
ssh bob@raspberrypi 'cat /home/leachd/repos/homeops/state/consumer/events.jsonl' \
  | python3 scripts/analyze_multi_zone_impact.py --log -
```

## Result

| Metric | Result |
|---|---:|
| Valid JSON objects in source log | 28,253 |
| `zone_time_to_temp.v1` records | 1 |
| Unique days represented | 1 (`2026-04-03`) |
| Zones represented | floor 1 |
| Contended records | 0 |
| Uncontended records | 1 |
| Minimum records per comparison group | 5 |

| Zone | Other zones at session start | Samples | Median duration (s) | Mean °F/min | Outdoor samples |
|---|---|---:|---:|---:|---:|
| floor_1 | none | 1 | 687.0 | 0.262 | 1 |

The corresponding per-zone comparison is:

| Zone | Uncontended samples | Contended samples | Median delta | Result |
|---|---:|---:|---:|---|
| floor_1 | 1 | 0 | — | `insufficient_data` |

The wider log contains 5,780 thermostat temperature updates but only one
setpoint-reached and one time-to-temperature event. It also contains 138
setpoint-miss events. Those events do not supply the completed time-to-temp
measure required for this comparison.

## Conclusion

The data does not support a conclusion about whether simultaneous calls slow a
zone's heating. In particular, zone 2 has no completed time-to-temperature
sample in this history, and there are no contended samples for any zone. No HA
automation, thermostat setting, scheduling threshold, or production behavior
was changed.

The next evidence needed is at least five uncontended and five contended
completed heating sessions for the same zone, with outdoor temperature and
session-start contention retained. Until then, a scheduling optimizer would be
unsupported.
