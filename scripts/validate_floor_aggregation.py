#!/usr/bin/env python3
"""
Validate that floor_daily_summary.v1 totals match raw floor_call_ended.v1 events.

For each floor+day present in the log, sums duration_s from all floor_call_ended.v1
events and compares to the total_runtime_s reported in floor_daily_summary.v1.
Reports any mismatches and a pass/fail summary.

Usage:
    python3 scripts/validate_floor_aggregation.py
    python3 scripts/validate_floor_aggregation.py --log state/consumer/events.jsonl
    python3 scripts/validate_floor_aggregation.py --start 2026-01-01 --end 2026-01-31
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date

FLOOR_CALL_SCHEMA = "homeops.consumer.floor_call_ended.v1"
DAILY_SUMMARY_SCHEMA = "homeops.consumer.floor_daily_summary.v1"

KNOWN_FLOORS = ("floor_1", "floor_2", "floor_3")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_log(
    log_path: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    """
    Parse the JSONL log and return two dicts keyed by (floor, date):

    raw_totals:  summed duration_s from floor_call_ended.v1 events
    summary_totals: total_runtime_s from floor_daily_summary.v1 events

    Only days within [start, end] (inclusive) are included when provided.
    """
    # {(floor, date): total_s}
    raw_totals: dict[tuple[str, str], int] = defaultdict(int)
    summary_totals: dict[tuple[str, str], int] = {}

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

                schema = evt.get("schema", "")
                data = evt.get("data", {})

                if schema == FLOOR_CALL_SCHEMA:
                    floor = data.get("floor")
                    dur = data.get("duration_s")
                    ts_str = data.get("ended_at") or evt.get("ts", "")
                    if not floor or dur is None or not ts_str:
                        continue
                    try:
                        evt_date = date.fromisoformat(ts_str[:10])
                    except ValueError:
                        continue
                    if start and evt_date < start:
                        continue
                    if end and evt_date > end:
                        continue
                    raw_totals[(floor, str(evt_date))] += dur

                elif schema == DAILY_SUMMARY_SCHEMA:
                    floor = data.get("floor")
                    runtime = data.get("total_runtime_s")
                    day = data.get("date")
                    if not floor or runtime is None or not day:
                        continue
                    try:
                        evt_date = date.fromisoformat(day)
                    except ValueError:
                        continue
                    if start and evt_date < start:
                        continue
                    if end and evt_date > end:
                        continue
                    summary_totals[(floor, day)] = runtime

    except FileNotFoundError:
        print(f"Error: log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error reading log file: {e}", file=sys.stderr)
        sys.exit(1)

    return dict(raw_totals), summary_totals


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(
    raw_totals: dict[tuple[str, str], int],
    summary_totals: dict[tuple[str, str], int],
) -> list[dict]:
    """
    Cross-check raw_totals against summary_totals.

    Returns a list of mismatch dicts:
      {floor, date, raw_s, summary_s, delta_s}

    Only compares days present in summary_totals (days without a summary
    have not rolled over yet and cannot be validated).
    """
    mismatches: list[dict] = []
    for (floor, day), summary_s in sorted(summary_totals.items()):
        raw_s = raw_totals.get((floor, day), 0)
        if raw_s != summary_s:
            mismatches.append(
                {
                    "floor": floor,
                    "date": day,
                    "raw_s": raw_s,
                    "summary_s": summary_s,
                    "delta_s": summary_s - raw_s,
                }
            )
    return mismatches


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _fmt_s(seconds: int) -> str:
    h = abs(seconds) // 3600
    m = (abs(seconds) % 3600) // 60
    sign = "-" if seconds < 0 else ""
    if h > 0:
        return f"{sign}{h}h {m:02d}m"
    return f"{sign}{m}m"


def print_report(
    mismatches: list[dict],
    summary_totals: dict[tuple[str, str], int],
    raw_totals: dict[tuple[str, str], int],
) -> None:
    days_checked = len({day for (_, day) in summary_totals})
    pairs_checked = len(summary_totals)

    print()
    print(
        f"Floor aggregation validation — {days_checked} day(s),"
        f" {pairs_checked} floor+day pair(s) checked"
    )
    print()

    if not mismatches:
        print(f"  ✅ All {pairs_checked} floor+day totals match — zero mismatches.")
    else:
        print(f"  ❌ {len(mismatches)} mismatch(es) found:")
        print()
        hdr = f"  {'Floor':<10}  {'Date':<12}  {'Raw':>10}  {'Summary':>10}  {'Delta':>10}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for m in mismatches:
            print(
                f"  {m['floor']:<10}  {m['date']:<12}  "
                f"{_fmt_s(m['raw_s']):>10}  {_fmt_s(m['summary_s']):>10}  "
                f"{_fmt_s(m['delta_s']):>10}"
            )

    print()

    # Days in raw but not in summary (session data exists but no rollover yet)
    raw_only_days = {day for (_, day) in raw_totals} - {day for (_, day) in summary_totals}
    if raw_only_days:
        print(
            f"  ℹ️  {len(raw_only_days)} day(s) have raw events but no daily summary yet"
            f" (not validated): {', '.join(sorted(raw_only_days))}"
        )
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate floor_daily_summary.v1 totals against raw floor_call_ended.v1 events."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--log", metavar="PATH", help="Derived event JSONL path")
    parser.add_argument(
        "--start", metavar="YYYY-MM-DD", help="Only validate days on or after this date"
    )
    parser.add_argument(
        "--end", metavar="YYYY-MM-DD", help="Only validate days on or before this date"
    )
    args = parser.parse_args()

    start: date | None = None
    end: date | None = None
    if args.start:
        try:
            start = date.fromisoformat(args.start)
        except ValueError:
            print(f"Error: invalid --start date: {args.start}", file=sys.stderr)
            sys.exit(1)
    if args.end:
        try:
            end = date.fromisoformat(args.end)
        except ValueError:
            print(f"Error: invalid --end date: {args.end}", file=sys.stderr)
            sys.exit(1)

    log_path = args.log or os.environ.get("DERIVED_EVENT_LOG") or "state/consumer/events.jsonl"
    raw_totals, summary_totals = load_log(log_path, start, end)

    if not summary_totals:
        print()
        print("No floor_daily_summary.v1 events found. Cannot validate.")
        print("(Run the consumer for at least one full day to generate summaries.)")
        print()
        sys.exit(0)

    mismatches = validate(raw_totals, summary_totals)
    print_report(mismatches, summary_totals, raw_totals)
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
