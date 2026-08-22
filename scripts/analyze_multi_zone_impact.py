#!/usr/bin/env python3
"""Measure time-to-temperature by simultaneous-zone contention.

The command is intentionally read-only.  It consumes
``zone_time_to_temp.v1`` events, groups them by zone and the other zones that
were calling at session start, and reports sample sizes and duration/rate
statistics.  It refuses to make a scheduling recommendation when either the
contended or uncontended comparison group lacks the configured minimum sample
size.

Usage::

    PYTHONPATH=services/insights python3 scripts/analyze_multi_zone_impact.py \
        --log state/consumer/events.jsonl
    ssh bob@pi 'cat /path/to/events.jsonl' | \
        python3 scripts/analyze_multi_zone_impact.py --log -

Revision history:
  2026-08-22  Added deterministic, read-only contention analysis with explicit
              sample sufficiency checks so sparse HVAC history cannot produce
              unsupported scheduling recommendations.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean, median
from typing import Any, TextIO

SCHEMA = "homeops.consumer.zone_time_to_temp.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_MIN_SAMPLES = 5
ZONE_ENTITY_TO_FLOOR = {
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


def _finite_number(value: Any) -> float | None:
    """Return a finite numeric value, excluding booleans and malformed data."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalise_other_zones(value: Any) -> tuple[str, ...] | None:
    """Map floor-call entity IDs to floor names and return a stable tuple."""
    if not isinstance(value, list):
        return None
    zones = {
        ZONE_ENTITY_TO_FLOOR.get(item, item) for item in value if isinstance(item, str) and item
    }
    return tuple(sorted(zones))


