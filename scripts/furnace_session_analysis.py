#!/usr/bin/env python3
"""
Correlate furnace session length with outdoor temperature.

Reads heating_session_ended.v1 events from the derived event log and produces
a CSV with one row per session: started_at, ended_at, duration_s, outdoor_temp_f.
Computes Pearson correlation between duration_s and outdoor_temp_f for sessions
where outdoor temperature data is available.

Usage (all sessions):
    python3 scripts/furnace_session_analysis.py

Usage with CSV output:
    python3 scripts/furnace_session_analysis.py --out state/furnace_session_correlation.csv

Usage with custom log:
    DERIVED_EVENT_LOG=/path/to/events.jsonl python3 scripts/furnace_session_analysis.py

Arguments:
    --days    Number of most recent days to include (default: all)
    --out     Optional: path to write CSV output file
    --log     Optional: path to derived event JSONL
              (overrides DERIVED_EVENT_LOG env var)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA = "homeops.consumer.heating_session_ended.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_OUT = "state/furnace_session_correlation.csv"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pearson_r(x_vals: list[float], y_vals: list[float]) -> float | None:
    """Compute Pearson r; returns None if insufficient data or zero variance."""
    n = len(x_vals)
    if n < 2 or len(y_vals) != n:
        return None
    mean_x = sum(x_vals) / n
    mean_y = sum(y_vals) / n
    numerator = sum((x_vals[i] - mean_x) * (y_vals[i] - mean_y) for i in range(n))
    var_x = sum((x - mean_x) ** 2 for x in x_vals)
    var_y = sum((y - mean_y) ** 2 for y in y_vals)
    if var_x == 0 or var_y == 0:
        return None
    denom = math.sqrt(var_x * var_y)
    return numerator / denom if denom else None


def _interpret_r(r: float) -> str:
    abs_r = abs(r)
    strength = (
        "strong"
        if abs_r >= 0.7
        else "moderate"
        if abs_r >= 0.4
        else "weak"
        if abs_r >= 0.2
        else "negligible"
    )
    direction = "positive" if r > 0 else "negative"
    return f"{strength} {direction}"


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m" if h else f"{m}m {seconds % 60:02d}s"


def _load_sessions(
    log_path: str,
    cutoff_dt: datetime | None = None,
) -> list[dict]:
    """Load heating_session_ended.v1 events, optionally filtered by cutoff date."""
    sessions = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("schema") != SCHEMA:
                    continue
                data = evt.get("data", {})
                ended_at = data.get("ended_at", "")
                duration_s = data.get("duration_s")
                outdoor_temp_f = data.get("outdoor_temp_f")
                if not ended_at or duration_s is None:
                    continue
                # Compute started_at
                try:
                    ended_dt = datetime.fromisoformat(ended_at)
                    started_dt = ended_dt - timedelta(seconds=int(duration_s))
                    started_at = started_dt.isoformat()
                except (ValueError, TypeError):
                    started_at = ""
                if cutoff_dt and ended_dt < cutoff_dt:
                    continue
                sessions.append(
                    {
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "duration_s": int(duration_s),
                        "outdoor_temp_f": outdoor_temp_f,
                        "entity_id": data.get("entity_id", ""),
                    }
                )
    except FileNotFoundError:
        print(f"Error: log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error reading log: {e}", file=sys.stderr)
        sys.exit(1)
    return sessions


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _write_csv(sessions: list[dict], out_path: str) -> None:
    fields = ["started_at", "ended_at", "duration_s", "outdoor_temp_f", "entity_id"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sessions)
    print(f"CSV written → {out_path} ({len(sessions)} rows)")


def _print_summary(sessions: list[dict]) -> None:
    total = len(sessions)
    with_temp = [s for s in sessions if s["outdoor_temp_f"] is not None]
    without_temp = total - len(with_temp)

    if not sessions:
        print("No sessions found.")
        return

    dates = sorted({s["ended_at"][:10] for s in sessions})
    date_range = f"{dates[0]} → {dates[-1]}" if dates else "—"

    print("=" * 60)
    print("Furnace Session × Outdoor Temp Correlation")
    print("=" * 60)
    print(f"Total sessions:       {total}")
    print(f"With outdoor_temp_f:  {len(with_temp)} ({len(with_temp) / total * 100:.0f}%)")
    print(f"Without (null):       {without_temp}")
    print(f"Unique days covered:  {len(dates)}")
    print(f"Date range:           {date_range}")

    if not with_temp:
        print("\nNo sessions with outdoor_temp_f — cannot compute correlation.")
        return

    durations = [float(s["duration_s"]) for s in with_temp]
    temps = [float(s["outdoor_temp_f"]) for s in with_temp]  # type: ignore[arg-type]
    r = _pearson_r(temps, durations)

    avg_dur = sum(durations) / len(durations)
    avg_temp = sum(temps) / len(temps)

    print()
    print("Correlation (sessions with temp data):")
    print(f"  Sample size:  {len(with_temp)} sessions")
    print(f"  Avg duration: {_fmt_duration(int(avg_dur))}")
    print(f"  Avg outdoor:  {avg_temp:.1f}°F")
    if r is not None:
        print(f"  Pearson r:    {r:.3f} ({_interpret_r(r)})")
        print()
        if r < -0.2:
            print("  → Colder outdoor temps correlate with longer furnace sessions.")
        elif r > 0.2:
            print("  → Warmer outdoor temps correlate with longer furnace sessions.")
        else:
            print("  → No meaningful correlation detected.")
    else:
        print("  Pearson r:    insufficient data or zero variance")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Correlate furnace session length with outdoor temperature."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of most recent days to include (default: all)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=f"Path to write CSV output (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Path to derived event JSONL (overrides DERIVED_EVENT_LOG env var)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)

    cutoff_dt: datetime | None = None
    if args.days is not None:
        cutoff_dt = datetime.now(tz=UTC) - timedelta(days=args.days)

    sessions = _load_sessions(log_path, cutoff_dt=cutoff_dt)
    _print_summary(sessions)

    out_path = args.out or DEFAULT_OUT
    _write_csv(sessions, out_path)


if __name__ == "__main__":
    main()
