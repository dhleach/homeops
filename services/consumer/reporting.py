"""Daily summary generation and formatting for the HomeOps consumer service."""

from constants import _FLOOR_ENTITIES
from utils import utc_ts


def emit_daily_summary(daily_state: dict, date_str: str) -> dict:
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
    outdoor_temps = daily_state.get("outdoor_temps") or []
    outdoor_temp_min_f = min(outdoor_temps) if outdoor_temps else None
    outdoor_temp_max_f = max(outdoor_temps) if outdoor_temps else None
    outdoor_temp_avg_f = (
        round(sum(outdoor_temps) / len(outdoor_temps), 1) if outdoor_temps else None
    )

    per_floor_runtime_s = {}
    per_floor_session_count = {}
    for entity_id, floor_name in _FLOOR_ENTITIES.items():
        per_floor_runtime_s[floor_name] = daily_state.get("floor_runtime_s", {}).get(entity_id, 0)
        per_floor_session_count[floor_name] = daily_state.get("per_floor_session_count", {}).get(
            entity_id, 0
        )

    warnings_triggered = dict(
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
            "warnings_triggered": warnings_triggered,
        },
    }


def format_daily_summary_message(data: dict) -> str:
    """
    Format a furnace_daily_summary.v1 event data dict as a human-readable Telegram message.

    Args:
        data: The ``data`` sub-dict from a ``furnace_daily_summary.v1`` event.

    Returns:
        A multi-line string suitable for sending via Telegram sendMessage.
    """
    date = data.get("date", "unknown")
    lines = [f"📊 Daily Heating Summary — {date}"]

    # Outdoor temperature line (omit entirely if all None)
    t_min = data.get("outdoor_temp_min_f")
    t_max = data.get("outdoor_temp_max_f")
    t_avg = data.get("outdoor_temp_avg_f")
    if t_min is not None or t_max is not None or t_avg is not None:
        min_str = f"{round(t_min)}°F" if t_min is not None else "?°F"
        max_str = f"{round(t_max)}°F" if t_max is not None else "?°F"
        avg_str = f"{round(t_avg, 1)}°F" if t_avg is not None else "?°F"
        lines.append(f"🌡️ Outdoor temp: {min_str} – {max_str} (avg {avg_str})")

    lines.append("")

    # Total furnace runtime
    total_s = data.get("total_furnace_runtime_s", 0)
    total_h = total_s // 3600
    total_m = (total_s % 3600) // 60
    lines.append(f"⏱️ Total furnace runtime: {total_h}h {total_m}m")

    # Heating sessions
    total_sessions = data.get("session_count", 0)
    lines.append(f"🔥 Heating sessions: {total_sessions} total")

    per_floor_runtime = data.get("per_floor_runtime_s", {})
    per_floor_sessions = data.get("per_floor_session_count", {})
    floor_display_order = [
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
    warnings = data.get("warnings_triggered", {})
    total_warnings = sum(warnings.values())
    if total_warnings == 0:
        lines.append("⚠️ Warnings today: None ✅")
    else:
        lines.append(f"⚠️ Warnings today: {total_warnings}")
        warning_display = [
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
