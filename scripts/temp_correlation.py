#!/usr/bin/env python3
"""
Analyze correlation between outdoor temperature and floor heating runtime.

Reads floor_daily_summary.v1 events and computes Pearson correlation coefficient
between outdoor_temp_avg_f and total_runtime_s for each floor. Answers: does floor
heating runtime depend on outdoor temperature?

Usage (last 30 days, all floors):
    python3 scripts/temp_correlation.py

Usage (last 60 days):
    python3 scripts/temp_correlation.py --days 60

Usage (single floor):
    python3 scripts/temp_correlation.py --floor floor_2

Usage with custom log:
    DERIVED_EVENT_LOG=/path/to/events.jsonl python3 scripts/temp_correlation.py

Arguments:
    --days    Number of most recent days to include (default: 30)
    --floor   Optional: analyze one floor only (floor_1 | floor_2 | floor_3)
    --log     Optional: path to derived event JSONL
              (overrides DERIVED_EVENT_LOG env var)
"""

from __future__ import annotations

import argparse
import json
import math
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


def _pearson_r(x_vals: list[float], y_vals: list[float]) -> float | None:
    """
    Compute Pearson correlation coefficient between two lists of numbers.

    Returns a value in [-1, 1], or None if insufficient data or division error.
    """
    if len(x_vals) < 2 or len(y_vals) < 2 or len(x_vals) != len(y_vals):
        return None

    n = len(x_vals)
    mean_x = sum(x_vals) / n
    mean_y = sum(y_vals) / n

    numerator = sum((x_vals[i] - mean_x) * (y_vals[i] - mean_y) for i in range(n))
    var_x = sum((x - mean_x) ** 2 for x in x_vals)
    var_y = sum((y - mean_y) ** 2 for y in y_vals)

    if var_x == 0 or var_y == 0:
        return None

    denominator = math.sqrt(var_x * var_y)
    if denominator == 0:
        return None

    return numerator / denominator


