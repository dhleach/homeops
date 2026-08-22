#!/usr/bin/env python3
"""Count zone calls by local hour over an explicit date range.

The command is intentionally read-only.  It consumes
``homeops.consumer.floor_call_started.v1`` events, converts their timestamps
to the requested timezone, and reports one 24-hour row per known floor.

Usage::

    python3 scripts/floor_hourly_heatmap.py \
        --start 2026-08-15 --end 2026-08-21
    ssh bob@pi 'cat /path/to/events.jsonl' | \
        python3 scripts/floor_hourly_heatmap.py \
        --log - --start 2026-08-15 --end 2026-08-21

Date boundaries are inclusive and interpreted in the selected timezone.  The
default timezone is ``America/New_York`` so the report reflects the home's
local clock rather than UTC.

Revision history:
  2026-08-22  Added deterministic, read-only hourly call-frequency reporting
              with explicit local date boundaries and timezone conversion so
              scheduling analysis can use real zone-demand patterns safely.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "homeops.consumer.floor_call_started.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_TIMEZONE = "America/New_York"
KNOWN_FLOORS = ("floor_1", "floor_2", "floor_3")
FLOOR_ENTITY_MAP = {
    "binary_sensor.floor_1_heating_call": "floor_1",
    "binary_sensor.floor_2_heating_call": "floor_2",
    "binary_sensor.floor_3_heating_call": "floor_3",
}


def load_events(source: str) -> tuple[list[dict[str, Any]], int, int]:
    """Load JSON objects from a path or stdin and count malformed records."""
    events: list[dict[str, Any]] = []
    invalid_json_lines = 0
    non_object_lines = 0

    stream: TextIO
    close_stream = False
    if source == "-":
        stream = sys.stdin
    else:
        stream = open(source, encoding="utf-8")
        close_stream = True

    try:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_json_lines += 1
                continue
            if not isinstance(event, dict):
                non_object_lines += 1
                continue
            events.append(event)
    finally:
        if close_stream:
            stream.close()

    return events, invalid_json_lines, non_object_lines


def _parse_date(value: str, label: str) -> date:
    """Parse a YYYY-MM-DD command-line date with a useful error."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be YYYY-MM-DD: {value!r}") from exc


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp, treating a missing offset as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _normalise_floor(floor: Any, entity_id: Any = None) -> str | None:
    """Return a known floor name, falling back to its canonical entity ID."""
    if isinstance(floor, str):
        candidate = floor.strip().lower().replace("-", "_").replace(" ", "_")
        if candidate in KNOWN_FLOORS:
            return candidate

    if isinstance(entity_id, str):
        return FLOOR_ENTITY_MAP.get(entity_id.strip().lower())
    return None


