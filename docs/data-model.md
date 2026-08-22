# HomeOps floor-call data model

This document defines the normalized records used to reason about per-floor
heating calls. It complements the wire-level event definitions in
[`docs/event-schemas/consumer-events.md`](event-schemas/consumer-events.md):
the event schemas describe what the consumer emits, while this document
describes how callers pair and aggregate those events for a time window.

The model is intentionally read-only. It does not add a consumer event or
change the state persisted by the consumer.

## `FloorCallSession`

`FloorCallSession` represents one floor call observed from a
`floor_call_started.v1`/`floor_call_ended.v1` pair. Field names use the
normalized model vocabulary rather than the event names.

| Field | Type | Units / semantics |
|---|---|---|
| `floor_id` | string | Canonical floor key: `floor_1`, `floor_2`, or `floor_3`. |
| `session_start_ts` | ISO 8601 string \| null | UTC timestamp from `floor_call_started.v1.data.started_at`; `null` when the consumer observed the end after a restart or otherwise missed the start. |
| `session_end_ts` | ISO 8601 string | UTC timestamp from `floor_call_ended.v1.data.ended_at`. |
| `duration_s` | integer \| null | Elapsed call duration in seconds. `null` means the matching start was unavailable; it is never treated as a measured zero-second call. |

Example of a measured session:

```json
{
  "floor_id": "floor_2",
  "session_start_ts": "2026-01-15T06:12:03.100000+00:00",
  "session_end_ts": "2026-01-15T07:04:51.500000+00:00",
  "duration_s": 3168
}
```

When a consumer restart prevents duration reconstruction, retain the end
event as an observed call but preserve the missing measurement:

```json
{
  "floor_id": "floor_2",
  "session_start_ts": null,
  "session_end_ts": "2026-01-15T07:04:51.500000+00:00",
  "duration_s": null
}
```

An active call with no end event is not a completed `FloorCallSession`. It is
in-flight consumer state and must not be included in completed-call duration
statistics.

## `FloorStats`

`FloorStats` is the per-floor aggregate for a half-open UTC window
`[window_start, window_end)`. A daily summary may use UTC calendar-day
boundaries; an analysis report may convert timestamps to a display timezone
before selecting its window.

| Field | Type | Units / semantics |
|---|---|---|
| `floor_id` | string | Canonical floor key. |
| `window_start` | ISO 8601 string | Inclusive UTC start of the aggregate window. |
| `window_end` | ISO 8601 string | Exclusive UTC end of the aggregate window. |
| `call_count` | integer | Number of `floor_call_ended.v1` events observed for the floor in the window, including ended calls whose duration is unknown. |
| `total_runtime_s` | integer | Sum of non-null `duration_s` values in seconds. Unknown durations contribute zero to this field. |
| `avg_duration_s` | number \| null | `total_runtime_s / call_count`, rounded to one decimal place by the current daily-summary implementation; `null` when `call_count` is zero. If unknown-duration calls are present, interpret this as an observed-call average with incomplete measurement coverage. |
| `min_duration_s` | integer \| null | Minimum non-null measured duration in seconds; `null` when no measured duration exists. |
| `max_duration_s` | integer \| null | Maximum non-null measured duration in seconds; `null` when no measured duration exists. |

Example with three measured calls:

```json
{
  "floor_id": "floor_2",
  "window_start": "2026-01-15T00:00:00+00:00",
  "window_end": "2026-01-16T00:00:00+00:00",
  "call_count": 3,
  "total_runtime_s": 7200,
  "avg_duration_s": 2400.0,
  "min_duration_s": 1500,
  "max_duration_s": 2900
}
```

For a zero-call window, use `call_count: 0`, `total_runtime_s: 0`, and
`null` for `avg_duration_s`, `min_duration_s`, and `max_duration_s`. For a
window containing only ended calls with unknown duration, retain the
`call_count`, keep measured-duration fields unavailable, and do not invent a
duration.

## Pairing and aggregation rules

1. Normalize the floor from the event's `data.floor`; the canonical entity ID
   can be used as a fallback when importing older records.
2. Pair a start and end for the same floor in event order. The end event's
   `duration_s` is authoritative when it is non-null.
3. Assign a completed call to an aggregate window by its end timestamp. Use
   the half-open interval so adjacent windows cannot count the same end twice.
4. Count every observed end event in `call_count`, but sum, average, and
   min/max only have measured-duration support. Do not convert `null` to zero
   before computing a duration statistic.
5. A separate report may use the start timestamp for demand-frequency views,
   such as the hourly call heatmap. That is intentionally different from the
   completed-call runtime aggregate defined here.

The consumer's `reporting.py` materializes the daily-summary form of this
aggregate from `daily_state`; the inline reference there should be kept with
this document when either the state keys or summary semantics change.
