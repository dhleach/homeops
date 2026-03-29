#!/usr/bin/env python3
import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from dateutil.parser import isoparse

# Add insights rules to path for floor_no_response rule
sys.path.insert(0, str(Path(__file__).parent.parent / "insights"))

STATE_FILE = Path("state/consumer/state.json")


def utc_ts():
    return datetime.now(UTC).isoformat()


def _get_version() -> str:
    """Return the current git version as <short_hash>-<YYYY-MM-DD>, or "unknown" if unavailable."""
    try:
        import subprocess as _subprocess

        return (
            _subprocess.check_output(
                ["git", "-C", str(Path(__file__).parent), "log", "-1", "--format=%h-%as"],
                stderr=_subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def follow(path: str, timeout_s: float = 60.0):
    """Yield new lines as they are appended to a file, or yield None on timeout."""
    import select as _select

    with open(path, encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        while True:
            ready, _, _ = _select.select([f], [], [], timeout_s)
            if ready:
                line = f.readline()
                if line:
                    yield line.rstrip("\n")
            else:
                # Timeout — no new events. Yield None so caller can do periodic checks.
                yield None


def append_jsonl(path: str, obj: dict):
    # Shared helper so all derived events are emitted in consistent JSONL format.
    line = json.dumps(obj)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def last_furnace_on_since(path: str):
    """
    Look back through the observer log to recover whether the furnace is currently 'on'
    and when that 'on' session started (based on the last off->on event).
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    # Reverse scan: the most recent furnace event determines whether a session is active.
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("schema") != "homeops.observer.state_changed.v1":
            continue
        data = evt.get("data") or {}
        if data.get("entity_id") != "binary_sensor.furnace_heating":
            continue
        old_state = data.get("old_state")
        new_state = data.get("new_state")
        ts_str = evt.get("ts")
        if old_state == "off" and new_state == "on" and ts_str:
            try:
                return isoparse(ts_str)
            except Exception:
                return None
        # If the last furnace event is an "off", we're not in a session
        return None

    return None


_FLOOR_ENTITIES = {
    "binary_sensor.floor_1_heating_call": "floor_1",
    "binary_sensor.floor_2_heating_call": "floor_2",
    "binary_sensor.floor_3_heating_call": "floor_3",
}

_ZONE_TO_FLOOR_ENTITY = {v: k for k, v in _FLOOR_ENTITIES.items()}

_ZONE_TO_CLIMATE_ENTITY = {
    "floor_1": "climate.floor_1_thermostat",
    "floor_2": "climate.floor_2_thermostat",
    "floor_3": "climate.floor_3_thermostat",
}

CLIMATE_ENTITIES = {
    "climate.floor_1_thermostat": "floor_1",
    "climate.floor_2_thermostat": "floor_2",
    "climate.floor_3_thermostat": "floor_3",
}

# Per-floor thresholds for the slow-to-heat warning (seconds).
# Overridable via env vars: SLOW_TO_HEAT_THRESHOLD_FLOOR1_S / FLOOR2_S / FLOOR3_S.
SLOW_TO_HEAT_THRESHOLDS_S: dict[str, int] = {
    "floor_1": int(os.environ.get("SLOW_TO_HEAT_THRESHOLD_FLOOR1_S", "900")),  # 15 min
    "floor_2": int(os.environ.get("SLOW_TO_HEAT_THRESHOLD_FLOOR2_S", "1800")),  # 30 min
    "floor_3": int(os.environ.get("SLOW_TO_HEAT_THRESHOLD_FLOOR3_S", "600")),  # 10 min
}


def process_floor_event(
    entity_id, old_state, new_state, ts, ts_str, floor_on_since, floor_2_warn_sent
):
    """
    Process a floor heating-call state change.

    Returns (events, updated_floor_on_since, updated_floor_2_warn_sent).
    events is a list of derived event dicts (0 or 1 items).
    """
    floor_key = _FLOOR_ENTITIES.get(entity_id)
    if floor_key is None:
        return [], floor_on_since, floor_2_warn_sent

    events = []
    floor_on_since = dict(floor_on_since)  # avoid mutating caller's dict

    if old_state == "off" and new_state == "on":
        floor_on_since[entity_id] = ts
        events.append(
            {
                "schema": "homeops.consumer.floor_call_started.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": {
                    "floor": floor_key,
                    "started_at": ts_str,
                    "entity_id": entity_id,
                },
            }
        )
        if floor_key == "floor_2":
            floor_2_warn_sent = False

    if old_state == "on" and new_state == "off":
        duration_s = None
        started = floor_on_since.get(entity_id)
        if started and ts:
            duration_s = int((ts - started).total_seconds())
        floor_on_since[entity_id] = None
        events.append(
            {
                "schema": "homeops.consumer.floor_call_ended.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": {
                    "floor": floor_key,
                    "ended_at": ts_str,
                    "entity_id": entity_id,
                    "duration_s": duration_s,
                },
            }
        )

    return events, floor_on_since, floor_2_warn_sent


def process_furnace_event(entity_id, old_state, new_state, ts, ts_str, furnace_on_since):
    """
    Process a furnace heating state change.

    Returns (events, updated_furnace_on_since).
    events is a list of derived event dicts (0 or 1 items).
    """
    events = []

    if old_state == "off" and new_state == "on":
        furnace_on_since = ts
        events.append(
            {
                "schema": "homeops.consumer.heating_session_started.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": {
                    "started_at": ts_str,
                    "entity_id": entity_id,
                },
            }
        )

    if old_state == "on" and new_state == "off":
        duration_s = None
        if furnace_on_since and ts:
            duration_s = int((ts - furnace_on_since).total_seconds())
        furnace_on_since = None
        events.append(
            {
                "schema": "homeops.consumer.heating_session_ended.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": {
                    "ended_at": ts_str,
                    "entity_id": entity_id,
                    "duration_s": duration_s,
                },
            }
        )

    return events, furnace_on_since


def process_climate_event(
    entity_id,
    attributes,
    ts_str,
    climate_state,
    new_state=None,
    floor_on_since=None,
    daily_state=None,
):
    """
    Process a climate entity state_changed event.

    Emits up to 3 events when setpoint, current_temp, or hvac mode/action changes.
    Also emits zone_time_to_temp.v1 when setpoint is crossed during a tracked heating session,
    and zone_overshoot.v1 when a heating session ends after setpoint was reached.

    climate_state is a dict keyed by entity_id with previous known values.
    new_state is the top-level HA state (e.g. "heat", "off", "cool") used as hvac_mode.
    floor_on_since is passed through from main() for other_zones_calling computation.
    daily_state is passed through from main() for outdoor_temp_f lookup.

    Returns (events, updated_climate_state).
    """
    zone = CLIMATE_ENTITIES.get(entity_id)
    if zone is None:
        return [], climate_state

    if not attributes:
        return [], climate_state

    if floor_on_since is None:
        floor_on_since = {}
    if daily_state is None:
        daily_state = {}

    setpoint = attributes.get("temperature")
    current_temp = attributes.get("current_temperature")
    hvac_mode = new_state
    hvac_action = attributes.get("hvac_action")

    prev = climate_state.get(entity_id) or {}
    events = []

    common = {
        "entity_id": entity_id,
        "zone": zone,
        "ts": ts_str,
        "hvac_mode": hvac_mode,
        "hvac_action": hvac_action,
        "setpoint": setpoint,
        "current_temp": current_temp,
    }

    # Parse event timestamp for session duration tracking.
    ts = None
    if ts_str:
        try:
            ts = isoparse(ts_str)
        except Exception:
            pass

    prev_hvac_action = prev.get("hvac_action")
    prev_current_temp = prev.get("current_temp")

    # Load heating session state persisted from the previous call.
    heating_start_temp = prev.get("heating_start_temp")
    heating_start_ts = prev.get("heating_start_ts")
    setpoint_reached_ts = prev.get("setpoint_reached_ts")
    setpoint_reached_temp = prev.get("setpoint_reached_temp")
    post_setpoint_temps = list(prev.get("post_setpoint_temps") or [])
    heating_start_other_zones = prev.get("heating_start_other_zones")
    setpoint_changed_during_heating = prev.get("setpoint_changed_during_heating", False)
    session_temps = list(prev.get("session_temps") or [])
    slow_to_heat_sent = prev.get("slow_to_heat_sent", False)

    # Detect heating session start: hvac_action transitions TO "heating".
    if prev_hvac_action != "heating" and hvac_action == "heating":
        heating_start_temp = current_temp
        heating_start_ts = ts
        setpoint_reached_ts = None
        setpoint_reached_temp = None
        post_setpoint_temps = []
        setpoint_changed_during_heating = False
        session_temps = []
        slow_to_heat_sent = False
        this_floor_entity = _ZONE_TO_FLOOR_ENTITY.get(zone)
        heating_start_other_zones = [
            k for k, v in floor_on_since.items() if v is not None and k != this_floor_entity
        ]

    if setpoint is not None and setpoint != prev.get("setpoint"):
        events.append(
            {
                "schema": "homeops.consumer.thermostat_setpoint_changed.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": common,
            }
        )
        if prev_hvac_action == "heating" and hvac_action == "heating":
            setpoint_changed_during_heating = True

    if current_temp is not None and current_temp != prev.get("current_temp"):
        events.append(
            {
                "schema": "homeops.consumer.thermostat_current_temp_updated.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": common,
            }
        )
        # Track all temp readings during heating for closest_temp computation.
        if hvac_action == "heating":
            session_temps.append(current_temp)

    if (hvac_mode is not None and hvac_mode != prev.get("hvac_mode")) or (
        hvac_action is not None and hvac_action != prev.get("hvac_action")
    ):
        events.append(
            {
                "schema": "homeops.consumer.thermostat_mode_changed.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": common,
            }
        )
        if prev_hvac_action == "heating" and hvac_action == "heating":
            setpoint_changed_during_heating = True

    # Setpoint reached: prev was heating and temp just crossed setpoint from below.
    setpoint_just_reached = False
    if (
        prev_hvac_action == "heating"
        and current_temp is not None
        and setpoint is not None
        and current_temp >= setpoint
        and (prev_current_temp is None or prev_current_temp < setpoint)
    ):
        events.append(
            {
                "schema": "homeops.consumer.thermostat_setpoint_reached.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": common,
            }
        )

        # Emit zone_time_to_temp.v1 only when we have a tracked heating session start.
        if heating_start_ts is not None and heating_start_temp is not None:
            duration_s = int((ts - heating_start_ts).total_seconds()) if ts else 0
            degrees_gained = current_temp - heating_start_temp
            degrees_per_min = (
                round(degrees_gained / (duration_s / 60), 3) if duration_s > 0 else 0.0
            )
            this_floor_entity = _ZONE_TO_FLOOR_ENTITY.get(zone)
            other_zones_calling = [
                k for k, v in floor_on_since.items() if v is not None and k != this_floor_entity
            ]
            events.append(
                {
                    "schema": "homeops.consumer.zone_time_to_temp.v1",
                    "source": "consumer.v1",
                    "ts": utc_ts(),
                    "data": {
                        "entity_id": entity_id,
                        "zone": zone,
                        "start_temp": heating_start_temp,
                        "setpoint": setpoint,
                        "setpoint_delta": setpoint - heating_start_temp,
                        "duration_s": duration_s,
                        "end_temp": current_temp,
                        "degrees_gained": degrees_gained,
                        "degrees_per_min": degrees_per_min,
                        "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                        "other_zones_calling": other_zones_calling,
                    },
                }
            )

        setpoint_reached_ts = ts
        setpoint_reached_temp = current_temp
        post_setpoint_temps.append(current_temp)
        setpoint_just_reached = True

    # Track subsequent temperature readings after setpoint reached (for peak_temp).
    if (
        not setpoint_just_reached
        and prev.get("setpoint_reached_ts") is not None
        and hvac_action == "heating"
        and current_temp is not None
        and current_temp != prev_current_temp
    ):
        post_setpoint_temps.append(current_temp)

    # Detect heating session end: hvac_action transitions FROM "heating".
    if prev_hvac_action == "heating" and hvac_action != "heating":
        if setpoint_reached_ts is not None:
            overshoot_s = (
                int((ts - setpoint_reached_ts).total_seconds()) if ts and setpoint_reached_ts else 0
            )
            peak_temp = max(post_setpoint_temps) if len(post_setpoint_temps) > 1 else None
            events.append(
                {
                    "schema": "homeops.consumer.zone_overshoot.v1",
                    "source": "consumer.v1",
                    "ts": utc_ts(),
                    "data": {
                        "entity_id": entity_id,
                        "zone": zone,
                        "start_temp": heating_start_temp,
                        "setpoint": setpoint,
                        "setpoint_delta": (
                            setpoint - heating_start_temp
                            if setpoint is not None and heating_start_temp is not None
                            else None
                        ),
                        "end_temp": current_temp,
                        "overshoot_s": overshoot_s,
                        "peak_temp": peak_temp,
                        "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                        "other_zones_calling": heating_start_other_zones or [],
                    },
                }
            )
        else:
            # Heating ended before setpoint was reached — emit setpoint miss event.
            if setpoint is not None and heating_start_temp is not None:
                duration_s = (
                    int((ts - heating_start_ts).total_seconds()) if ts and heating_start_ts else 0
                )
                closest_temp = max(session_temps) if session_temps else heating_start_temp
                setpoint_delta = setpoint - heating_start_temp
                # Guard 1: skip if zone was already at/above setpoint when heating started.
                # Guard 2: skip if closest_temp reached setpoint (fallback for missed edge).
                if setpoint_delta > 0 and closest_temp < setpoint:
                    events.append(
                        {
                            "schema": "homeops.consumer.zone_setpoint_miss.v1",
                            "source": "consumer.v1",
                            "ts": utc_ts(),
                            "data": {
                                "entity_id": entity_id,
                                "zone": zone,
                                "start_temp": heating_start_temp,
                                "setpoint": setpoint,
                                "setpoint_delta": setpoint_delta,
                                "duration_s": duration_s,
                                "closest_temp": closest_temp,
                                "delta": setpoint - closest_temp,
                                "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                                "other_zones_calling": heating_start_other_zones or [],
                                "likely_cause": (
                                    "thermostat_adjustment"
                                    if setpoint_changed_during_heating
                                    else "unknown"
                                ),
                            },
                        }
                    )
        # Clear all heating session state for this entity.
        heating_start_temp = None
        heating_start_ts = None
        setpoint_reached_ts = None
        setpoint_reached_temp = None
        post_setpoint_temps = []
        session_temps = []
        heating_start_other_zones = None
        setpoint_changed_during_heating = False
        slow_to_heat_sent = False

    # Slow-to-heat check: zone has been calling longer than threshold without reaching setpoint.
    if (
        hvac_action == "heating"
        and heating_start_ts is not None
        and setpoint_reached_ts is None
        and not slow_to_heat_sent
        and ts is not None
        and zone in SLOW_TO_HEAT_THRESHOLDS_S
    ):
        threshold_s = SLOW_TO_HEAT_THRESHOLDS_S[zone]
        elapsed_s = int((ts - heating_start_ts).total_seconds())
        if elapsed_s >= threshold_s:
            events.append(
                {
                    "schema": "homeops.consumer.zone_slow_to_heat_warning.v1",
                    "source": "consumer.v1",
                    "ts": utc_ts(),
                    "data": {
                        "zone": zone,
                        "entity_id": entity_id,
                        "elapsed_s": elapsed_s,
                        "threshold_s": threshold_s,
                        "start_temp": heating_start_temp,
                        "current_temp": current_temp,
                        "setpoint": setpoint,
                        "setpoint_delta": (
                            setpoint - heating_start_temp
                            if setpoint is not None and heating_start_temp is not None
                            else None
                        ),
                        "degrees_gained": (
                            current_temp - heating_start_temp
                            if current_temp is not None and heating_start_temp is not None
                            else None
                        ),
                        "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                    },
                }
            )
            slow_to_heat_sent = True

    updated_state = dict(climate_state)
    updated_state[entity_id] = {
        "setpoint": setpoint,
        "current_temp": current_temp,
        "hvac_mode": hvac_mode,
        "hvac_action": hvac_action,
        "heating_start_temp": heating_start_temp,
        "heating_start_ts": heating_start_ts,
        "setpoint_reached_ts": setpoint_reached_ts,
        "setpoint_reached_temp": setpoint_reached_temp,
        "post_setpoint_temps": post_setpoint_temps,
        "session_temps": session_temps,
        "heating_start_other_zones": heating_start_other_zones,
        "setpoint_changed_during_heating": setpoint_changed_during_heating,
        "slow_to_heat_sent": slow_to_heat_sent,
    }

    return events, updated_state


def process_outdoor_temp_event(entity_id, new_state, ts_str):
    """
    Process an outdoor temperature state change.

    Returns a list of derived event dicts (empty if the state is not a valid float).
    """
    if new_state in (None, "unavailable", "unknown", ""):
        return []
    try:
        temp_f = float(new_state)
    except (ValueError, TypeError):
        return []
    return [
        {
            "schema": "homeops.consumer.outdoor_temp_updated.v1",
            "source": "consumer.v1",
            "ts": utc_ts(),
            "data": {
                "entity_id": entity_id,
                "temperature_f": temp_f,
                "timestamp": ts_str,
            },
        }
    ]


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


def check_floor_2_warning(
    floor_on_since, floor_2_warn_sent, floor_2_warn_threshold_s, now_ts, climate_state=None
):
    """
    Check whether the floor-2 long-call warning should fire.

    Returns (warning_event_or_None, updated_floor_2_warn_sent).
    """
    if floor_2_warn_sent:
        return None, floor_2_warn_sent

    f2_entity = "binary_sensor.floor_2_heating_call"
    f2_started = floor_on_since.get(f2_entity)
    if f2_started is None:
        return None, floor_2_warn_sent

    elapsed_s = int((now_ts - f2_started).total_seconds())
    if elapsed_s < floor_2_warn_threshold_s:
        return None, floor_2_warn_sent

    f2_climate = (climate_state or {}).get("climate.floor_2_thermostat", {})
    current_temp = f2_climate.get("current_temp")
    setpoint = f2_climate.get("setpoint")

    warn_event = {
        "schema": "homeops.consumer.floor_2_long_call_warning.v1",
        "source": "consumer.v1",
        "ts": utc_ts(),
        "data": {
            "floor": "floor_2",
            "elapsed_s": elapsed_s,
            "threshold_s": floor_2_warn_threshold_s,
            "entity_id": f2_entity,
            "current_temp": current_temp,
            "setpoint": setpoint,
        },
    }
    return warn_event, True


def check_floor_2_escalation(
    long_call_count_today: int,
    floor_2_warn_threshold_s: int,
    climate_state: dict | None = None,
) -> dict | None:
    """
    Return a floor_2_long_call_escalation.v1 event if today's long-call count has
    reached the escalation threshold (>= 2), otherwise return None.

    This fires on the 2nd, 3rd, etc. long-call warning in the same calendar day so
    ongoing furnace issues stay visible.

    Args:
        long_call_count_today: Value of daily_state["warnings_triggered"]["floor_2_long_call"]
            *after* it has already been incremented for the current warning.
        floor_2_warn_threshold_s: The threshold used for long-call warnings (seconds).
        climate_state: Optional climate_state dict for current_temp / setpoint.

    Returns:
        An escalation event dict, or None if escalation should not fire.
    """
    if long_call_count_today < 2:
        return None

    f2_climate = (climate_state or {}).get("climate.floor_2_thermostat", {})
    return {
        "schema": "homeops.consumer.floor_2_long_call_escalation.v1",
        "source": "consumer.v1",
        "ts": utc_ts(),
        "data": {
            "floor": "floor_2",
            "long_call_count_today": long_call_count_today,
            "threshold_s": floor_2_warn_threshold_s,
            "current_temp": f2_climate.get("current_temp"),
            "setpoint": f2_climate.get("setpoint"),
        },
    }


def check_observer_silence(
    last_event_ts: datetime | None,
    observer_silence_sent: bool,
    threshold_s: int,
    now_ts: datetime,
) -> tuple[dict | None, bool]:
    """
    Check whether the observer has been silent longer than threshold_s.

    Returns (warning_event_or_None, updated_observer_silence_sent).
    Only fires once per silence episode (deduplicated via observer_silence_sent flag).
    """
    if observer_silence_sent:
        return None, observer_silence_sent

    if last_event_ts is None:
        return None, observer_silence_sent

    silence_s = int((now_ts - last_event_ts).total_seconds())
    if silence_s < threshold_s:
        return None, observer_silence_sent

    warn_event = {
        "schema": "homeops.consumer.observer_silence_warning.v1",
        "source": "consumer.v1",
        "ts": utc_ts(),
        "data": {
            "last_event_ts": last_event_ts.isoformat(),
            "silence_s": silence_s,
            "threshold_s": threshold_s,
        },
    }
    return warn_event, True


ZONE_TEMP_SNAPSHOT_INTERVAL_S = 300  # 5 minutes
ZONE_TEMP_SNAPSHOT_LOG = "state/consumer/zone_temps.jsonl"


def write_zone_temp_snapshot(
    climate_state: dict,
    daily_state: dict,
    snapshot_log: str = ZONE_TEMP_SNAPSHOT_LOG,
) -> bool:
    """Write a zone_temp_snapshot.v1 record if climate_state has at least one zone.

    Returns True if a snapshot was written, False otherwise.
    """
    if not climate_state:
        return False

    zones: dict = {}
    for _eid, zone_data in climate_state.items():
        zone_name = zone_data.get("zone")
        if not zone_name:
            continue
        current_temp = zone_data.get("current_temp")
        if current_temp is None:
            continue
        zones[zone_name] = {
            "current_temp": current_temp,
            "setpoint": zone_data.get("setpoint"),
            "hvac_action": zone_data.get("hvac_action"),
        }

    if not zones:
        return False

    record = {
        "schema": "homeops.consumer.zone_temp_snapshot.v1",
        "source": "consumer.v1",
        "ts": utc_ts(),
        "data": {
            "zones": zones,
            "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
        },
    }
    append_jsonl(snapshot_log, record)
    return True


def _empty_daily_state() -> dict:
    return {
        "furnace_runtime_s": 0,
        "session_count": 0,
        "floor_runtime_s": {},
        "per_floor_session_count": {eid: 0 for eid in _FLOOR_ENTITIES},
        "outdoor_temps": [],
        "last_outdoor_temp_f": None,
        "warnings_triggered": {
            "floor_2_long_call": 0,
            "floor_2_escalation": 0,
            "floor_no_response": 0,
            "zone_slow_to_heat": 0,
            "observer_silence": 0,
            "setpoint_miss": 0,
        },
    }


def _parse_dt(s: str | None):
    if s is None:
        return None
    try:
        return isoparse(s)
    except Exception:
        return None


def _save_state(
    floor_on_since: dict,
    furnace_on_since,
    climate_state: dict,
    daily_state: dict,
    *,
    state_file: Path | None = None,
) -> None:
    """Atomically persist consumer runtime state to disk."""

    def _dt(dt):
        return dt.isoformat() if dt is not None else None

    serialized_fos = {k: _dt(v) for k, v in floor_on_since.items()}

    serialized_cs: dict = {}
    for eid, es in climate_state.items():
        s = dict(es)
        s["heating_start_ts"] = _dt(s.get("heating_start_ts"))
        s["setpoint_reached_ts"] = _dt(s.get("setpoint_reached_ts"))
        serialized_cs[eid] = s

    payload = {
        "floor_on_since": serialized_fos,
        "furnace_on_since": _dt(furnace_on_since),
        "climate_state": serialized_cs,
        "daily_state": daily_state,
        "saved_at": utc_ts(),
    }
    sf = state_file or STATE_FILE
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.rename(sf)


def _load_state(*, state_file: Path | None = None) -> dict | None:
    """
    Load persisted consumer state from disk.

    Returns None on cold-start (file missing or older than 3720 s / 62 min).
    Returns the state dict when resuming from a recent restart.
    """
    sf = state_file or STATE_FILE
    if not sf.exists():
        return None
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return None
    saved_at_str = data.get("saved_at")
    if not saved_at_str:
        return None
    try:
        age_s = (datetime.now(UTC) - isoparse(saved_at_str)).total_seconds()
    except Exception:
        return None
    if age_s > 3720:
        return None
    return data


def _register_sigterm_handler(*, state_file: Path | None = None) -> None:
    """Register a SIGTERM handler that stamps shutdown_ts into the state file."""
    sf = state_file or STATE_FILE

    def _handler(signum, frame):
        state: dict = {}
        if sf.exists():
            try:
                state = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                pass
        state["shutdown_ts"] = utc_ts()
        try:
            sf.write_text(json.dumps(state), encoding="utf-8")
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)


_RESTART_CLEAR_SCHEMAS = frozenset(
    {
        "homeops.consumer.zone_setpoint_miss.v1",
        "homeops.consumer.zone_time_to_temp.v1",
    }
)


def _emit_derived(derived: dict, derived_log: str, fresh_restart: bool) -> bool:
    """
    Print + append a derived event; tag with across_restart when applicable.

    Returns the updated fresh_restart flag (cleared after the first full session).
    """
    if fresh_restart:
        derived["data"]["across_restart"] = True
    print(json.dumps(derived), flush=True)
    append_jsonl(derived_log, derived)
    if fresh_restart and derived.get("schema") in _RESTART_CLEAR_SCHEMAS:
        print(
            f"[{utc_ts()}] Cleared fresh_restart after first full heating session",
            flush=True,
        )
        return False
    return fresh_restart


def main():
    """Tail observer events and emit derived floor/furnace session events."""
    path = os.environ.get("EVENT_LOG", "state/observer/events.jsonl")
    derived_log = os.environ.get("DERIVED_EVENT_LOG", "state/consumer/events.jsonl")
    print(f"[{utc_ts()}] Derived log: {derived_log}", flush=True)
    version = _get_version()
    print(f"[{utc_ts()}] Consumer version: {version}", flush=True)
    os.makedirs("state/consumer", exist_ok=True)
    with open("state/consumer/version.txt", "w", encoding="utf-8") as _vf:
        _vf.write(version + "\n")
    print(f"[{utc_ts()}] Consumer following: {path}", flush=True)

    floor_2_warn_threshold_s = int(os.environ.get("FLOOR_2_WARN_THRESHOLD_S", "2700"))  # 45 min
    print(f"[{utc_ts()}] Floor-2 warning threshold: {floor_2_warn_threshold_s}s", flush=True)
    print(
        f"[{utc_ts()}] Slow-to-heat thresholds: "
        + ", ".join(f"{z}={t}s" for z, t in SLOW_TO_HEAT_THRESHOLDS_S.items()),
        flush=True,
    )
    observer_silence_threshold_s = int(
        os.environ.get("OBSERVER_SILENCE_THRESHOLD_S", "600")
    )  # 10 min
    print(f"[{utc_ts()}] Observer silence threshold: {observer_silence_threshold_s}s", flush=True)
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    _register_sigterm_handler()

    floor_entities = _FLOOR_ENTITIES
    floor_2_warn_sent = False  # reset each time floor 2 starts a new call
    last_observer_event_ts: datetime | None = None
    observer_silence_sent = False  # reset when a new event arrives after silence

    # Floor-not-responding rule (temp-based: zone calling > threshold with no temp rise).
    from rules.floor_no_response import FloorNoResponseRule  # noqa: PLC0415
    from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule  # noqa: PLC0415

    floor_no_response_rule = FloorNoResponseRule()

    # Furnace session anomaly rule — load baseline once at startup if available.
    _baseline_path = Path("state/consumer/baseline_constants.json")
    _baseline: dict = {}
    if _baseline_path.exists():
        try:
            _baseline = json.loads(_baseline_path.read_text(encoding="utf-8"))
            print(f"[{utc_ts()}] Loaded baseline from {_baseline_path}", flush=True)
        except Exception as _e:
            print(f"[{utc_ts()}] WARN: Could not load baseline: {_e}", flush=True)
    furnace_session_anomaly_rule = FurnaceSessionAnomalyRule(_baseline)

    # Attempt to resume from a recent state file; otherwise cold-start.
    saved = _load_state()
    fresh_restart = True  # always set on any restart (cold or resume)

    if saved is not None:
        fos_raw = saved.get("floor_on_since") or {}
        floor_on_since = {k: _parse_dt(v) for k, v in fos_raw.items()}
        for k in floor_entities:
            floor_on_since.setdefault(k, None)
        furnace_on_since = _parse_dt(saved.get("furnace_on_since"))
        raw_cs = saved.get("climate_state") or {}
        climate_state: dict = {}
        for eid, es in raw_cs.items():
            s = dict(es)
            s["heating_start_ts"] = _parse_dt(s.get("heating_start_ts"))
            s["setpoint_reached_ts"] = _parse_dt(s.get("setpoint_reached_ts"))
            climate_state[eid] = s
        daily_state = saved.get("daily_state") or _empty_daily_state()
        print(
            f"[{utc_ts()}] Resumed from state file (saved_at={saved.get('saved_at')})",
            flush=True,
        )
    else:
        # Cold-start: bootstrap furnace state from the observer log.
        furnace_on_since = last_furnace_on_since(path)
        if furnace_on_since:
            print(
                f"[{utc_ts()}] Bootstrapped furnace_on_since={furnace_on_since.isoformat()}",
                flush=True,
            )
        floor_on_since = {key: None for key in floor_entities.keys()}
        climate_state = {}
        daily_state = _empty_daily_state()

    current_date = datetime.now(UTC).strftime("%Y-%m-%d")
    last_snapshot_ts: datetime | None = None

    # Main stream loop: consume observer events and emit higher-level derived events.
    for line in follow(path):
        if line is None:
            # Timeout — no new events. Just run the in-flight check below.
            pass
        else:
            try:
                evt = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[{utc_ts()}] WARN: bad json line: {e}", flush=True)
                continue

            schema = evt.get("schema")
            # Ignore non-observer events if this file is shared with other producers.
            if schema != "homeops.observer.state_changed.v1":
                continue

            # Track last observer event time for silence watchdog.
            last_observer_event_ts = datetime.now(UTC)
            if observer_silence_sent:
                # New event arrived after a silence period — reset dedup flag.
                observer_silence_sent = False

            ts_str = evt.get("ts")
            data = evt.get("data", {})
            entity_id = data.get("entity_id")
            old_state = data.get("old_state")
            new_state = data.get("new_state")
            attributes = data.get("attributes") or {}

            # Always keep the simple print
            print(f"{ts_str} {schema} {entity_id}: {old_state} -> {new_state}", flush=True)

            try:
                ts = isoparse(ts_str) if ts_str else None
            except Exception:
                # Preserve processing even if one event has a malformed timestamp.
                ts = None

            # Date rollover: emit daily summary when the event date changes.
            if ts is not None:
                evt_date = ts.strftime("%Y-%m-%d")
                if current_date is None:
                    current_date = evt_date
                elif evt_date != current_date:
                    summary = emit_daily_summary(daily_state, current_date)
                    print(json.dumps(summary), flush=True)
                    append_jsonl(derived_log, summary)
                    if telegram_bot_token and telegram_chat_id:
                        import urllib.parse as _parse
                        import urllib.request as _urllib

                        tg_msg = format_daily_summary_message(summary["data"])
                        tg_url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                        tg_data = _parse.urlencode(
                            {"chat_id": telegram_chat_id, "text": tg_msg}
                        ).encode()
                        try:
                            _urllib.urlopen(tg_url, tg_data, timeout=10)
                        except Exception as e:
                            print(
                                f"[{utc_ts()}] WARN: Telegram daily summary failed: {e}",
                                flush=True,
                            )
                    else:
                        print(
                            f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                            " not set, skipping daily summary alert",
                            flush=True,
                        )
                    daily_state = _empty_daily_state()
                    current_date = evt_date

                    # --- Floor runtime anomaly detection ---
                    # Load prior daily summaries (exclude today to avoid circular reference).
                    from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule  # noqa: PLC0415

                    _prior_summaries: list[dict] = []
                    _summary_date = summary["data"]["date"]
                    if Path(derived_log).exists():
                        try:
                            with open(derived_log, encoding="utf-8") as _dlog:
                                for _line in _dlog:
                                    _line = _line.strip()
                                    if not _line:
                                        continue
                                    try:
                                        _evt = json.loads(_line)
                                    except json.JSONDecodeError:
                                        continue
                                    if (
                                        _evt.get("schema")
                                        == "homeops.consumer.furnace_daily_summary.v1"
                                        and _evt.get("data", {}).get("date") != _summary_date
                                    ):
                                        _prior_summaries.append(_evt)
                        except Exception as _e:
                            print(
                                f"[{utc_ts()}] WARN: Could not read derived log"
                                f" for anomaly check: {_e}",
                                flush=True,
                            )

                    _runtime_anomaly_rule = FloorRuntimeAnomalyRule(history=_prior_summaries)
                    _per_floor = summary["data"].get("per_floor_runtime_s", {})
                    for _floor, _floor_runtime_s in _per_floor.items():
                        for _anom_evt in _runtime_anomaly_rule.check_daily_runtime(
                            _floor, _floor_runtime_s, summary["data"]["date"]
                        ):
                            print(json.dumps(_anom_evt), flush=True)
                            append_jsonl(derived_log, _anom_evt)

            # Per-floor call sessions are derived from floor_* heating_call sensors.
            if entity_id in floor_entities:
                derived_events, floor_on_since, floor_2_warn_sent = process_floor_event(
                    entity_id, old_state, new_state, ts, ts_str, floor_on_since, floor_2_warn_sent
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.floor_call_started.v1":
                        zone = derived["data"]["floor"]
                        climate_eid = _ZONE_TO_CLIMATE_ENTITY.get(zone)
                        start_temp = None
                        if climate_eid:
                            start_temp = (climate_state.get(climate_eid) or {}).get("current_temp")
                        floor_no_response_rule.on_floor_call_started(
                            zone, ts or datetime.now(UTC), start_temp
                        )
                    if derived["schema"] == "homeops.consumer.floor_call_ended.v1":
                        d = derived["data"]
                        floor_no_response_rule.on_floor_call_ended(d["floor"])
                        eid = d["entity_id"]
                        if d.get("duration_s") is not None:
                            daily_state["floor_runtime_s"][eid] = (
                                daily_state["floor_runtime_s"].get(eid, 0) + d["duration_s"]
                            )
                        daily_state["per_floor_session_count"][eid] = (
                            daily_state["per_floor_session_count"].get(eid, 0) + 1
                        )
                _save_state(floor_on_since, furnace_on_since, climate_state, daily_state)

            # Outdoor temperature readings are passed through as-is from the sensor.
            if entity_id == "sensor.outdoor_temperature":
                for derived in process_outdoor_temp_event(entity_id, new_state, ts_str):
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    daily_state["outdoor_temps"].append(derived["data"]["temperature_f"])
                    daily_state["last_outdoor_temp_f"] = derived["data"]["temperature_f"]
                if new_state in (None, "unavailable", "unknown", ""):
                    print(
                        f"[{utc_ts()}] WARN: outdoor_temperature state unavailable, skipping",
                        flush=True,
                    )
                else:
                    try:
                        float(new_state)
                    except (ValueError, TypeError):
                        print(
                            f"[{utc_ts()}] WARN: outdoor_temperature non-numeric value"
                            f" {new_state!r}, skipping",
                            flush=True,
                        )
                # Always save on outdoor_temp event — this is the 62-min heartbeat write.
                _save_state(floor_on_since, furnace_on_since, climate_state, daily_state)

            # Thermostat climate entities: setpoint, current temp, and mode changes.
            if entity_id in CLIMATE_ENTITIES:
                derived_events, climate_state = process_climate_event(
                    entity_id,
                    attributes,
                    ts_str,
                    climate_state,
                    new_state,
                    floor_on_since=floor_on_since,
                    daily_state=daily_state,
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.zone_slow_to_heat_warning.v1":
                        daily_state["warnings_triggered"]["zone_slow_to_heat"] += 1
                        d = derived["data"]
                        zone_label = d["zone"].replace("_", " ").title()
                        elapsed_min = d["elapsed_s"] // 60
                        start_t = d["start_temp"]
                        curr_t = d["current_temp"]
                        sp = d["setpoint"]
                        away = (
                            round(sp - curr_t, 1) if sp is not None and curr_t is not None else None
                        )
                        away_str = f"{away}°" if away is not None else "?"
                        msg = (
                            f"⚠️ {zone_label} slow to heat!\n"
                            f"Calling for {elapsed_min} min — setpoint not reached yet.\n"
                            f"Start: {start_t}°F → Now: {curr_t}°F → Target: {sp}°F"
                            f" ({away_str} away)"
                        )
                        outdoor_t = d.get("outdoor_temp_f")
                        if outdoor_t is not None:
                            msg += f"\nOutdoor temp: {round(outdoor_t)}°F"
                        if telegram_bot_token and telegram_chat_id:
                            import urllib.parse as _parse
                            import urllib.request as _urllib

                            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                            data = _parse.urlencode(
                                {"chat_id": telegram_chat_id, "text": msg}
                            ).encode()
                            try:
                                _urllib.urlopen(url, data=data, timeout=10)
                            except Exception as e:
                                print(
                                    f"[{utc_ts()}] WARN: Telegram slow-to-heat alert failed: {e}",
                                    flush=True,
                                )
                        else:
                            print(
                                f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                                " not set, skipping slow-to-heat alert",
                                flush=True,
                            )
                    if derived["schema"] == "homeops.consumer.zone_setpoint_miss.v1":
                        daily_state["warnings_triggered"]["setpoint_miss"] += 1
                # Feed temperature updates to the floor-not-responding rule.
                zone = CLIMATE_ENTITIES.get(entity_id)
                current_temp = (attributes or {}).get("current_temperature")
                if zone and current_temp is not None:
                    floor_no_response_rule.on_temp_updated(zone, current_temp)
                _save_state(floor_on_since, furnace_on_since, climate_state, daily_state)

            # Whole-home heating sessions are derived from furnace on/off transitions.
            if entity_id == "binary_sensor.furnace_heating":
                derived_events, furnace_on_since = process_furnace_event(
                    entity_id, old_state, new_state, ts, ts_str, furnace_on_since
                )
                for derived in derived_events:
                    fresh_restart = _emit_derived(derived, derived_log, fresh_restart)
                    if derived["schema"] == "homeops.consumer.heating_session_ended.v1":
                        d = derived["data"]
                        if d.get("duration_s") is not None:
                            daily_state["furnace_runtime_s"] += d["duration_s"]
                        daily_state["session_count"] += 1
                        # Check for session duration anomalies.
                        _session_floor = d.get("floor")
                        _session_dur = d.get("duration_s")
                        _session_ts = d.get("ended_at") or derived["ts"]
                        for _anom in furnace_session_anomaly_rule.check_session(
                            _session_floor, _session_dur, _session_ts
                        ):
                            fresh_restart = _emit_derived(_anom, derived_log, fresh_restart)
                            _anom_data = _anom["data"]
                            if (
                                _anom["schema"]
                                == "homeops.consumer.heating_short_session_warning.v1"
                            ):
                                _anom_floor = _anom_data["floor"] or "unknown"
                                _anom_dur = _anom_data["duration_s"]
                                _anom_thr = _anom_data["threshold_s"]
                                _anom_msg = (
                                    f"⚡ Short furnace session on {_anom_floor}:"
                                    f" {_anom_dur}s (threshold: {_anom_thr}s)"
                                    " — possible short-cycling"
                                )
                                if telegram_bot_token and telegram_chat_id:
                                    import urllib.parse as _parse
                                    import urllib.request as _urllib

                                    _url = (
                                        f"https://api.telegram.org/bot{telegram_bot_token}"
                                        "/sendMessage"
                                    )
                                    _tdata = _parse.urlencode(
                                        {"chat_id": telegram_chat_id, "text": _anom_msg}
                                    ).encode()
                                    try:
                                        _urllib.urlopen(_url, _tdata, timeout=10)
                                    except Exception as _te:
                                        print(
                                            f"[{utc_ts()}] WARN: Telegram short-session"
                                            f" alert failed: {_te}",
                                            flush=True,
                                        )
                                else:
                                    print(
                                        f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or"
                                        " TELEGRAM_CHAT_ID not set, skipping short-session alert",
                                        flush=True,
                                    )
                            elif _anom[
                                "schema"
                            ] == "homeops.consumer.heating_long_session_warning.v1" and _anom_data[
                                "floor"
                            ] in ("floor_2", None):
                                _anom_floor = _anom_data["floor"] or "unknown"
                                _anom_dur = _anom_data["duration_s"]
                                _anom_thr = _anom_data["threshold_s"]
                                _anom_msg = (
                                    f"🔥 Long furnace session on {_anom_floor}:"
                                    f" {_anom_dur}s (threshold: {_anom_thr}s)"
                                    " — overheating risk"
                                )
                                if telegram_bot_token and telegram_chat_id:
                                    import urllib.parse as _parse
                                    import urllib.request as _urllib

                                    _url = (
                                        f"https://api.telegram.org/bot{telegram_bot_token}"
                                        "/sendMessage"
                                    )
                                    _tdata = _parse.urlencode(
                                        {"chat_id": telegram_chat_id, "text": _anom_msg}
                                    ).encode()
                                    try:
                                        _urllib.urlopen(_url, _tdata, timeout=10)
                                    except Exception as _te:
                                        print(
                                            f"[{utc_ts()}] WARN: Telegram long-session"
                                            f" alert failed: {_te}",
                                            flush=True,
                                        )
                                else:
                                    print(
                                        f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or"
                                        " TELEGRAM_CHAT_ID not set, skipping long-session alert",
                                        flush=True,
                                    )
                _save_state(floor_on_since, furnace_on_since, climate_state, daily_state)

        # In-flight floor-2 long-call check (runs on every event and on timeouts)
        warn_event, floor_2_warn_sent = check_floor_2_warning(
            floor_on_since,
            floor_2_warn_sent,
            floor_2_warn_threshold_s,
            datetime.now(UTC),
            climate_state,
        )
        if warn_event:
            daily_state["warnings_triggered"]["floor_2_long_call"] += 1
            fresh_restart = _emit_derived(warn_event, derived_log, fresh_restart)
            if telegram_bot_token and telegram_chat_id:
                import urllib.parse as _parse
                import urllib.request as _urllib

                elapsed_s = warn_event["data"]["elapsed_s"]
                current_temp = warn_event["data"].get("current_temp")
                setpoint = warn_event["data"].get("setpoint")
                temp_line = ""
                if current_temp is not None and setpoint is not None:
                    delta = abs(round(setpoint - current_temp))
                    temp_line = (
                        f"Current temp: {current_temp}°F → Setpoint: {setpoint}°F ({delta}° away)\n"
                    )
                msg = (
                    f"⚠️ Floor 2 has been calling for {elapsed_s // 60} min!\n"
                    f"{temp_line}"
                    f"Risk of furnace overheating (Code 4/7 limit trip).\n"
                    f"Consider lowering floor 2 thermostat manually."
                )
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                data = _parse.urlencode({"chat_id": telegram_chat_id, "text": msg}).encode()
                try:
                    _urllib.urlopen(url, data=data, timeout=10)
                except Exception as e:
                    print(f"[{utc_ts()}] WARN: Telegram alert failed: {e}", flush=True)
            else:
                print(
                    f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                    " not set, skipping alert",
                    flush=True,
                )
            # Escalation: fire on 2nd, 3rd, etc. long-call warning in the same day
            long_call_count = daily_state["warnings_triggered"]["floor_2_long_call"]
            escalation_event = check_floor_2_escalation(
                long_call_count, floor_2_warn_threshold_s, climate_state
            )
            if escalation_event:
                daily_state["warnings_triggered"]["floor_2_escalation"] += 1
                fresh_restart = _emit_derived(escalation_event, derived_log, fresh_restart)
                if telegram_bot_token and telegram_chat_id:
                    import urllib.parse as _parse
                    import urllib.request as _urllib

                    esc_msg = (
                        f"🚨 Floor 2 long-call escalation: {long_call_count} long calls today"
                        " — furnace may be struggling. Check HVAC."
                    )
                    _url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                    _tdata = _parse.urlencode(
                        {"chat_id": telegram_chat_id, "text": esc_msg}
                    ).encode()
                    try:
                        _urllib.urlopen(_url, _tdata, timeout=10)
                    except Exception as _esc_e:
                        print(
                            f"[{utc_ts()}] WARN: Telegram escalation alert failed: {_esc_e}",
                            flush=True,
                        )
                else:
                    print(
                        f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                        " not set, skipping escalation alert",
                        flush=True,
                    )

        # Observer silence watchdog (runs on every event and on timeouts)
        silence_event, observer_silence_sent = check_observer_silence(
            last_observer_event_ts,
            observer_silence_sent,
            observer_silence_threshold_s,
            datetime.now(UTC),
        )
        if silence_event:
            daily_state["warnings_triggered"]["observer_silence"] += 1
            fresh_restart = _emit_derived(silence_event, derived_log, fresh_restart)
            if telegram_bot_token and telegram_chat_id:
                import urllib.parse as _parse
                import urllib.request as _urllib

                silence_s = silence_event["data"]["silence_s"]
                last_ts = silence_event["data"]["last_event_ts"]
                silence_min = silence_s // 60
                msg = (
                    f"⚠️ Observer silence detected!\n"
                    f"No events received for {silence_min} min.\n"
                    f"Last event: {last_ts}\n"
                    f"Check observer service on Pi."
                )
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                data = _parse.urlencode({"chat_id": telegram_chat_id, "text": msg}).encode()
                try:
                    _urllib.urlopen(url, data=data, timeout=10)
                except Exception as e:
                    print(f"[{utc_ts()}] WARN: Telegram alert failed: {e}", flush=True)
            else:
                print(
                    f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                    " not set, skipping observer silence alert",
                    flush=True,
                )

        # In-flight floor-not-responding check (runs on every event and on timeouts)
        for finding in floor_no_response_rule.check(datetime.now(UTC)):
            no_resp_event = {
                "schema": "homeops.consumer.floor_no_response_warning.v1",
                "source": "consumer.v1",
                "ts": utc_ts(),
                "data": finding,
            }
            daily_state["warnings_triggered"]["floor_no_response"] += 1
            fresh_restart = _emit_derived(no_resp_event, derived_log, fresh_restart)
            zone_label = finding["zone"].replace("_", " ").title()
            if telegram_bot_token and telegram_chat_id:
                import urllib.parse as _parse
                import urllib.request as _urllib

                start_t = finding["start_temp"]
                curr_t = finding["current_temp"]
                elapsed_m = finding["minutes_elapsed"]
                msg = (
                    f"⚠️ {zone_label} not responding!\n"
                    f"Calling for {elapsed_m:.0f} min with no temperature increase.\n"
                    f"Start temp: {start_t}°F, Current: {curr_t}°F\n"
                    f"Check thermostat or vents."
                )
                url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
                data = _parse.urlencode({"chat_id": telegram_chat_id, "text": msg}).encode()
                try:
                    _urllib.urlopen(url, data=data, timeout=10)
                except Exception as e:
                    print(f"[{utc_ts()}] WARN: Telegram alert failed: {e}", flush=True)
            else:
                print(
                    f"[{utc_ts()}] WARN: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
                    " not set, skipping floor-not-responding alert",
                    flush=True,
                )

        # Zone temperature snapshot — write every 5 minutes if we have data.
        now = datetime.now(UTC)
        if (
            last_snapshot_ts is None
            or (now - last_snapshot_ts).total_seconds() >= ZONE_TEMP_SNAPSHOT_INTERVAL_S
        ):
            if write_zone_temp_snapshot(climate_state, daily_state):
                print(f"[{utc_ts()}] Zone temp snapshot written", flush=True)
            last_snapshot_ts = now


if __name__ == "__main__":
    main()
