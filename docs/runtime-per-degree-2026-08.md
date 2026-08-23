# Furnace runtime per degree — Pi history snapshot

Generated on 2026-08-23 from the Pi-derived event history with
`scripts/runtime_per_degree.py`:

```bash
python3 scripts/runtime_per_degree.py \
  --log state/consumer/events.jsonl \
  --start 2026-03-20 \
  --end 2026-05-31 \
  --format json \
  --out reports/runtime-per-degree.json
```

The report found **1,258 observed call ends**, **1,255 calls with measured
duration**, and **43 eligible runtime-per-degree observations**. The ratio is
furnace on-time seconds divided by the positive zone temperature rise in °F.
Furnace on-time is attributed to every overlapping zone call, so these values
must not be summed across zones.

## Zone and outdoor-temperature buckets

| Zone | Outdoor bucket | Observations | Median seconds/°F | P25–P75 seconds/°F | Status |
|---|---|---:|---:|---:|---|
| floor_1 | [30, 40)°F | 14 | 9,153.7 | 8,960.4–10,696.8 | ok |
| floor_1 | [40, 50)°F | 14 | 8,378.8 | 6,081.5–9,559.9 | ok |
| floor_1 | [50, 60)°F | 9 | 12,452.0 | 7,424.0–13,184.5 | ok |
| floor_1 | [80, 90)°F | 1 | 2,475.2 | 2,475.2–2,475.2 | insufficient_data |
| floor_2 | — | 0 | — | — | insufficient_data |
| floor_3 | [30, 40)°F | 4 | 37,018.3 | 21,093.9–53,869.3 | ok |
| floor_3 | [40, 50)°F | 1 | 80,337.7 | 80,337.7–80,337.7 | insufficient_data |

## Telemetry quality

- 3 calls were incomplete because their recorded duration was unavailable.
- 148 completed calls had no usable thermostat boundary reading.
- 593 completed calls had boundary readings older than the 30-minute quality window.
- 471 had a flat or falling measured temperature delta and were excluded.
- No completed call lacked overlapping furnace runtime or outdoor temperature
  after the report's furnace-session fallback was applied.

The floor-2 result is explicitly insufficient history, not a claim that the
zone is efficient or that its calls had no heating effect. The artifact is a
historical, read-only measurement and does not emit a production event, change
thermostat settings, or modify Pi state.
