#!/usr/bin/env python3
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dateutil.parser import isoparse


def utc_ts():
    return datetime.now(UTC).isoformat()


def _get_version() -> str:
    """Return the current git short commit hash, or "unknown" if unavailable."""
    try:
        import subprocess as _subprocess

        return (
            _subprocess.check_output(
                ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
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

CLIMATE_ENTITIES = {
    "climate.floor_1_thermostat": "floor_1",
    "climate.floor_2_thermostat": "floor_2",
    "climate.floor_3_thermostat": "floor_3",
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

    # Detect heating session start: hvac_action transitions TO "heating".
    if prev_hvac_action != "heating" and hvac_action == "heating":
        heating_start_temp = current_temp
        heating_start_ts = ts
        setpoint_reached_ts = None
        setpoint_reached_temp = None
        post_setpoint_temps = []
        setpoint_changed_during_heating = False
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
            # Heating ended before setpoint was reached — emit undershoot event.
            if setpoint is not None and current_temp is not None:
                call_duration_s = (
                    int((ts - heating_start_ts).total_seconds()) if ts and heating_start_ts else 0
                )
                shortfall_f = round(setpoint - current_temp, 1)
                likely_cause = (
                    "thermostat_adjustment" if setpoint_changed_during_heating else "unknown"
                )
                events.append(
                    {
                        "schema": "homeops.consumer.zone_undershoot.v1",
                        "source": "consumer.v1",
                        "ts": utc_ts(),
                        "data": {
                            "entity_id": entity_id,
                            "zone": zone,
                            "start_temp_f": heating_start_temp,
                            "final_temp_f": current_temp,
                            "setpoint_f": setpoint,
                            "shortfall_f": shortfall_f,
                            "call_duration_s": call_duration_s,
                            "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                            "likely_cause": likely_cause,
                        },
                    }
                )
        # Clear all heating session state for this entity.
        heating_start_temp = None
        heating_start_ts = None
        setpoint_reached_ts = None
        setpoint_reached_temp = None
        post_setpoint_temps = []
        heating_start_other_zones = None
        setpoint_changed_during_heating = False

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
        "heating_start_other_zones": heating_start_other_zones,
        "setpoint_changed_during_heating": setpoint_changed_during_heating,
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
      - outdoor_temps: list of float
    Returns the event dict.
    """
    outdoor_temps = daily_state.get("outdoor_temps") or []
    outdoor_temp_min_f = min(outdoor_temps) if outdoor_temps else None
    outdoor_temp_max_f = max(outdoor_temps) if outdoor_temps else None

    per_floor_runtime_s = {}
    for entity_id, floor_name in _FLOOR_ENTITIES.items():
        per_floor_runtime_s[floor_name] = daily_state.get("floor_runtime_s", {}).get(entity_id, 0)

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
        },
    }


def check_floor_2_warning(floor_on_since, floor_2_warn_sent, floor_2_warn_threshold_s, now_ts):
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

    warn_event = {
        "schema": "homeops.consumer.floor_2_long_call_warning.v1",
        "source": "consumer.v1",
        "ts": utc_ts(),
        "data": {
            "floor": "floor_2",
            "elapsed_s": elapsed_s,
            "threshold_s": floor_2_warn_threshold_s,
            "entity_id": f2_entity,
        },
    }
    return warn_event, True


def main():
    """Tail observer events and emit derived floor/furnace session events."""
    path = os.environ.get("EVENT_LOG", "state/observer/events.jsonl")
    derived_log = os.environ.get("DERIVED_EVENT_LOG", "state/consumer/events.jsonl")
    print(f"[{utc_ts()}] Derived log: {derived_log}", flush=True)
    print(f"[{utc_ts()}] Consumer version: {_get_version()}", flush=True)
    print(f"[{utc_ts()}] Consumer following: {path}", flush=True)

    floor_2_warn_threshold_s = int(os.environ.get("FLOOR_2_WARN_THRESHOLD_S", "2700"))  # 45 min
    print(f"[{utc_ts()}] Floor-2 warning threshold: {floor_2_warn_threshold_s}s", flush=True)
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Track session state across events so "ended" records can include durations.
    furnace_on_since = last_furnace_on_since(path)
    if furnace_on_since:
        print(
            f"[{utc_ts()}] Bootstrapped furnace_on_since={furnace_on_since.isoformat()}",
            flush=True,
        )

    floor_entities = _FLOOR_ENTITIES
    floor_on_since = {key: None for key in floor_entities.keys()}
    floor_2_warn_sent = False  # reset each time floor 2 starts a new call

    # Track previous climate state per entity to detect changes.
    climate_state: dict = {}

    # Daily accumulation state for furnace_daily_summary.v1
    def _empty_daily_state():
        return {
            "furnace_runtime_s": 0,
            "session_count": 0,
            "floor_runtime_s": {},
            "outdoor_temps": [],
            "last_outdoor_temp_f": None,
        }

    daily_state = _empty_daily_state()
    current_date = datetime.now(UTC).strftime("%Y-%m-%d")

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
                    daily_state = _empty_daily_state()
                    current_date = evt_date

            # Per-floor call sessions are derived from floor_* heating_call sensors.
            if entity_id in floor_entities:
                derived_events, floor_on_since, floor_2_warn_sent = process_floor_event(
                    entity_id, old_state, new_state, ts, ts_str, floor_on_since, floor_2_warn_sent
                )
                for derived in derived_events:
                    print(json.dumps(derived), flush=True)
                    append_jsonl(derived_log, derived)
                    if derived["schema"] == "homeops.consumer.floor_call_ended.v1":
                        d = derived["data"]
                        if d.get("duration_s") is not None:
                            eid = d["entity_id"]
                            daily_state["floor_runtime_s"][eid] = (
                                daily_state["floor_runtime_s"].get(eid, 0) + d["duration_s"]
                            )

            # Outdoor temperature readings are passed through as-is from the sensor.
            if entity_id == "sensor.outdoor_temperature":
                for derived in process_outdoor_temp_event(entity_id, new_state, ts_str):
                    print(json.dumps(derived), flush=True)
                    append_jsonl(derived_log, derived)
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
                    print(json.dumps(derived), flush=True)
                    append_jsonl(derived_log, derived)

            # Whole-home heating sessions are derived from furnace on/off transitions.
            if entity_id == "binary_sensor.furnace_heating":
                derived_events, furnace_on_since = process_furnace_event(
                    entity_id, old_state, new_state, ts, ts_str, furnace_on_since
                )
                for derived in derived_events:
                    print(json.dumps(derived), flush=True)
                    append_jsonl(derived_log, derived)
                    if derived["schema"] == "homeops.consumer.heating_session_ended.v1":
                        d = derived["data"]
                        if d.get("duration_s") is not None:
                            daily_state["furnace_runtime_s"] += d["duration_s"]
                        daily_state["session_count"] += 1

        # In-flight floor-2 long-call check (runs on every event and on timeouts)
        warn_event, floor_2_warn_sent = check_floor_2_warning(
            floor_on_since, floor_2_warn_sent, floor_2_warn_threshold_s, datetime.now(UTC)
        )
        if warn_event:
            print(json.dumps(warn_event), flush=True)
            append_jsonl(derived_log, warn_event)
            if telegram_bot_token and telegram_chat_id:
                import urllib.parse as _parse
                import urllib.request as _urllib

                elapsed_s = warn_event["data"]["elapsed_s"]
                msg = (
                    f"⚠️ Floor 2 has been calling for {elapsed_s // 60} min!\n"
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


if __name__ == "__main__":
    main()
