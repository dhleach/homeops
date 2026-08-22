# HomeOps anomaly detector validation — August 2026

Snapshot of the Raspberry Pi derived-event log read on 2026-08-22. The source
log is runtime data and is not committed. The report was produced with
[`scripts/validate_anomalies.py`](../scripts/validate_anomalies.py), which
replays the production detector classes without writing to the Pi.

## Method

The replay preserves JSONL order, matching the consumer's event-processing
order. It evaluates:

- `FloorRuntimeAnomalyRule`: the prior 14 summary events, a 1.5× baseline-mean
  threshold, at least 3 history points, and a 300-second minimum baseline mean.
- `FurnaceSessionAnomalyRule`: a short-session warning below 90 seconds and the
  documented absolute long-session fallback thresholds. No
  `baseline_constants.json` exists on the Pi, and the live furnace-session
  events have no floor value, so the effective long-session fallback is 2,700
  seconds.

The replay compares each finding with the corresponding event already present
in the log. A finding is considered contract-valid when it satisfies the rule
that generated it; determining whether it represents a mechanical fault needs
an operator, maintenance, or limit-switch label that is not present in this
telemetry.

Command used:

```bash
ssh bob@raspberrypi 'cat /home/leachd/repos/homeops/state/consumer/events.jsonl' \
  | PYTHONPATH=services/insights python3 scripts/validate_anomalies.py --log -
```

## Coverage and parity

| Dataset | Coverage | Complete records | Replay | Already emitted | Post-detector parity |
|---|---:|---:|---:|---:|---:|
| Daily summaries | 2026-03-20 → 2026-08-21, 151 unique days | 153 summary events | 27 floor alerts | 23 | 23 / 23 |
| Furnace sessions | 2026-02-23 → 2026-05-17, 44 days | 1,040 complete; 12 null-duration skipped | 8 warnings | 7 | 7 / 7 |

The full replay intentionally includes data from before the detectors were
deployed. The four floor replay-only findings are 2026-03-28 and 2026-03-29;
the floor detector merged in PR [#89](https://github.com/dhleach/homeops/pull/89)
on 2026-03-30. The one session replay-only finding is 2026-02-23; the furnace
session detector merged in PR
[#85](https://github.com/dhleach/homeops/pull/85) on 2026-03-27. Excluding
those pre-detector records gives exact replay/emission parity.

The input contained 28,246 valid JSON objects, with zero invalid JSON lines and
zero non-object lines.

## Floor runtime findings

Every row below met the detector contract. Sixteen of the 23 post-detector
findings occurred with an outdoor average at or below 50°F; seven occurred in
milder conditions. The findings are deviations from each floor's recent
runtime baseline, not proof of a furnace fault.

| Date | Floor | Runtime (s) | Baseline (s) | Threshold (s) | Outdoor (°F) |
|---|---|---:|---:|---:|---:|
| 2026-03-28 | floor_1 | 25,642 | 416.1 | 624.2 | 35.7 |
| 2026-03-28 | floor_2 | 16,954 | 641.6 | 962.4 | 35.7 |
| 2026-03-29 | floor_1 | 11,318 | 3,569.4 | 5,354.1 | 46.4 |
| 2026-03-29 | floor_2 | 20,919 | 2,680.6 | 4,020.9 | 46.4 |
| 2026-03-30 | floor_2 | 7,308 | 4,707.1 | 7,060.7 | 60.6 |
| 2026-04-06 | floor_1 | 12,921 | 3,469.6 | 5,204.5 | 47.1 |
| 2026-04-06 | floor_2 | 8,405 | 3,421.1 | 5,131.6 | 47.1 |
| 2026-04-07 | floor_1 | 14,794 | 4,392.6 | 6,588.9 | 40.3 |
| 2026-04-07 | floor_2 | 15,215 | 4,021.4 | 6,032.1 | 40.3 |
| 2026-04-08 | floor_1 | 13,238 | 5,449.3 | 8,173.9 | 42.2 |
| 2026-04-08 | floor_2 | 21,193 | 5,108.2 | 7,662.3 | 42.2 |
| 2026-04-08 | floor_3 | 3,034 | 346.6 | 520.0 | 42.2 |
| 2026-04-09 | floor_2 | 11,902 | 6,622.0 | 9,933.0 | 61.2 |
| 2026-04-20 | floor_1 | 14,111 | 4,811.6 | 7,217.5 | 42.5 |
| 2026-04-21 | floor_1 | 10,399 | 4,896.6 | 7,345.0 | 47.6 |
| 2026-04-21 | floor_2 | 15,759 | 4,480.1 | 6,720.2 | 47.6 |
| 2026-04-26 | floor_1 | 5,330 | 2,691.5 | 4,037.2 | 56.4 |
| 2026-04-27 | floor_1 | 4,731 | 2,646.1 | 3,969.1 | 59.5 |
| 2026-04-30 | floor_1 | 8,142 | 3,143.9 | 4,715.9 | 52.7 |
| 2026-05-01 | floor_1 | 6,342 | 3,725.5 | 5,588.2 | 49.6 |
| 2026-05-01 | floor_2 | 7,583 | 2,044.1 | 3,066.1 | 49.6 |
| 2026-05-02 | floor_1 | 12,341 | 4,178.5 | 6,267.8 | 45.5 |
| 2026-05-02 | floor_2 | 12,805 | 2,585.7 | 3,878.6 | 45.5 |
| 2026-05-03 | floor_1 | 7,966 | 5,060.0 | 7,590.0 | 46.9 |
| 2026-05-03 | floor_2 | 14,982 | 3,500.4 | 5,250.5 | 46.9 |
| 2026-05-04 | floor_2 | 7,378 | 4,550.6 | 6,825.9 | 58.7 |
| 2026-05-14 | floor_1 | 9,127 | 4,203.6 | 6,305.4 | 53.6 |

Post-detector alert counts were floor 1: 12, floor 2: 10, and floor 3: 1.

## Furnace session findings

All eight replayed findings met the `< 90s` short-session contract. There were
no long-session findings. The seven post-detector warnings were all emitted in
restart context (`across_restart: true`), so their root cause cannot be
classified from the event stream alone.

| Warning | Session timestamp | Duration (s) | Restart context |
|---|---|---:|---:|
| short | 2026-02-23T04:33:14.225843+00:00 | 36 | false |
| short | 2026-04-02T02:26:21.632535+00:00 | 59 | true |
| short | 2026-04-03T22:32:36.505139+00:00 | 21 | true |
| short | 2026-04-11T20:56:41.665388+00:00 | 60 | true |
| short | 2026-05-01T13:52:23.997020+00:00 | 39 | true |
| short | 2026-05-12T23:24:00.621606+00:00 | 79 | true |
| short | 2026-05-16T00:29:20.997923+00:00 | 79 | true |
| short | 2026-05-17T09:30:00.814414+00:00 | 39 | true |

## Decision

No threshold change is recommended. The replay found zero detector-contract
violations, and the post-detector replay matches production output exactly.
The available telemetry cannot produce a defensible fault-level false-positive
rate, so claiming “below 10%” would be fabricated precision. Keep the current
1.5× floor-runtime and 90-second session thresholds, and add an operator or
maintenance outcome label to future alerts before tuning them.
