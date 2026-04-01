#!/usr/bin/env python3
"""
Query per-floor heating runtime from floor_daily_summary.v1 events.

Reads floor_daily_summary.v1 events from the derived event log (JSONL), filters
by date range, and outputs a summary table:

    Floor    | Days | Total Runtime | Avg Daily | Max Single Day

Usage:
    python3 scripts/query_floor_runtime.py --start 2026-01-01 --end 2026-01-31
    python3 scripts/query_floor_runtime.py --start 2026-01-01 --end 2026-01-31 --floor floor_2
    DERIVED_EVENT_LOG=/custom/path.jsonl python3 scripts/query_floor_runtime.py ...

Arguments:
    --start   Start date (YYYY-MM-DD, inclusive)
    --end     End date   (YYYY-MM-DD, inclusive)
    --floor   Optional: filter to a single floor (floor_1, floor_2, floor_3)
    --log     Optional: path to derived event JSONL (overrides DERIVED_EVENT_LOG env var)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KNOWN_FLOORS = ("floor_1", "floor_2", "floor_3")
SCHEMA = "homeops.consumer.floor_daily_summary.v1"


def _fmt_duration(seconds: int) -> str:
    """Format integer seconds as 'Xh Ym' (e.g. '2h 14m')."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _load_events(log_path: str, start: date, end: date, floor_filter: str | None) -> list[dict]:
    """
    Read floor_daily_summary.v1 events from log_path that fall within [start, end].
    Returns a list of event data dicts.
    """
    events: list[dict] = []
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
                evt_date_str = data.get("date", "")
                try:
                    evt_date = date.fromisoformat(evt_date_str)
                except ValueError:
                    continue
                if evt_date < start or evt_date > end:
                    continue
                if floor_filter and data.get("floor") != floor_filter:
                    continue
                events.append(data)
    except FileNotFoundError:
        print(f"Error: log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error reading log file: {e}", file=sys.stderr)
        sys.exit(1)
    return events


def _aggregate(events: list[dict]) -> dict[str, dict]:
    """
    Aggregate events by floor.
    Returns: {floor: {total_s, days, max_s}}
    """
    totals: dict[str, int] = defaultdict(int)
    day_counts: dict[str, set] = defaultdict(set)
    max_s: dict[str, int] = defaultdict(int)

    for data in events:
        floor = data.get("floor", "unknown")
        runtime_s = int(data.get("total_runtime_s", 0))
        date_str = data.get("date", "")
        totals[floor] += runtime_s
        day_counts[floor].add(date_str)
        if runtime_s > max_s[floor]:
            max_s[floor] = runtime_s

    result: dict[str, dict] = {}
    for floor in totals:
        days = len(day_counts[floor])
        total = totals[floor]
        result[floor] = {
            "total_s": total,
            "days": days,
            "avg_s": total // days if days > 0 else 0,
            "max_s": max_s[floor],
        }
    return result


def _print_table(
    aggregated: dict[str, dict],
    start: date,
    end: date,
    floor_filter: str | None,
) -> None:
    """Print the formatted summary table to stdout."""
    # Determine which floors to show
    floors_to_show: list[str]
    if floor_filter:
        floors_to_show = [floor_filter]
    else:
        # Show all known floors in order, even those with no data
        floors_to_show = list(KNOWN_FLOORS)

    # Header
    print()
    print(f"Floor runtime summary: {start} → {end}")
    if floor_filter:
        print(f"Filter: {floor_filter}")
    print()

    col_floor = 10
    col_days = 6
    col_total = 15
    col_avg = 15
    col_max = 16

    header = (
        f"{'Floor':<{col_floor}}  "
        f"{'Days':>{col_days}}  "
        f"{'Total Runtime':>{col_total}}  "
        f"{'Avg Daily':>{col_avg}}  "
        f"{'Max Single Day':>{col_max}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)

    any_data = False
    for floor in floors_to_show:
        if floor not in aggregated:
            print(
                f"{floor:<{col_floor}}  "
                f"{'—':>{col_days}}  "
                f"{'—':>{col_total}}  "
                f"{'—':>{col_avg}}  "
                f"{'—':>{col_max}}"
            )
            continue
        any_data = True
        d = aggregated[floor]
        print(
            f"{floor:<{col_floor}}  "
            f"{d['days']:>{col_days}}  "
            f"{_fmt_duration(d['total_s']):>{col_total}}  "
            f"{_fmt_duration(d['avg_s']):>{col_avg}}  "
            f"{_fmt_duration(d['max_s']):>{col_max}}"
        )

    print(sep)
    if not any_data:
        print()
        print("No floor_daily_summary.v1 events found in the specified date range.")
        print("Run the consumer to generate events, or check that the log path is correct.")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query per-floor heating runtime from floor_daily_summary.v1 events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--start",
        required=True,
        metavar="YYYY-MM-DD",
        help="Start date (inclusive)",
    )
    parser.add_argument(
        "--end",
        required=True,
        metavar="YYYY-MM-DD",
        help="End date (inclusive)",
    )
    parser.add_argument(
        "--floor",
        metavar="FLOOR",
        choices=KNOWN_FLOORS,
        help="Filter to a single floor: floor_1, floor_2, or floor_3",
    )
    parser.add_argument(
        "--log",
        metavar="PATH",
        help="Path to derived event JSONL (overrides DERIVED_EVENT_LOG env var)",
    )
    args = parser.parse_args()

    # Parse dates
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as e:
        print(f"Error: invalid date format — {e}", file=sys.stderr)
        sys.exit(1)

    if start > end:
        print(f"Error: --start ({args.start}) must be <= --end ({args.end})", file=sys.stderr)
        sys.exit(1)

    # Resolve log path
    log_path = args.log or os.environ.get("DERIVED_EVENT_LOG") or "state/consumer/events.jsonl"

    events = _load_events(log_path, start, end, args.floor)
    aggregated = _aggregate(events)
    _print_table(aggregated, start, end, args.floor)


if __name__ == "__main__":
    main()