def extract_records(
    events: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Extract valid in-range call starts and return extraction counters."""
    if start_date > end_date:
        raise ValueError("start date must be on or before end date")

    records: list[dict[str, Any]] = []
    counters = {
        "matching_events": 0,
        "valid_payloads": 0,
        "included_events": 0,
        "excluded_out_of_range": 0,
        "invalid_payloads": 0,
    }

    for event in events:
        if event.get("schema") != SCHEMA:
            continue
        counters["matching_events"] += 1
        data = event.get("data")
        if not isinstance(data, dict):
            counters["invalid_payloads"] += 1
            continue

        floor = _normalise_floor(data.get("floor"), data.get("entity_id"))
        timestamp = _parse_timestamp(data.get("started_at") or event.get("ts"))
        if floor is None or timestamp is None:
            counters["invalid_payloads"] += 1
            continue

        counters["valid_payloads"] += 1
        local_timestamp = timestamp.astimezone(timezone)
        local_date = local_timestamp.date()
        if not start_date <= local_date <= end_date:
            counters["excluded_out_of_range"] += 1
            continue

        records.append(
            {
                "floor": floor,
                "date": local_date.isoformat(),
                "hour": local_timestamp.hour,
                "timestamp": local_timestamp.isoformat(),
            }
        )
        counters["included_events"] += 1

    return records, counters


def _peak_hours(hours: list[int]) -> list[int]:
    """Return all peak hour numbers, or no peak for an empty row."""
    peak = max(hours, default=0)
    return [hour for hour, count in enumerate(hours) if count == peak and peak > 0]


def build_report(
    events: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
    invalid_json_lines: int = 0,
    non_object_lines: int = 0,
    source: str = "events.jsonl",
) -> dict[str, Any]:
    """Build a stable hourly frequency report from a complete event history."""
    records, extraction = extract_records(events, start_date, end_date, timezone)
    counts = {floor: [0] * 24 for floor in KNOWN_FLOORS}
    observed_dates: set[str] = set()
    for record in records:
        counts[record["floor"]][record["hour"]] += 1
        observed_dates.add(record["date"])

    floor_reports = {}
    for floor in KNOWN_FLOORS:
        hours = counts[floor]
        floor_reports[floor] = {
            "hours": hours,
            "total": sum(hours),
            "peak_hours": _peak_hours(hours),
        }

    return {
        "schema": "homeops.floor-hourly-heatmap.v1",
        "source": source,
        "timezone": str(timezone),
        "date_range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "inclusive_days": (end_date - start_date).days + 1,
        },
        "coverage": {
            "included_events": len(records),
            "observed_dates": sorted(observed_dates),
        },
        "floors": floor_reports,
        "data_quality": {
            "valid_json_objects": len(events),
            "invalid_json_lines": invalid_json_lines,
            "non_object_lines": non_object_lines,
            **extraction,
        },
    }


def _fmt_counts(hours: list[int]) -> str:
    return " ".join(f"{count:02d}" for count in hours)


def render_table(report: dict[str, Any]) -> str:
    """Render the report as a readable fixed-width hourly table."""
    date_range = report["date_range"]
    quality = report["data_quality"]
    header = " ".join(f"{hour:02d}" for hour in range(24))
    separator = "-" * (10 + 3 + len(header) + 3 + 5 + 3 + 12)
    lines = [
        "HomeOps hourly zone-call frequency",
        (
            f"Range: {date_range['start']} → {date_range['end']} "
            f"({date_range['inclusive_days']} inclusive days)"
        ),
        f"Timezone: {report['timezone']}",
        "",
        f"{'Floor':<10} | {header} | Total | Peak hours",
        separator,
    ]
    for floor in KNOWN_FLOORS:
        floor_report = report["floors"][floor]
        peak = ", ".join(f"{hour:02d}" for hour in floor_report["peak_hours"]) or "—"
        lines.append(
            f"{floor:<10} | {_fmt_counts(floor_report['hours'])} | "
            f"{floor_report['total']:>5} | {peak}"
        )

    lines.extend(
        [
            "",
            (
                f"Included calls: {quality['included_events']} of "
                f"{quality['valid_payloads']} valid matching payloads; "
                f"{quality['excluded_out_of_range']} outside range"
            ),
            (
                f"Data quality: {quality['valid_json_objects']} valid JSON objects; "
                f"{quality['invalid_json_lines']} invalid JSON lines; "
                f"{quality['non_object_lines']} non-object lines; "
                f"{quality['invalid_payloads']} invalid call payloads"
            ),
            "Read-only report: no consumer state, thermostat setting, or HA automation is changed.",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive local start date (YYYY-MM-DD).")
    parser.add_argument("--end", required=True, help="Inclusive local end date (YYYY-MM-DD).")
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"IANA timezone for dates/hours (default: {DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Derived event JSONL path, or '-' for stdin (overrides DERIVED_EVENT_LOG).",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Report format (default: table).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path; otherwise write the report to stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        start_date = _parse_date(args.start, "--start")
        end_date = _parse_date(args.end, "--end")
        if start_date > end_date:
            raise ValueError("--start must be on or before --end")
        timezone = ZoneInfo(args.timezone)
        source = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
        events, invalid_json_lines, non_object_lines = load_events(source)
        report = build_report(
            events,
            start_date,
            end_date,
            timezone,
            invalid_json_lines=invalid_json_lines,
            non_object_lines=non_object_lines,
            source=source,
        )
        rendered = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.format == "json"
            else render_table(report)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, TypeError, ValueError, ZoneInfoNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
