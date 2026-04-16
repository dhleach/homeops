#!/usr/bin/env python3
"""
HVAC context summarizer for LLM input.

Reads state/consumer/state.json and state/consumer/events.jsonl and produces
a compact, structured plain-text summary of recent HVAC behavior suitable for
feeding into an LLM context window.

Usage
-----
Default (48h lookback, standard paths):
    python3 hvac_context.py

Custom lookback window:
    python3 hvac_context.py --hours 24

Override file paths (useful for testing):
    python3 hvac_context.py --state /path/to/state.json --events /path/to/events.jsonl

Write to file:
    python3 hvac_context.py --output /tmp/hvac_context.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Add insights rules to path (insights/ is a sibling of consumer/)
sys.path.insert(0, str(Path(__file__).parent.parent / "insights"))

from rules.efficiency_degradation import EfficiencyDegradationRule
from rules.heating_efficiency import HeatingEfficiencyRule
from rules.time_of_day_pattern import TimeOfDayPatternRule

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STATE_PATH = "state/consumer/state.json"
DEFAULT_EVENTS_PATH = "state/consumer/events.jsonl"
DEFAULT_LOOKBACK_HOURS = 48

FLOOR_LABELS = {
    "floor_1": "Floor 1",
    "floor_2": "Floor 2",
    "floor_3": "Floor 3",
}

ZONE_TO_FLOOR = {
    "climate.floor_1_thermostat": "floor_1",
    "climate.floor_2_thermostat": "floor_2",
    "climate.floor_3_thermostat": "floor_3",
}

RELEVANT_SCHEMAS = {
    "homeops.consumer.heating_session_ended.v1",
    "homeops.consumer.furnace_daily_summary.v1",
    "homeops.consumer.floor_daily_summary.v1",
    "homeops.consumer.floor_2_long_call_warning.v1",
    "homeops.consumer.furnace_short_call_warning.v1",
    "homeops.consumer.floor_not_responding.v1",
    "homeops.consumer.anomaly_detected.v1",
    "homeops.consumer.observer_silence_alert.v1",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601 timestamp to UTC-aware datetime."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _fmt_duration(seconds: int | float | None) -> str:
    """Format seconds as 'Xh Ym' or 'Zm s'."""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


def _fmt_temp(val: float | int | None) -> str:
    if val is None:
        return "—"
    return f"{val:.0f}°F"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_state(path: str) -> dict[str, Any]:
    """Load and return the consumer state.json."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        return json.load(f)


def load_events(path: str, since: datetime) -> list[dict[str, Any]]:
    """
    Load derived events from events.jsonl that are:
    - within the lookback window (ts >= since), OR
    - daily summaries from the day before the window (for yesterday comparison)
    """
    p = Path(path)
    if not p.exists():
        return []

    # Also grab daily summaries from the previous day for comparison
    prev_day = (since - timedelta(days=1)).date().isoformat()

    events: list[dict[str, Any]] = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            schema = evt.get("schema", "")
            if schema not in RELEVANT_SCHEMAS:
                continue
            ts_str = evt.get("ts", "")
            if not ts_str:
                continue
            try:
                ts = _parse_ts(ts_str)
            except ValueError:
                continue
            # Include events in lookback window
            if ts >= since:
                events.append(evt)
                continue
            # Also include yesterday's daily summaries for context
            if schema in (
                "homeops.consumer.furnace_daily_summary.v1",
                "homeops.consumer.floor_daily_summary.v1",
            ):
                evt_date = evt.get("data", {}).get("date", "")
                if evt_date == prev_day:
                    events.append(evt)

    return events


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_current_conditions(state: dict[str, Any]) -> str:
    """Build the 'Current Conditions' section from state.json."""
    lines = ["CURRENT CONDITIONS"]

    # Furnace
    furnace_on = state.get("furnace_on_since") is not None
    furnace_str = "ON" if furnace_on else "OFF (idle)"
    lines.append(f"  Furnace: {furnace_str}")

    # Zone temps + setpoints
    climate = state.get("climate_state", {})
    for entity_id, floor in ZONE_TO_FLOOR.items():
        cs = climate.get(entity_id, {})
        temp = _fmt_temp(cs.get("current_temp"))
        sp = _fmt_temp(cs.get("setpoint"))
        action = cs.get("hvac_action", "unknown")
        label = FLOOR_LABELS[floor]
        lines.append(f"  {label}: {temp} (setpoint {sp}, {action})")

    # Outdoor temp is tracked in events; not directly in state.json
    return "\n".join(lines)


