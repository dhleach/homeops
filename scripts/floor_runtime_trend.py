#!/usr/bin/env python3
"""
Day-by-day floor runtime trend table from floor_daily_summary.v1 events.

Reads floor_daily_summary.v1 events from the derived event log (JSONL) and
outputs a trend table showing per-floor heating runtime over recent days.

Usage (all floors, last 30 days):
    python3 scripts/floor_runtime_trend.py

Usage (single floor with call detail, last 14 days):
    python3 scripts/floor_runtime_trend.py --floor floor_2 --days 14

Usage with custom log:
    DERIVED_EVENT_LOG=/path/to/events.jsonl python3 scripts/floor_runtime_trend.py

Arguments:
    --days    Number of most recent days to include (default: 30)
    --floor   Optional: filter to one floor for detailed view
              (floor_1 | floor_2 | floor_3)
    --log     Optional: path to derived event JSONL
              (overrides DERIVED_EVENT_LOG env var)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA = "homeops.consumer.floor_daily_summary.v1"
KNOWN_FLOORS = ("floor_1", "floor_2", "floor_3")
DEFAULT_DAYS = 30
DEFAULT_LOG = "state/consumer/events.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: int | None) -> str:
    """Format seconds as 'Xh Ym'; return '—' for None or zero."""
    if seconds is None:
        return "—"
    if seconds == 0:
        return "0m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _fmt_temp(temp_f: float | None) -> str:
    """Format outdoor temp as 'NN°F'; return '—' for None."""
    if temp_f is None:
        return "—"
    return f"{round(temp_f)}°F"


def _load_floor_summaries(
    log_path: str,
    days: int,
    floor_filter: str | None = None,
) -> dict[str, dict[str, dict]]:
    """
    Read floor_daily_summary.v1 events from log_path for the most recent ``days`` days.

    Returns a nested dict: ``{date_str: {floor: data_dict}}``.
    Only includes dates within [today - days + 1, today].
    If ``floor_filter`` is set, only that floor's data is included.
    """
    today = date.today()
    cutoff = today - timedelta(days=days - 1)

    rows: dict[str, dict[str, dict]] = defaultdict(dict)

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
                date_str = data.get("date", "")
                floor = data.get("floor", "")
                try:
                    evt_date = date.fromisoformat(date_str)
                except ValueError:
                    continue
                if evt_date < cutoff or evt_date > today:
                    continue
                if floor_filter and floor != floor_filter:
                    continue
                # Last write wins (handles duplicate events)
                rows[date_str][floor] = data
    except FileNotFoundError:
        print(f"Error: log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error reading log: {e}", file=sys.stderr)
        sys.exit(1)

    return dict(rows)


def _all_dates(days: int) -> list[str]:
    """Return a list of date strings for the most recent ``days`` days, newest first."""
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def _print_all_floors_table(
    rows: dict[str, dict[str, dict]],
    days: int,
    file=sys.stdout,
) -> None:
    """Print a table showing all 3 floors side-by-side."""
    col_w = 12
    date_w = 12

    header = (
        f"{'Date':<{date_w}}"
        f"{'Floor 1':>{col_w}}"
        f"{'Floor 2':>{col_w}}"
        f"{'Floor 3':>{col_w}}"
        f"{'Outdoor':>{col_w}}"
    )
    sep = "-" * (date_w + col_w * 4)
    print(header, file=file)
    print(sep, file=file)

    shown = 0
    for date_str in _all_dates(days):
        day_data = rows.get(date_str, {})
        if not day_data and shown == 0:
            continue  # skip leading empty days
        f1 = day_data.get("floor_1", {})
        f2 = day_data.get("floor_2", {})
        f3 = day_data.get("floor_3", {})
        outdoor = (
            f1.get("outdoor_temp_avg_f")
            or f2.get("outdoor_temp_avg_f")
            or f3.get("outdoor_temp_avg_f")
        )
        row = (
            f"{date_str:<{date_w}}"
            f"{_fmt_duration(f1.get('total_runtime_s')):>{col_w}}"
            f"{_fmt_duration(f2.get('total_runtime_s')):>{col_w}}"
            f"{_fmt_duration(f3.get('total_runtime_s')):>{col_w}}"
            f"{_fmt_temp(outdoor):>{col_w}}"
        )
        print(row, file=file)
        shown += 1

    if shown == 0:
        print("No data found for this period.", file=file)


def _print_single_floor_table(
    rows: dict[str, dict[str, dict]],
    floor: str,
    days: int,
    file=sys.stdout,
) -> None:
    """Print a detailed table for a single floor."""
    floor_label = floor.replace("_", " ").title()
    col_w = 11

    header = (
        f"{'Date':<12}"
        f"{'Runtime':>{col_w}}"
        f"{'Calls':>{7}}"
        f"{'Avg Call':>{col_w}}"
        f"{'Max Call':>{col_w}}"
        f"{'Outdoor':>{col_w}}"
    )
    sep = "-" * (12 + 7 + col_w * 3)
    print(f"{floor_label} — {days}-day trend", file=file)
    print(header, file=file)
    print(sep, file=file)

    shown = 0
    for date_str in _all_dates(days):
        day_data = rows.get(date_str, {})
        data = day_data.get(floor, {})
        if not data and shown == 0:
            continue  # skip leading empty days
        total_s = data.get("total_runtime_s")
        calls = data.get("total_calls")
        avg_s_raw = data.get("avg_duration_s")
        avg_s = int(avg_s_raw) if avg_s_raw is not None else None
        max_s = data.get("max_duration_s")
        outdoor = data.get("outdoor_temp_avg_f")

        calls_str = str(calls) if calls is not None else "—"
        row = (
            f"{date_str:<12}"
            f"{_fmt_duration(total_s):>{col_w}}"
            f"{calls_str:>{7}}"
            f"{_fmt_duration(avg_s):>{col_w}}"
            f"{_fmt_duration(max_s):>{col_w}}"
            f"{_fmt_temp(outdoor):>{col_w}}"
        )
        print(row, file=file)
        shown += 1

    if shown == 0:
        print(f"No data found for {floor_label} in this period.", file=file)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show per-floor heating runtime trend from floor_daily_summary.v1 events."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Number of most recent days to include (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--floor",
        choices=list(KNOWN_FLOORS),
        default=None,
        help="Filter to one floor for detailed view (floor_1 | floor_2 | floor_3)",
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

    rows = _load_floor_summaries(log_path, args.days, floor_filter=args.floor)

    if args.floor:
        _print_single_floor_table(rows, args.floor, args.days)
    else:
        _print_all_floors_table(rows, args.days)


if __name__ == "__main__":
    main()