def _load_floor_summaries(
    log_path: str,
    days: int,
    floor_filter: str | None = None,
) -> dict[str, dict[str, dict]]:
    """
    Load floor_daily_summary.v1 events for the most recent ``days`` days.

    Returns a nested dict: {date_str: {floor: data_dict}}.
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
                rows[date_str][floor] = data
    except FileNotFoundError:
        print(f"Error: log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error reading log: {e}", file=sys.stderr)
        sys.exit(1)

    return dict(rows)


def _all_dates(days: int) -> list[str]:
    """Return date strings for the most recent ``days`` days, newest first."""
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def _print_single_floor_correlation(
    rows: dict[str, dict[str, dict]],
    floor: str,
    days: int,
    file=sys.stdout,
) -> None:
    """Print correlation analysis for a single floor."""
    floor_label = floor.replace("_", " ").title()
    col_w = 11

    header = f"{'Date':<12}{'Outdoor':>{col_w}}{'Runtime':>{col_w}}"
    sep = "-" * (12 + col_w * 2)

    print(f"{floor_label} — Temperature Correlation Analysis", file=file)
    print(header, file=file)
    print(sep, file=file)

    temps: list[float] = []
    runtimes: list[float] = []
    shown = 0

    for date_str in _all_dates(days):
        day_data = rows.get(date_str, {})
        data = day_data.get(floor, {})
        if not data and shown == 0:
            continue  # skip leading empty days
        temp = data.get("outdoor_temp_avg_f")
        runtime_s = data.get("total_runtime_s")

        if temp is not None and runtime_s is not None:
            temps.append(float(temp))
            runtimes.append(float(runtime_s))

        row = f"{date_str:<12}{_fmt_temp(temp):>{col_w}}{_fmt_duration(runtime_s):>{col_w}}"
        print(row, file=file)
        shown += 1

    if shown == 0:
        print(f"No data found for {floor_label} in this period.", file=file)
        return

    r = _pearson_r(temps, runtimes)
    print(sep, file=file)
    if r is not None:
        r_rounded = round(r, 3)
        interpretation = _interpret_correlation(r)
        print(
            f"Pearson r: {r_rounded:>6.3f} ({interpretation})",
            file=file,
        )
        print(
            f"Sample size: {len(temps)} days",
            file=file,
        )
    else:
        print("Insufficient data or zero variance for correlation.", file=file)


def _print_all_floors_correlation(
    rows: dict[str, dict[str, dict]],
    days: int,
    file=sys.stdout,
) -> None:
    """Print correlation summary for all floors."""
    col_w = 11
    header = (
        f"{'Date':<12}"
        f"{'Outdoor':>{col_w}}"
        f"{'Floor 1':>{col_w}}"
        f"{'Floor 2':>{col_w}}"
        f"{'Floor 3':>{col_w}}"
    )
    sep = "-" * (12 + col_w * 4)

    print("Temperature vs Floor Runtime Correlation", file=file)
    print(header, file=file)
    print(sep, file=file)

    # Collect temps and runtimes for each floor
    floor_data: dict[str, tuple[list[float], list[float]]] = {f: ([], []) for f in KNOWN_FLOORS}

    shown = 0
    for date_str in _all_dates(days):
        day_data = rows.get(date_str, {})
        outdoor = (
            day_data.get("floor_1", {}).get("outdoor_temp_avg_f")
            or day_data.get("floor_2", {}).get("outdoor_temp_avg_f")
            or day_data.get("floor_3", {}).get("outdoor_temp_avg_f")
        )

        if not any(day_data.get(f, {}) for f in KNOWN_FLOORS) and shown == 0:
            continue  # skip leading empty days

        f1_runtime = day_data.get("floor_1", {}).get("total_runtime_s")
        f2_runtime = day_data.get("floor_2", {}).get("total_runtime_s")
        f3_runtime = day_data.get("floor_3", {}).get("total_runtime_s")

        # Track temps and runtimes for correlation
        if outdoor is not None:
            if f1_runtime is not None:
                floor_data["floor_1"][0].append(float(outdoor))
                floor_data["floor_1"][1].append(float(f1_runtime))
            if f2_runtime is not None:
                floor_data["floor_2"][0].append(float(outdoor))
                floor_data["floor_2"][1].append(float(f2_runtime))
            if f3_runtime is not None:
                floor_data["floor_3"][0].append(float(outdoor))
                floor_data["floor_3"][1].append(float(f3_runtime))

        row = (
            f"{date_str:<12}"
            f"{_fmt_temp(outdoor):>{col_w}}"
            f"{_fmt_duration(f1_runtime):>{col_w}}"
            f"{_fmt_duration(f2_runtime):>{col_w}}"
            f"{_fmt_duration(f3_runtime):>{col_w}}"
        )
        print(row, file=file)
        shown += 1

    if shown == 0:
        print("No data found for this period.", file=file)
        return

    print(sep, file=file)
    print("\nCorrelation Summary (Pearson r):", file=file)
    for floor in KNOWN_FLOORS:
        temps, runtimes = floor_data[floor]
        r = _pearson_r(temps, runtimes)
        floor_label = floor.replace("_", " ").title()
        if r is not None:
            r_rounded = round(r, 3)
            interpretation = _interpret_correlation(r)
            print(f"  {floor_label:<10}: {r_rounded:>7.3f} ({interpretation})", file=file)
        else:
            print(f"  {floor_label:<10}: insufficient data", file=file)


def _interpret_correlation(r: float) -> str:
    """Interpret correlation strength."""
    abs_r = abs(r)
    if abs_r >= 0.7:
        strength = "strong"
    elif abs_r >= 0.4:
        strength = "moderate"
    elif abs_r >= 0.2:
        strength = "weak"
    else:
        strength = "negligible"

    direction = "positive" if r > 0 else "negative"
    return f"{strength} {direction}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze correlation between outdoor temp and floor heating runtime."
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
        help="Analyze one floor only (floor_1 | floor_2 | floor_3)",
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
        _print_single_floor_correlation(rows, args.floor, args.days)
    else:
        _print_all_floors_correlation(rows, args.days)


if __name__ == "__main__":
    main()