def _build_today_section(state: dict[str, Any], today_str: str) -> str:
    """Build today's runtime summary from state.daily_state."""
    daily = state.get("daily_state", {})
    session_count = daily.get("session_count", 0)
    furnace_runtime_s = daily.get("furnace_runtime_s", 0)
    floor_runtime = daily.get("floor_runtime_s", {})
    floor_sessions = daily.get("per_floor_session_count", {})
    warnings = daily.get("warnings_triggered", {})

    plural = "s" if session_count != 1 else ""
    lines = [f"TODAY ({today_str}) — {session_count} furnace session{plural}"]
    lines.append(f"  Total furnace runtime: {_fmt_duration(furnace_runtime_s)}")

    for floor, label in FLOOR_LABELS.items():
        entity = f"binary_sensor.{floor}_heating_call"
        rt = floor_runtime.get(floor, floor_runtime.get(entity, 0))
        sc = floor_sessions.get(floor, floor_sessions.get(entity, 0))
        lines.append(f"  {label}: {sc} sessions, {_fmt_duration(rt)} runtime")

    # Active warnings
    active_warnings = [k for k, v in warnings.items() if v > 0]
    if active_warnings:
        lines.append(f"  Warnings: {', '.join(active_warnings)}")
    else:
        lines.append("  Warnings: none")

    return "\n".join(lines)


def _build_daily_summary_section(
    events: list[dict[str, Any]], date_str: str, label: str
) -> str | None:
    """Build a summary section for a specific date from daily summary events."""
    furnace_summary = next(
        (
            e
            for e in events
            if e.get("schema") == "homeops.consumer.furnace_daily_summary.v1"
            and e.get("data", {}).get("date") == date_str
        ),
        None,
    )
    if not furnace_summary:
        return None

    d = furnace_summary["data"]
    session_count = d.get("session_count", 0)
    furnace_rt = d.get("total_furnace_runtime_s", 0)
    outdoor_avg = d.get("outdoor_temp_avg_f")
    outdoor_min = d.get("outdoor_temp_min_f")
    outdoor_max = d.get("outdoor_temp_max_f")
    warnings = d.get("warnings_triggered", {})

    temp_str = ""
    if outdoor_avg is not None:
        temp_str = f", outdoor avg {_fmt_temp(outdoor_avg)}"
        if outdoor_min is not None and outdoor_max is not None:
            mn, mx, avg = _fmt_temp(outdoor_min), _fmt_temp(outdoor_max), _fmt_temp(outdoor_avg)
            temp_str = f", outdoor {mn}–{mx} (avg {avg})"

    plural = "s" if session_count != 1 else ""
    lines = [f"{label} ({date_str}) — {session_count} furnace session{plural}{temp_str}"]
    lines.append(f"  Total furnace runtime: {_fmt_duration(furnace_rt)}")

    # Per-floor summaries
    floor_events = [
        e
        for e in events
        if e.get("schema") == "homeops.consumer.floor_daily_summary.v1"
        and e.get("data", {}).get("date") == date_str
    ]
    for floor, label_f in FLOOR_LABELS.items():
        fe = next((e for e in floor_events if e.get("data", {}).get("floor") == floor), None)
        if fe:
            fd = fe["data"]
            calls = fd.get("total_calls", 0)
            rt = fd.get("total_runtime_s", 0)
            avg_dur = fd.get("avg_duration_s")
            max_dur = fd.get("max_duration_s")
            avg_str = f", avg {_fmt_duration(avg_dur)}/session" if avg_dur else ""
            max_str = f", max {_fmt_duration(max_dur)}" if max_dur else ""
            lines.append(
                f"  {label_f}: {calls} sessions, {_fmt_duration(rt)} runtime{avg_str}{max_str}"
            )
        else:
            lines.append(f"  {label_f}: no data")

    # Warnings
    active_warnings = [k for k, v in warnings.items() if v > 0]
    if active_warnings:
        lines.append(f"  Warnings: {', '.join(f'{k} x{warnings[k]}' for k in active_warnings)}")

    return "\n".join(lines)


