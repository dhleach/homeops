"""Monthly aggregate summary for furnace daily summary data.

Reads ``furnace_daily_summary.v1`` events from a JSONL file and computes
aggregated statistics for a given calendar month.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

_SUMMARY_SCHEMA = "homeops.consumer.furnace_daily_summary.v1"

KNOWN_FLOORS = ("floor_1", "floor_2", "floor_3")
KNOWN_WARNINGS = (
    "floor_2_long_call",
    "floor_no_response",
    "zone_slow_to_heat",
    "observer_silence",
    "setpoint_miss",
)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class MonthlyStats:
    """Aggregated furnace statistics for a calendar month."""

    month: str  # "YYYY-MM"
    day_count: int = 0  # days with data
    days_in_month: int = 0  # expected days in month
    total_furnace_s: int = 0
    session_count: int = 0
    per_floor_s: dict[str, int] = field(default_factory=dict)
    per_floor_sessions: dict[str, int] = field(default_factory=dict)
    outdoor_temps: list[float] = field(default_factory=list)
    outdoor_min_f: float | None = None
    outdoor_max_f: float | None = None
    warnings: dict[str, int] = field(default_factory=dict)

    @property
    def outdoor_avg_f(self) -> float | None:
        if not self.outdoor_temps:
            return None
        return round(sum(self.outdoor_temps) / len(self.outdoor_temps), 1)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def _days_in_month(year: int, month: int) -> int:
    """Return the number of days in a given month/year."""
    import calendar

    return calendar.monthrange(year, month)[1]


def load_daily_summaries(events_file: str) -> list[dict]:
    """Read JSONL and return only furnace_daily_summary.v1 events."""
    summaries: list[dict] = []
    try:
        with open(events_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("schema") == _SUMMARY_SCHEMA:
                    summaries.append(evt)
    except FileNotFoundError:
        pass
    return summaries


def compute_monthly_summary(events: list[dict], month: str) -> MonthlyStats:
    """
    Aggregate furnace_daily_summary.v1 events for the given month.

    Args:
        events: List of ``furnace_daily_summary.v1`` event dicts.
        month:  Month string ``"YYYY-MM"`` to filter on.

    Returns:
        A :class:`MonthlyStats` populated with the month's aggregated data.
    """
    import re as _re

    if not _re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError(f"Invalid month format {month!r}; expected YYYY-MM")
    try:
        year, mon = int(month[:4]), int(month[5:7])
    except (ValueError, IndexError):
        raise ValueError(f"Invalid month format {month!r}; expected YYYY-MM")

    stats = MonthlyStats(month=month)
    stats.days_in_month = _days_in_month(year, mon)

    monthly_events = [e for e in events if e.get("data", {}).get("date", "").startswith(month)]

    if not monthly_events:
        return stats

    all_min: list[float] = []
    all_max: list[float] = []

    for evt in monthly_events:
        d = evt.get("data", {})
        stats.day_count += 1
        stats.total_furnace_s += d.get("total_furnace_runtime_s", 0)
        stats.session_count += d.get("session_count", 0)

        for floor in KNOWN_FLOORS:
            pfr = d.get("per_floor_runtime_s", {})
            stats.per_floor_s[floor] = stats.per_floor_s.get(floor, 0) + pfr.get(floor, 0)
            pfc = d.get("per_floor_session_count", {})
            stats.per_floor_sessions[floor] = stats.per_floor_sessions.get(floor, 0) + pfc.get(
                floor, 0
            )

        avg_t = d.get("outdoor_temp_avg_f")
        if avg_t is not None:
            stats.outdoor_temps.append(float(avg_t))

        min_t = d.get("outdoor_temp_min_f")
        if min_t is not None:
            all_min.append(float(min_t))

        max_t = d.get("outdoor_temp_max_f")
        if max_t is not None:
            all_max.append(float(max_t))

        for w in KNOWN_WARNINGS:
            wcount = d.get("warnings_triggered", {}).get(w, 0)
            stats.warnings[w] = stats.warnings.get(w, 0) + wcount

    if all_min:
        stats.outdoor_min_f = round(min(all_min), 1)
    if all_max:
        stats.outdoor_max_f = round(max(all_max), 1)

    return stats


def _fmt_duration(seconds: int) -> str:
    """Format seconds as 'Xh Ym'."""
    if seconds == 0:
        return "0m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def format_monthly_summary(stats: MonthlyStats) -> str:
    """Render a MonthlyStats as a human-readable string."""
    lines: list[str] = []
    lines.append(f"Monthly Summary — {stats.month}")
    lines.append("=" * 44)

    coverage = f"{stats.day_count}/{stats.days_in_month} days"
    lines.append(f"  Coverage     : {coverage}")
    lines.append(f"  Total runtime: {_fmt_duration(stats.total_furnace_s)}")
    lines.append(f"  Sessions     : {stats.session_count}")

    # Outdoor temp
    temp_parts: list[str] = []
    if stats.outdoor_avg_f is not None:
        temp_parts.append(f"avg {stats.outdoor_avg_f}°F")
    if stats.outdoor_min_f is not None:
        temp_parts.append(f"min {stats.outdoor_min_f}°F")
    if stats.outdoor_max_f is not None:
        temp_parts.append(f"max {stats.outdoor_max_f}°F")
    if temp_parts:
        lines.append(f"  Outdoor temp : {' / '.join(temp_parts)}")
    else:
        lines.append("  Outdoor temp : —")

    # Per-floor breakdown
    lines.append("")
    lines.append("  Per-floor runtime:")
    for floor in KNOWN_FLOORS:
        label = floor.replace("_", " ").title()
        runtime = stats.per_floor_s.get(floor, 0)
        sessions = stats.per_floor_sessions.get(floor, 0)
        lines.append(f"    {label:<10}: {_fmt_duration(runtime):>8}  ({sessions} calls)")

    # Warnings (only show non-zero)
    active_warnings = {k: v for k, v in stats.warnings.items() if v > 0}
    if active_warnings:
        lines.append("")
        lines.append("  Warnings triggered:")
        for w, count in active_warnings.items():
            lines.append(f"    {w}: {count}")

    return "\n".join(lines)
