#!/usr/bin/env python3
"""
Compute furnace duty cycle for a time window from heating_session_ended.v1 events.

Duty cycle = (total furnace on-time / total window duration) × 100%.

Sessions that span window boundaries are clipped: only the portion of the session
that falls within [start, end] is counted.

Usage:
    python3 scripts/furnace_duty_cycle.py --start 2026-01-15 --end 2026-01-15
    python3 scripts/furnace_duty_cycle.py --start 2026-01-01 --end 2026-01-31
    python3 scripts/furnace_duty_cycle.py --start "2026-01-15T06:00" --end "2026-01-15T18:00"

Arguments:
    --start   Window start (YYYY-MM-DD or YYYY-MM-DDTHH:MM, inclusive)
    --end     Window end   (YYYY-MM-DD or YYYY-MM-DDTHH:MM, inclusive)
              Date-only values expand to start/end of that calendar day (UTC).
    --log     Optional: path to derived event JSONL (overrides DERIVED_EVENT_LOG env var)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta

SCHEMA = "homeops.consumer.heating_session_ended.v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_window_dt(s: str, end_of_day: bool = False) -> datetime:
    """
    Parse a date or datetime string into a UTC-aware datetime.

    Date-only (YYYY-MM-DD):
      - start → midnight UTC that day
      - end   → 23:59:59 UTC that day (end_of_day=True)
    Datetime (YYYY-MM-DDTHH:MM or YYYY-MM-DDTHH:MM:SS):
      - treated as UTC regardless of local timezone
    """
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ]
    dt: datetime | None = None
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(f"Cannot parse datetime: {s!r} — use YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    dt = dt.replace(tzinfo=UTC)
    if end_of_day and len(s) == 10:  # date-only → expand to end of day
        dt = dt + timedelta(days=1) - timedelta(seconds=1)
    return dt


def _fmt_duration(seconds: float) -> str:
    """Format seconds as 'Xh Ym Zs'."""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _load_and_clip(log_path: str, window_start: datetime, window_end: datetime) -> list[dict]:
    """
    Read heating_session_ended.v1 events from log_path.

    For each session, compute the clipped contribution within [window_start, window_end].
    Returns list of dicts: {ended_at, duration_s, clipped_s, started_at}.
    Skips sessions with null duration_s.
    """
    results: list[dict] = []
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
                duration_s = data.get("duration_s")
                if duration_s is None:
                    continue  # session started before consumer restart — can't clip accurately

                ended_at_str = data.get("ended_at") or evt.get("ts")
                if not ended_at_str:
                    continue
                try:
                    ended_at = datetime.fromisoformat(ended_at_str.replace("Z", "+00:00"))
                    if ended_at.tzinfo is None:
                        ended_at = ended_at.replace(tzinfo=UTC)
                except ValueError:
                    continue

                started_at = ended_at - timedelta(seconds=duration_s)

                # Clip to window boundaries
                effective_start = max(started_at, window_start)
                effective_end = min(ended_at, window_end)

                if effective_end <= effective_start:
                    continue  # session entirely outside window

                clipped_s = (effective_end - effective_start).total_seconds()
                results.append(
                    {
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "duration_s": duration_s,
                        "clipped_s": clipped_s,
                    }
                )
    except FileNotFoundError:
        print(f"Error: log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error reading log file: {e}", file=sys.stderr)
        sys.exit(1)
    return results


def _compute(sessions: list[dict], window_start: datetime, window_end: datetime) -> dict:
    """Aggregate session contributions into duty cycle stats."""
    window_duration_s = (window_end - window_start).total_seconds()
    total_on_s = sum(s["clipped_s"] for s in sessions)
    duty_cycle_pct = (total_on_s / window_duration_s * 100) if window_duration_s > 0 else 0.0
    return {
        "session_count": len(sessions),
        "total_on_s": total_on_s,
        "window_duration_s": window_duration_s,
        "duty_cycle_pct": duty_cycle_pct,
        "clipped_count": sum(1 for s in sessions if s["clipped_s"] < s["duration_s"]),
    }


def _print_result(
    stats: dict, window_start: datetime, window_end: datetime, sessions: list[dict]
) -> None:
    """Print formatted duty cycle output."""
    print()
    print(
        f"Furnace duty cycle: {window_start.strftime('%Y-%m-%d %H:%M')} UTC"
        f" → {window_end.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    print()
    print(f"  Window duration :  {_fmt_duration(stats['window_duration_s'])}")
    print(f"  Total on-time   :  {_fmt_duration(stats['total_on_s'])}")
    print(
        f"  Sessions counted:  {stats['session_count']}"
        + (f"  ({stats['clipped_count']} clipped at boundary)" if stats["clipped_count"] else "")
    )
    print(f"  Duty cycle      :  {stats['duty_cycle_pct']:.1f}%")
    print()
    if not sessions:
        print("  No heating_session_ended.v1 events found in window.")
        print("  (Sessions still in progress at --end time are not counted.)")
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute furnace duty cycle from heating_session_ended.v1 events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--start", required=True, help="Window start (YYYY-MM-DD or YYYY-MM-DDTHH:MM)"
    )
    parser.add_argument("--end", required=True, help="Window end (YYYY-MM-DD or YYYY-MM-DDTHH:MM)")
    parser.add_argument("--log", metavar="PATH", help="Derived event JSONL path")
    args = parser.parse_args()

    try:
        window_start = _parse_window_dt(args.start, end_of_day=False)
        window_end = _parse_window_dt(args.end, end_of_day=True)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if window_start >= window_end:
        print("Error: --start must be before --end", file=sys.stderr)
        sys.exit(1)

    log_path = args.log or os.environ.get("DERIVED_EVENT_LOG") or "state/consumer/events.jsonl"
    sessions = _load_and_clip(log_path, window_start, window_end)
    stats = _compute(sessions, window_start, window_end)
    _print_result(stats, window_start, window_end, sessions)


if __name__ == "__main__":
    main()
