"""Daily summary generation and formatting for the HomeOps consumer service."""

from __future__ import annotations

from typing import Any

from constants import _FLOOR_ENTITIES, CLIMATE_ENTITIES
from utils import utc_ts


def emit_daily_summary(daily_state: dict[str, Any], date_str: str) -> dict[str, Any]:
    """
    Build a furnace_daily_summary.v1 event from accumulated daily state.
    daily_state keys:
      - furnace_runtime_s: int (total furnace on-time for the day)
      - session_count: int
      - floor_runtime_s: dict {entity_id: int seconds}
      - per_floor_session_count: dict {entity_id: int}
      - outdoor_temps: list of float
      - warnings_triggered: dict {warning_type: int}
    Returns the event dict.
    """
    outdoor_temps: list[float] = daily_state.get("outdoor_temps") or []
    outdoor_temp_min_f: float | None = min(outdoor_temps) if outdoor_temps else None
    outdoor_temp_max_f: float | None = max(outdoor_temps) if outdoor_temps else None
    outdoor_temp_avg_f: float | None = (
        round(sum(outdoor_temps) / len(outdoor_temps), 1) if outdoor_temps else None
    )

    per_floor_runtime_s: dict[str, int] = {}
    per_floor_session_count: dict[str, int] = {}
    for entity_id, floor_name in _FLOOR_ENTITIES.items():
        per_floor_runtime_s[floor_name] = daily_state.get("floor_runtime_s", {}).get(entity_id, 0)
        per_floor_session_count[floor_name] = daily_state.get("per_floor_session_count", {}).get(
            entity_id, 0
        )

    per_floor_setpoint_samples: dict[str, list] = (
        daily_state.get("per_floor_setpoint_samples") or {}
    )
    per_floor_avg_setpoint_f: dict[str, float | None] = {}
    for entity_id, floor_name in CLIMATE_ENTITIES.items():
        samples = per_floor_setpoint_samples.get(entity_id) or []
        per_floor_avg_setpoint_f[floor_name] = (
            round(sum(samples) / len(samples), 1) if samples else None
        )

    warnings_triggered: dict[str, int] = dict(
        daily_state.get(
            "warnings_triggered",
            {
                "floor_2_long_call": 0,
                "floor_2_escalation": 0,
                "floor_no_response": 0,
                "zone_slow_to_heat": 0,
                "observer_silence": 0,
                "setpoint_miss": 0,
            },
        )
    )

    return {
        "schema": "homeops.consumer.furnace_daily_summary.v1",
        "source": "consumer.v1",
        "ts": utc_ts(),
        "data": {
            "date": date_str,
            "total_furnace_runtime_s": daily_state.get("furnace_runtime_s", 0),
            "session_count": daily_state.get("session_count", 0),
            "per_floor_runtime_s": per_floor_runtime_s,
            "outdoor_temp_min_f": outdoor_temp_min_f,
            "outdoor_temp_max_f": outdoor_temp_max_f,
            "outdoor_temp_avg_f": outdoor_temp_avg_f,
            "per_floor_session_count": per_floor_session_count,
            "per_floor_avg_setpoint_f": per_floor_avg_setpoint_f,
            "warnings_triggered": warnings_triggered,
        },
    }


def emit_floor_daily_summaries(daily_state: dict[str, Any], date_str: str) -> list[dict[str, Any]]:
    """
    Build a list of floor_daily_summary.v1 events — one per floor — from accumulated daily state.

    Each event summarises completed floor heating calls for the day:
    - total_calls: number of completed floor_call_ended.v1 events
    - total_runtime_s: sum of all call durations in seconds
    - avg_duration_s: mean call duration (null if no calls)
    - max_duration_s: longest single call duration (null if no calls)
    - outdoor_temp_avg_f: day's average outdoor temperature (from daily_state; null if no readings)

    Returns a list of 3 event dicts (floor_1, floor_2, floor_3), even for floors with zero calls.
    """
    outdoor_temps: list[float] = daily_state.get("outdoor_temps") or []
    outdoor_temp_avg_f: float | None = (
        round(sum(outdoor_temps) / len(outdoor_temps), 1) if outdoor_temps else None
    )

    events: list[dict[str, Any]] = []
    for entity_id, floor_name in _FLOOR_ENTITIES.items():
        total_calls: int = daily_state.get("per_floor_session_count", {}).get(entity_id, 0)
        total_runtime_s: int = daily_state.get("floor_runtime_s", {}).get(entity_id, 0)
        avg_duration_s: float | None = (
            round(total_runtime_s / total_calls, 1) if total_calls > 0 else None
        )
        max_duration_s: int | None = daily_state.get("per_floor_max_call_s", {}).get(entity_id)

        events.append(
            {
                "schema": "homeops.consumer.floor_daily_summary.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": {
                    "floor": floor_name,
                    "date": date_str,
                    "total_calls": total_calls,
                    "total_runtime_s": total_runtime_s,
                    "avg_duration_s": avg_duration_s,
                    "max_duration_s": max_duration_s,
                    "outdoor_temp_avg_f": outdoor_temp_avg_f,
                },
            }
        )
    return events


