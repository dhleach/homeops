#!/usr/bin/env python3
"""Build daily outdoor-temperature/furnace-runtime data for scatter plots.

The report consumes the derived JSONL event log rather than changing the live
consumer. Outdoor readings are averaged by their source timestamp, while
completed furnace sessions are assigned to the UTC date of ``ended_at``. When
the log contains ``furnace_daily_summary.v1`` events, their runtime (including
zero-runtime days) is authoritative and raw session events provide the
fallback for dates without a daily summary.

Usage:
    python3 scripts/furnace_temp_scatter.py \
        --start 2026-03-20 --end 2026-08-21 \
        --log state/consumer/events.jsonl \
        --out state/furnace_temp_scatter.csv

Revision history:
  2026-08-22  Added deterministic daily CSV generation so the historical trend
              work has a reproducible whole-furnace temperature/runtime dataset,
              including explicit missing-data and duplicate-event handling.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

OUTDOOR_SCHEMA = "homeops.consumer.outdoor_temp_updated.v1"
FURNACE_SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"
FURNACE_SUMMARY_SCHEMA = "homeops.consumer.furnace_daily_summary.v1"
CSV_FIELDS = ("date", "avg_temp_f", "furnace_runtime_min")
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_OUT = "state/furnace_temp_scatter.csv"


@dataclass
class _DailyData:
    """Intermediate values collected for one UTC calendar date."""

    outdoor_temps_f: list[float] = field(default_factory=list)
    raw_runtime_s: float = 0.0
    raw_runtime_seen: bool = False
    summary_runtime_s: float | None = None
    summary_temp_f: float | None = None


@dataclass(frozen=True)
class DailyScatterPoint:
    """One CSV row representing a UTC calendar day."""

    date: str
    avg_temp_f: float | None
    furnace_runtime_min: float | None


def _finite_number(value: Any) -> float | None:
    """Return finite numeric JSON values, excluding booleans and strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _utc_date(value: Any) -> date | None:
    """Parse an ISO timestamp and return its UTC calendar date."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).date()


def _summary_date(value: Any) -> date | None:
    """Parse a furnace-summary ``YYYY-MM-DD`` value."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _event_key(event: dict[str, Any]) -> str:
    """Create a stable identity for exact duplicate JSONL records."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def _load_daily_data(log_path: str | Path) -> dict[date, _DailyData]:
    """Load relevant derived events grouped by UTC calendar date.

    Exact duplicate records are ignored. A daily summary is preferred for
    runtime because it explicitly represents zero-runtime days; raw completed
    furnace sessions remain the fallback for dates without a summary.
    """
    daily: defaultdict[date, _DailyData] = defaultdict(_DailyData)
    seen_events: set[str] = set()

    with open(log_path, encoding="utf-8") as events_file:
        for line in events_file:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            schema = event.get("schema")
            if schema not in {
                OUTDOOR_SCHEMA,
                FURNACE_SESSION_SCHEMA,
                FURNACE_SUMMARY_SCHEMA,
            }:
                continue
            key = _event_key(event)
            if key in seen_events:
                continue
            seen_events.add(key)

            data = event.get("data")
            if not isinstance(data, dict):
                continue

            if schema == OUTDOOR_SCHEMA:
                temperature_f = _finite_number(data.get("temperature_f"))
                event_date = _utc_date(data.get("timestamp") or event.get("ts"))
                if temperature_f is not None and event_date is not None:
                    daily[event_date].outdoor_temps_f.append(temperature_f)
                continue

            if schema == FURNACE_SESSION_SCHEMA:
                duration_s = _finite_number(data.get("duration_s"))
                event_date = _utc_date(data.get("ended_at") or event.get("ts"))
                if duration_s is not None and duration_s >= 0 and event_date is not None:
                    daily[event_date].raw_runtime_s += duration_s
                    daily[event_date].raw_runtime_seen = True
                continue

            event_date = _summary_date(data.get("date"))
            runtime_s = _finite_number(data.get("total_furnace_runtime_s"))
            summary_temp_f = _finite_number(data.get("outdoor_temp_avg_f"))
            if event_date is not None:
                values = daily[event_date]
                if runtime_s is not None and runtime_s >= 0:
                    values.summary_runtime_s = runtime_s
                if summary_temp_f is not None:
                    values.summary_temp_f = summary_temp_f

    return dict(daily)


def _point_for(event_date: date, values: _DailyData) -> DailyScatterPoint:
    """Convert intermediate daily values into a serializable point."""
    if values.outdoor_temps_f:
        avg_temp_f: float | None = round(
            sum(values.outdoor_temps_f) / len(values.outdoor_temps_f), 1
        )
    else:
        avg_temp_f = round(values.summary_temp_f, 1) if values.summary_temp_f is not None else None

    runtime_s = values.summary_runtime_s
    if runtime_s is None and values.raw_runtime_seen:
        runtime_s = values.raw_runtime_s
    runtime_min = round(runtime_s / 60, 1) if runtime_s is not None else None

    return DailyScatterPoint(
        date=event_date.isoformat(),
        avg_temp_f=avg_temp_f,
        furnace_runtime_min=runtime_min,
    )


def build_scatter_points(
    log_path: str | Path,
    start: date | None = None,
    end: date | None = None,
) -> list[DailyScatterPoint]:
    """Build sorted daily points, optionally restricted to an inclusive range."""
    if start is not None and end is not None and start > end:
        raise ValueError("start date must be on or before end date")

    daily = _load_daily_data(log_path)
    selected_dates = (
        event_date
        for event_date in daily
        if (start is None or event_date >= start) and (end is None or event_date <= end)
    )
    return [_point_for(event_date, daily[event_date]) for event_date in sorted(selected_dates)]


def _csv_value(value: float | None) -> str:
    """Format optional numeric values consistently for CSV consumers."""
    return "" if value is None else f"{value:.1f}"


def write_csv(points: list[DailyScatterPoint], out_path: str | Path) -> None:
    """Write points with the stable three-column scatter-data contract."""
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for point in points:
            writer.writerow(
                {
                    "date": point.date,
                    "avg_temp_f": _csv_value(point.avg_temp_f),
                    "furnace_runtime_min": _csv_value(point.furnace_runtime_min),
                }
            )


def _print_summary(points: list[DailyScatterPoint], file: TextIO = sys.stdout) -> None:
    """Print coverage information without interpreting the correlation."""
    complete = sum(
        point.avg_temp_f is not None and point.furnace_runtime_min is not None for point in points
    )
    print(f"Days represented: {len(points)}", file=file)
    print(f"Complete scatter points: {complete}", file=file)
    print(f"Partial rows: {len(points) - complete}", file=file)
    if points:
        print(f"Date range: {points[0].date} → {points[-1].date}", file=file)


def _parse_date_arg(value: str) -> date:
    """Parse a CLI date argument."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date_arg, help="Inclusive UTC start date")
    parser.add_argument("--end", type=_parse_date_arg, help="Inclusive UTC end date")
    parser.add_argument(
        "--log",
        default=None,
        help="Path to derived event JSONL (overrides DERIVED_EVENT_LOG env var)",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUT, help=f"CSV output path (default: {DEFAULT_OUT})"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the scatter-data export CLI."""
    args = _parse_args(argv)
    if (args.start is None) != (args.end is None):
        print("Error: --start and --end must be supplied together", file=sys.stderr)
        return 2
    if args.start is not None and args.start > args.end:
        print("Error: --start must be on or before --end", file=sys.stderr)
        return 2

    log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
    try:
        points = build_scatter_points(log_path, start=args.start, end=args.end)
        write_csv(points, args.out)
    except OSError as exc:
        print(f"Error reading or writing data: {exc}", file=sys.stderr)
        return 1

    _print_summary(points)
    print(f"CSV written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