def extract_records(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Extract valid time-to-temperature records and count malformed payloads."""
    records: list[dict[str, Any]] = []
    invalid_payloads = 0

    for event in events:
        if event.get("schema") != SCHEMA:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            invalid_payloads += 1
            continue
        zone = data.get("zone")
        duration_s = _finite_number(data.get("duration_s"))
        other_zones = _normalise_other_zones(data.get("other_zones_calling"))
        if (
            not isinstance(zone, str)
            or not zone
            or duration_s is None
            or duration_s < 0
            or other_zones is None
        ):
            invalid_payloads += 1
            continue

        records.append(
            {
                "zone": zone,
                "other_zones": other_zones,
                "duration_s": duration_s,
                "degrees_per_min": _finite_number(data.get("degrees_per_min")),
                "outdoor_temp_f": _finite_number(data.get("outdoor_temp_f")),
                "ts": event.get("ts"),
            }
        )

    return records, invalid_payloads


def _date_range(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a stable date range based on event envelope timestamps."""
    values: list[str] = []
    for record in records:
        timestamp = record.get("ts")
        if not isinstance(timestamp, str) or len(timestamp) < 10:
            continue
        value = timestamp[:10]
        try:
            date.fromisoformat(value)
        except ValueError:
            continue
        values.append(value)
    unique = sorted(set(values))
    return {
        "unique_days": len(unique),
        "first": unique[0] if unique else None,
        "last": unique[-1] if unique else None,
    }


def _rounded(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def summarise_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise one contention group with duration, rate, and weather context."""
    durations = [float(record["duration_s"]) for record in records]
    rates = [record["degrees_per_min"] for record in records]
    rates = [float(rate) for rate in rates if rate is not None]
    temperatures = [record["outdoor_temp_f"] for record in records]
    temperatures = [float(temp) for temp in temperatures if temp is not None]
    return {
        "count": len(records),
        "mean_duration_s": _rounded(mean(durations)),
        "median_duration_s": _rounded(float(median(durations))),
        "min_duration_s": _rounded(min(durations)),
        "max_duration_s": _rounded(max(durations)),
        "mean_degrees_per_min": _rounded(mean(rates)) if rates else None,
        "outdoor_temp_count": len(temperatures),
        "outdoor_temp_min_f": _rounded(min(temperatures)) if temperatures else None,
        "outdoor_temp_max_f": _rounded(max(temperatures)) if temperatures else None,
    }


def analyse_records(
    records: list[dict[str, Any]], min_samples: int = DEFAULT_MIN_SAMPLES
) -> dict[str, Any]:
    """Build exact contention groups and conservative per-zone comparisons."""
    if min_samples < 1:
        raise ValueError("min_samples must be at least 1")

    groups: defaultdict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    by_zone: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (record["zone"], record["other_zones"])
        groups[key].append(record)
        by_zone[record["zone"]].append(record)

    group_reports: list[dict[str, Any]] = []
    for (zone, other_zones), group_records in sorted(groups.items()):
        group_reports.append(
            {
                "zone": zone,
                "other_zones": list(other_zones),
                "other_zone_count": len(other_zones),
                "sufficient_samples": len(group_records) >= min_samples,
                **summarise_group(group_records),
            }
        )

    comparisons: list[dict[str, Any]] = []
    for zone, zone_records in sorted(by_zone.items()):
        uncontended = [record for record in zone_records if not record["other_zones"]]
        contended = [record for record in zone_records if record["other_zones"]]
        uncontended_stats = summarise_group(uncontended) if uncontended else None
        contended_stats = summarise_group(contended) if contended else None
        sufficient = len(uncontended) >= min_samples and len(contended) >= min_samples
        median_delta = None
        if sufficient and uncontended_stats and contended_stats:
            median_delta = _rounded(
                float(contended_stats["median_duration_s"])
                - float(uncontended_stats["median_duration_s"])
            )
        comparisons.append(
            {
                "zone": zone,
                "uncontended_count": len(uncontended),
                "contended_count": len(contended),
                "uncontended_median_duration_s": (
                    uncontended_stats["median_duration_s"] if uncontended_stats else None
                ),
                "contended_median_duration_s": (
                    contended_stats["median_duration_s"] if contended_stats else None
                ),
                "median_duration_delta_s": median_delta,
                "sufficient_samples": sufficient,
                "conclusion": ("comparison_available" if sufficient else "insufficient_data"),
            }
        )

    sufficient_comparisons = [item for item in comparisons if item["sufficient_samples"]]
    if sufficient_comparisons:
        conclusion = {
            "status": "comparison_available",
            "supports_scheduling_conclusion": True,
            "reason": "Both uncontended and contended groups meet the minimum sample size.",
        }
    else:
        conclusion = {
            "status": "insufficient_data",
            "supports_scheduling_conclusion": False,
            "reason": (
                "No zone has at least the minimum sample size in both uncontended and "
                "contended groups."
            ),
        }

    return {
        "schema": "homeops.multi-zone-impact-analysis.v1",
        "min_samples": min_samples,
        "coverage": {
            "valid_records": len(records),
            "date_range": _date_range(records),
            "zones": sorted(by_zone),
        },
        "groups": group_reports,
        "zone_comparisons": comparisons,
        "conclusion": conclusion,
    }


def build_report(
    events: list[dict[str, Any]],
    invalid_json_lines: int = 0,
    non_object_lines: int = 0,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    source: str = "events.jsonl",
) -> dict[str, Any]:
    """Build a deterministic report from a complete event history."""
    records, invalid_payloads = extract_records(events)
    report = analyse_records(records, min_samples=min_samples)
    report["source"] = source
    report["data_quality"] = {
        "valid_json_objects": len(events),
        "invalid_json_lines": invalid_json_lines,
        "non_object_lines": non_object_lines,
        "invalid_zone_time_payloads": invalid_payloads,
    }
    return report


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    """Render the analysis as a reviewable Markdown report."""
    coverage = report["coverage"]
    quality = report["data_quality"]
    lines = [
        "# HomeOps multi-zone call impact analysis",
        "",
        f"Source: `{report['source']}`",
        (
            f"Coverage: `{_fmt(coverage['date_range']['first'])}` → "
            f"`{_fmt(coverage['date_range']['last'])}`; "
            f"{coverage['date_range']['unique_days']} unique days"
        ),
        f"Valid `zone_time_to_temp.v1` records: **{coverage['valid_records']}**",
        (
            f"Data quality: {quality['valid_json_objects']} valid JSON objects; "
            f"{quality['invalid_json_lines']} invalid JSON lines; "
            f"{quality['non_object_lines']} non-object lines; "
            f"{quality['invalid_zone_time_payloads']} invalid target payloads"
        ),
        "",
        "## Method",
        "",
        (
            f"Records are grouped by zone and the exact other zones calling at session "
            f"start. A comparison requires at least **{report['min_samples']}** "
            "uncontended and contended records for the same zone."
        ),
        "",
        "## Contention groups",
        "",
        "| Zone | Other zones | Samples | Median duration (s) | Mean duration (s) | "
        "Mean °F/min | Outdoor samples | Sufficient |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    if report["groups"]:
        for group in report["groups"]:
            others = ", ".join(group["other_zones"]) if group["other_zones"] else "none"
            lines.append(
                f"| {group['zone']} | {others} | {group['count']} | "
                f"{_fmt(group['median_duration_s'])} | {_fmt(group['mean_duration_s'])} | "
                f"{_fmt(group['mean_degrees_per_min'])} | {group['outdoor_temp_count']} | "
                f"{group['sufficient_samples']} |"
            )
    else:
        lines.append("| — | — | 0 | — | — | — | 0 | False |")

    lines.extend(
        [
            "",
            "## Per-zone comparison",
            "",
            "| Zone | Uncontended samples | Contended samples | Uncontended median (s) | "
            "Contended median (s) | Median delta (s) | Result |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    if report["zone_comparisons"]:
        for comparison in report["zone_comparisons"]:
            lines.append(
                f"| {comparison['zone']} | {comparison['uncontended_count']} | "
                f"{comparison['contended_count']} | "
                f"{_fmt(comparison['uncontended_median_duration_s'])} | "
                f"{_fmt(comparison['contended_median_duration_s'])} | "
                f"{_fmt(comparison['median_duration_delta_s'])} | "
                f"{comparison['conclusion']} |"
            )
    else:
        lines.append("| — | 0 | 0 | — | — | — | insufficient_data |")

    conclusion = report["conclusion"]
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            f"**{conclusion['status']}** — {conclusion['reason']}",
            "",
            (
                "No HA automation, thermostat setting, or scheduling threshold is changed "
                "by this analysis. A scheduling recommendation is only defensible after "
                "both comparison groups meet the minimum sample size and outdoor-temperature "
                "confounding is addressed."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        default=None,
        help="Derived event JSONL path, or '-' for stdin (overrides DERIVED_EVENT_LOG).",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help=f"Minimum samples per comparison group (default: {DEFAULT_MIN_SAMPLES}).",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Report format (default: markdown).",
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
    source = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
    try:
        events, invalid_json_lines, non_object_lines = load_events(source)
        report = build_report(
            events,
            invalid_json_lines=invalid_json_lines,
            non_object_lines=non_object_lines,
            min_samples=args.min_samples,
            source=source,
        )
        rendered = (
            json.dumps(report, indent=2, sort_keys=True) + "\n"
            if args.format == "json"
            else render_markdown(report)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