def _build_recent_sessions(events: list[dict[str, Any]], n: int = 8) -> str:
    """Build the recent heating sessions section."""
    sessions = [e for e in events if e.get("schema") == "homeops.consumer.heating_session_ended.v1"]
    # Sort by timestamp descending, take last n
    sessions.sort(key=lambda e: e.get("ts", ""), reverse=True)
    sessions = sessions[:n]

    if not sessions:
        return "RECENT HEATING SESSIONS\n  No sessions in lookback window"

    lines = [f"RECENT HEATING SESSIONS (last {len(sessions)})"]
    for e in sessions:
        d = e.get("data", {})
        ts = _parse_ts(e["ts"])
        ts_str = ts.strftime("%m-%d %H:%M UTC")
        dur = _fmt_duration(d.get("duration_s"))
        outdoor = _fmt_temp(d.get("outdoor_temp_f"))
        lines.append(f"  {ts_str}: {dur} (outdoor {outdoor})")

    return "\n".join(lines)


def _build_warnings_section(events: list[dict[str, Any]], since: datetime) -> str:
    """Build active warnings section from recent warning events."""
    warning_schemas = {
        "homeops.consumer.floor_2_long_call_warning.v1": "Floor 2 long call",
        "homeops.consumer.furnace_short_call_warning.v1": "Furnace short call",
        "homeops.consumer.floor_not_responding.v1": "Floor not responding",
        "homeops.consumer.anomaly_detected.v1": "Anomaly detected",
        "homeops.consumer.observer_silence_alert.v1": "Observer silence",
    }

    recent_warnings = [
        e for e in events if e.get("schema") in warning_schemas and _parse_ts(e["ts"]) >= since
    ]

    if not recent_warnings:
        return "RECENT WARNINGS\n  None in lookback window"

    lines = ["RECENT WARNINGS"]
    for e in sorted(recent_warnings, key=lambda x: x.get("ts", ""), reverse=True):
        schema = e.get("schema", "")
        label = warning_schemas.get(schema, schema)
        ts = _parse_ts(e["ts"]).strftime("%m-%d %H:%M UTC")
        data_str = ""
        d = e.get("data", {})
        if "duration_s" in d:
            data_str = f" ({_fmt_duration(d['duration_s'])})"
        elif "message" in d:
            data_str = f" — {d['message'][:60]}"
        lines.append(f"  {ts}: {label}{data_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def build_context(
    state_path: str = DEFAULT_STATE_PATH,
    events_path: str = DEFAULT_EVENTS_PATH,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
) -> str:
    """
    Build and return the full HVAC context summary string.

    Parameters
    ----------
    state_path:
        Path to state/consumer/state.json
    events_path:
        Path to state/consumer/events.jsonl
    lookback_hours:
        How many hours of events to include in the window
    """
    now = datetime.now(UTC)
    since = now - timedelta(hours=lookback_hours)
    today_str = now.date().isoformat()
    yesterday_str = (now.date() - timedelta(days=1)).isoformat()

    state = load_state(state_path)
    events = load_events(events_path, since)

    sections: list[str] = []

    # Header
    sections.append(
        f"=== HomeOps HVAC Context Summary ===\n"
        f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')} | "
        f"Lookback: {lookback_hours}h | "
        f"Events loaded: {len(events)}"
    )

    # Current conditions
    if state:
        sections.append(_build_current_conditions(state))
        sections.append(_build_today_section(state, today_str))
    else:
        sections.append("CURRENT CONDITIONS\n  state.json not available")

    # Yesterday (from daily summary events)
    yesterday_section = _build_daily_summary_section(events, yesterday_str, "YESTERDAY")
    if yesterday_section:
        sections.append(yesterday_section)

    # Older daily summaries in window
    known_dates = {today_str, yesterday_str}
    summary_dates: set[str] = set()
    for e in events:
        if e.get("schema") == "homeops.consumer.furnace_daily_summary.v1":
            d = e.get("data", {}).get("date", "")
            if d and d not in known_dates:
                summary_dates.add(d)

    for date_str in sorted(summary_dates, reverse=True):
        section = _build_daily_summary_section(events, date_str, date_str)
        if section:
            sections.append(section)

    # Recent heating sessions
    sections.append(_build_recent_sessions(events))

    # Warnings
    sections.append(_build_warnings_section(events, since))

    # Insights: heating efficiency, efficiency degradation, time-of-day patterns
    insights_section = _build_insights_section(events)
    if insights_section:
        sections.append(insights_section)

    return "\n\n".join(sections)


