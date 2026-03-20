#!/usr/bin/env python3
"""
Furnace session duration baseline analysis.

Reads heating_session_ended.v1 events from a JSONL file,
computes per-floor duration statistics, prints a human-readable
report, and writes baseline_constants.json to the same directory.
"""

import json
import statistics
import sys
from pathlib import Path

_SCHEMA = "homeops.consumer.heating_session_ended.v1"


def _percentile(sorted_data: list, p: float) -> float:
    """Compute p-th percentile (0–100) using linear interpolation."""
    n = len(sorted_data)
    if n == 1:
        return float(sorted_data[0])
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return float(sorted_data[-1])
    frac = idx - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


def compute_baseline(events: list[dict]) -> dict:
    """
    Compute per-floor duration statistics from heating_session_ended.v1 events.

    Groups events by data["floor"] if present, else by data["entity_id"].
    Skips events where duration_s is None.

    Returns a dict keyed by floor/entity identifier, each value being:
        {count, min, max, median, p75, p95}  — durations in seconds.
    """
    groups: dict[str, list[int]] = {}

    for evt in events:
        data = evt.get("data", {})
        duration_s = data.get("duration_s")
        if duration_s is None:
            continue
        key = data.get("floor") or data.get("entity_id") or "unknown"
        groups.setdefault(key, []).append(int(duration_s))

    result = {}
    for key, durations in groups.items():
        durations.sort()
        result[key] = {
            "count": len(durations),
            "min": durations[0],
            "max": durations[-1],
            "median": statistics.median(durations),
            "p75": _percentile(durations, 75),
            "p95": _percentile(durations, 95),
        }

    return result


def load_events_from_jsonl(path: str) -> list[dict]:
    """
    Read a JSONL file and return all heating_session_ended.v1 events.
    Skips blank lines and lines with invalid JSON.
    """
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("schema") == _SCHEMA:
                events.append(evt)
    return events


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path-to-events.jsonl>", file=sys.stderr)
        sys.exit(1)

    jsonl_path = sys.argv[1]
    events = load_events_from_jsonl(jsonl_path)

    if not events:
        print("No heating_session_ended.v1 events found.")
        sys.exit(0)

    baseline = compute_baseline(events)

    # Human-readable report
    print(f"Furnace session duration baseline ({len(events)} sessions)\n")
    for key, stats in sorted(baseline.items()):
        print(f"  {key}")
        print(f"    count  : {stats['count']}")
        print(f"    min    : {_format_duration(stats['min'])}")
        print(f"    median : {_format_duration(stats['median'])}")
        print(f"    p75    : {_format_duration(stats['p75'])}")
        print(f"    p95    : {_format_duration(stats['p95'])}")
        print(f"    max    : {_format_duration(stats['max'])}")
        print()

    # Write baseline_constants.json alongside the input file
    out_path = Path(jsonl_path).parent / "baseline_constants.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2)
        f.write("\n")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