def format_daily_summary_message(data: dict[str, Any]) -> str:
    """
    Format a furnace_daily_summary.v1 event data dict as a human-readable Telegram message.

    Args:
        data: The ``data`` sub-dict from a ``furnace_daily_summary.v1`` event.

    Returns:
        A multi-line string suitable for sending via Telegram sendMessage.
    """
    date = data.get("date", "unknown")
    lines: list[str] = [f"📊 Daily Heating Summary — {date}"]

    # Outdoor temperature line (omit entirely if all None)
    t_min: float | None = data.get("outdoor_temp_min_f")
    t_max: float | None = data.get("outdoor_temp_max_f")
    t_avg: float | None = data.get("outdoor_temp_avg_f")
    if t_min is not None or t_max is not None or t_avg is not None:
        min_str = f"{round(t_min)}°F" if t_min is not None else "?°F"
        max_str = f"{round(t_max)}°F" if t_max is not None else "?°F"
        avg_str = f"{round(t_avg, 1)}°F" if t_avg is not None else "?°F"
        lines.append(f"🌡️ Outdoor temp: {min_str} – {max_str} (avg {avg_str})")

    lines.append("")

    # Total furnace runtime
    total_s: int = data.get("total_furnace_runtime_s", 0)
    total_h = total_s // 3600
    total_m = (total_s % 3600) // 60
    lines.append(f"⏱️ Total furnace runtime: {total_h}h {total_m}m")

    # Heating sessions
    total_sessions: int = data.get("session_count", 0)
    lines.append(f"🔥 Heating sessions: {total_sessions} total")

    per_floor_runtime: dict[str, int] = data.get("per_floor_runtime_s", {})
    per_floor_sessions: dict[str, int] = data.get("per_floor_session_count", {})
    floor_display_order: list[tuple[str, str]] = [
        ("floor_1", "Floor 1"),
        ("floor_2", "Floor 2"),
        ("floor_3", "Floor 3"),
    ]
    for floor_key, floor_label in floor_display_order:
        n = per_floor_sessions.get(floor_key, 0)
        runtime_s = per_floor_runtime.get(floor_key, 0)
        if n > 0:
            avg_s = runtime_s // n
            avg_m = avg_s // 60
            suffix = " ⚠️" if floor_key == "floor_2" and avg_s > 1800 else ""
            lines.append(f"  • {floor_label}: {n} sessions, {avg_m}m avg{suffix}")
        else:
            lines.append(f"  • {floor_label}: 0 sessions")

    lines.append("")

    # Warnings section
    warnings: dict[str, int] = data.get("warnings_triggered", {})
    total_warnings = sum(warnings.values())
    if total_warnings == 0:
        lines.append("⚠️ Warnings today: None ✅")
    else:
        lines.append(f"⚠️ Warnings today: {total_warnings}")
        warning_display: list[tuple[str, str]] = [
            ("floor_2_long_call", "Floor-2 long call"),
            ("floor_2_escalation", "Floor-2 escalation 🚨"),
            ("floor_no_response", "Floor no-response"),
            ("zone_slow_to_heat", "Slow to heat"),
            ("setpoint_miss", "Setpoint miss"),
            ("observer_silence", "Observer silence"),
        ]
        for key, label in warning_display:
            count = warnings.get(key, 0)
            if count > 0:
                lines.append(f"  • {label}: {count}")

    return "\n".join(lines)
