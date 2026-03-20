#!/usr/bin/env python3
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dateutil.parser import isoparse


def utc_ts():
    return datetime.now(UTC).isoformat()


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

    # Daily accumulation state for furnace_daily_summary.v1
    def _empty_daily_state():
        return {
            "furnace_runtime_s": 0,
            "session_count": 0,
            "floor_runtime_s": {},
            "outdoor_temps": [],
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