def _build_insights_section(events: list[dict[str, Any]]) -> str:
    """
    Run the three new insights rules against the full event list and return
    a formatted section string, or an empty string if no data is available.
    """
    lines: list[str] = []

    # --- Heating efficiency ---
    efficiency_rule = HeatingEfficiencyRule(history=events, min_sessions=5, lookback_days=14)
    efficiency_text = efficiency_rule.summary_text()
    if efficiency_text:
        lines.append(efficiency_text)

    # --- Efficiency degradation ---
    degradation_rule = EfficiencyDegradationRule(
        history=events, min_weeks=3, min_events_per_week=3, slope_threshold_s_per_week=60.0
    )
    degradation_findings = degradation_rule.check()
    if degradation_findings:
        lines.append("Efficiency Degradation Warnings:")
        for f in degradation_findings:
            d = f["data"]
            lines.append(
                f"  {d['floor']}: session duration trending up "
                f"+{d['slope_s_per_week']:.0f}s/week over {d['weeks_analysed']} weeks "
                f"({d['earliest_week']} → {d['latest_week']})"
            )

    # --- Time-of-day patterns ---
    # Use full event history as both baseline and window (48h lookback is already applied upstream)
    session_events = [
        e for e in events if e.get("schema") == "homeops.consumer.heating_session_ended.v1"
    ]
    if len(session_events) >= 8:
        # Split: older 75% as baseline, newer 25% as observation window
        split = max(1, len(session_events) * 3 // 4)
        baseline_events = session_events[:split]
        window_events = session_events[split:]
        tod_rule = TimeOfDayPatternRule(
            history=baseline_events, threshold_ratio=1.8, min_events=8, min_window_events=3
        )
        tod_findings = tod_rule.check(window_events)
        if tod_findings:
            lines.append("Time-of-Day Pattern Anomalies:")
            for f in tod_findings:
                d = f["data"]
                lines.append(
                    f"  {d['floor']} calling {d['ratio']}x more during {d['period']} "
                    f"than baseline ({d['observed_share']:.0%} vs "
                    f"{d['historical_share']:.0%} historically)"
                )

    if not lines:
        return ""

    return "INSIGHTS\n" + "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="HVAC context summarizer for LLM input")
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
        help=f"Lookback window in hours (default: {DEFAULT_LOOKBACK_HOURS})",
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE_PATH,
        help="Path to state.json",
    )
    parser.add_argument(
        "--events",
        default=DEFAULT_EVENTS_PATH,
        help="Path to events.jsonl",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write output to file instead of stdout",
    )
    args = parser.parse_args()

    context = build_context(
        state_path=args.state,
        events_path=args.events,
        lookback_hours=args.hours,
    )

    if args.output:
        Path(args.output).write_text(context)
    else:
        print(context)


if __name__ == "__main__":
    main()
