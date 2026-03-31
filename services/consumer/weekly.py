"""Week-over-week comparison for furnace daily summary data.

Reads ``furnace_daily_summary.v1`` events from a JSONL file and computes
this-week vs last-week statistics.

  - "This week"  = the last 7 days for which data exists in the file
  - "Last week"  = the 7 days immediately before that window
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WeeklyStats:
    """Aggregated statistics for a single week."""

    day_count: int = 0
    total_furnace_s: int = 0
    session_count: int = 0
    # Per-floor total runtime (seconds) across all days in the window
    floor_total_s: dict[str, int] = field(default_factory=dict)

    @property
    def floor_avg_daily_s(self) -> dict[str, float]:
        """Average daily runtime per floor (seconds).  Returns 0.0 if no days."""
        if self.day_count == 0:
            return {}
        return {floor: total / self.day_count for floor, total in self.floor_total_s.items()}


@dataclass
class WeeklyComparison:
    """Container holding stats for this week and last week."""

    this_week: WeeklyStats
    last_week: WeeklyStats
    # Ordered floor keys as they appear in the data
    floors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUMMARY_SCHEMA = "homeops.consumer.furnace_daily_summary.v1"


def pct_change(last: float, this: float) -> float | None:
    """Return the percentage change from *last* to *this*.

    Returns ``None`` when *last* is 0 (division by zero).
    """
    if last == 0:
        return None
    return (this - last) / last * 100.0


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def load_daily_summaries(events_file: str) -> list[dict]:
    """Read JSONL file and return only ``furnace_daily_summary.v1`` events.

    Malformed JSON lines are silently skipped.
    """
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


def compute_weekly_comparison(events: list[dict]) -> WeeklyComparison | None:
    """Compute week-over-week comparison from a list of daily summary events.

    Args:
        events: List of ``furnace_daily_summary.v1`` event dicts (order doesn't matter).

    Returns:
        A :class:`WeeklyComparison` or ``None`` if fewer than 7 days of data.
    """
    if not events:
        return None

    # Sort by date string (ISO format sorts lexicographically)
    sorted_events = sorted(events, key=lambda e: e.get("data", {}).get("date", ""))
    # Collect unique dates
    seen_dates: list[str] = []
    for evt in sorted_events:
        d = evt.get("data", {}).get("date", "")
        if d and d not in seen_dates:
            seen_dates.append(d)

    if len(seen_dates) < 7:
        return None

    this_week_dates = set(seen_dates[-7:])
    last_week_dates = set(seen_dates[-14:-7])  # may be empty or partial

    # Discover all floor keys present in the data
    floors_ordered = ["floor_1", "floor_2", "floor_3"]
    floors_seen: set[str] = set()
    for evt in sorted_events:
        floors_seen.update(evt.get("data", {}).get("per_floor_runtime_s", {}).keys())
    # Keep only floors present in the data, in canonical order
    floors = [f for f in floors_ordered if f in floors_seen]
    # Add any extra floors not in canonical order
    for f in sorted(floors_seen):
        if f not in floors:
            floors.append(f)

    def _aggregate(dates: set[str]) -> WeeklyStats:
        stats = WeeklyStats(
            day_count=0,
            total_furnace_s=0,
            session_count=0,
            floor_total_s={f: 0 for f in floors},
        )
        for evt in sorted_events:
            d = evt.get("data", {}).get("date", "")
            if d not in dates:
                continue
            data = evt.get("data", {})
            stats.day_count += 1
            stats.total_furnace_s += data.get("total_furnace_runtime_s", 0)
            stats.session_count += data.get("session_count", 0)
            per_floor = data.get("per_floor_runtime_s", {})
            for floor in floors:
                stats.floor_total_s[floor] = stats.floor_total_s.get(floor, 0) + per_floor.get(
                    floor, 0
                )
        return stats

    return WeeklyComparison(
        this_week=_aggregate(this_week_dates),
        last_week=_aggregate(last_week_dates),
        floors=floors,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_FLOOR_LABELS = {
    "floor_1": "Floor 1",
    "floor_2": "Floor 2",
    "floor_3": "Floor 3",
}


def _fmt_hm(seconds: float) -> str:
    """Format seconds as '2h 15m' or '45m'."""
    total_m = round(seconds / 60)
    h = total_m // 60
    m = total_m % 60
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _fmt_pct(pct: float | None) -> str:
    """Format a percentage change string like '+12%' or '-5%' or 'n/a'."""
    if pct is None:
        return "n/a"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{round(pct)}%"


def format_weekly_comparison(result: WeeklyComparison) -> str:
    """Format a :class:`WeeklyComparison` as a human-readable string."""
    lines: list[str] = []

    tw = result.this_week
    lw = result.last_week

    # Header
    lines.append("Weekly Comparison (this week vs last week)")
    if lw.day_count < 7:
        lines.append(
            f"  ⚠️  Last week has only {lw.day_count} day(s) of data — comparison may be skewed"
        )
    lines.append("")

    # --- Total furnace runtime ---
    tw_rt = _fmt_hm(tw.total_furnace_s)
    lw_rt = _fmt_hm(lw.total_furnace_s)
    pct = pct_change(lw.total_furnace_s, tw.total_furnace_s)
    lines.append(f"  Total furnace runtime:  {lw_rt:<10}  →  {tw_rt:<10}  ({_fmt_pct(pct)})")

    # --- Session count ---
    pct_s = pct_change(lw.session_count, tw.session_count)
    lines.append(
        f"  Session count:          {lw.session_count:<10}  →  "
        f"{tw.session_count:<10}  ({_fmt_pct(pct_s)})"
    )

    lines.append("")

    # --- Per-floor avg daily runtime ---
    tw_floor = tw.floor_avg_daily_s
    lw_floor = lw.floor_avg_daily_s

    for floor in result.floors:
        label = _FLOOR_LABELS.get(floor, floor.replace("_", " ").title())
        tw_s = tw_floor.get(floor, 0.0)
        lw_s = lw_floor.get(floor, 0.0)
        pct_f = pct_change(lw_s, tw_s)
        tw_str = _fmt_hm(tw_s)
        lw_str = _fmt_hm(lw_s)
        flag = ""
        # Floor 2 is the overheating risk floor — flag any increase
        if floor == "floor_2" and pct_f is not None and pct_f > 0:
            flag = "  ← watch this ⚠️"
        lines.append(
            f"  {label} avg daily:      {lw_str:<10}  →  {tw_str:<10}  ({_fmt_pct(pct_f)}){flag}"
        )

    return "\n".join(lines)
