#!/usr/bin/env python3
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from dateutil.parser import isoparse


def utc_ts():
    return datetime.now(UTC).isoformat()


def follow(path: str):
    """Yield new lines as they are appended to a file (tail -f)."""
    with open(path, encoding="utf-8") as f:
        # Start at end of file (only new events)
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            yield line.rstrip("\n")


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


def main():
    """Tail observer events and emit derived floor/furnace session events."""
    path = os.environ.get("EVENT_LOG", "state/observer/events.jsonl")
    derived_log = os.environ.get("DERIVED_EVENT_LOG", "state/consumer/events.jsonl")
    print(f"[{utc_ts()}] Derived log: {derived_log}", flush=True)
    print(f"[{utc_ts()}] Consumer following: {path}", flush=True)

    floor_2_long_call_threshold_s = int(os.environ.get("FLOOR_2_LONG_CALL_THRESHOLD_S", "3600"))
    print(f"[{utc_ts()}] Floor-2 long-call threshold: {floor_2_long_call_threshold_s}s", flush=True)

    # Track session state across events so "ended" records can include durations.
    furnace_on_since = last_furnace_on_since(path)
    if furnace_on_since:
        print(
            f"[{utc_ts()}] Bootstrapped furnace_on_since={furnace_on_since.isoformat()}",
            flush=True,
        )

    floor_entities = {
        "binary_sensor.floor_1_heating_call": "floor_1",
        "binary_sensor.floor_2_heating_call": "floor_2",
        "binary_sensor.floor_3_heating_call": "floor_3",
    }
    floor_on_since = {key: None for key in floor_entities.keys()}

    # Main stream loop: consume observer events and emit higher-level derived events.
    for line in follow(path):
        if not line.strip():
            continue
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

        # Per-floor call sessions are derived from floor_* heating_call sensors.
        if entity_id in floor_entities:
            floor_key = floor_entities[entity_id]

            if old_state == "off" and new_state == "on":
                floor_on_since[entity_id] = ts
                derived = {
                    "schema": "homeops.consumer.floor_call_started.v1",
                    "source": "consumer.v1",
                    "ts": utc_ts(),
                    "data": {
                        "floor": floor_key,
                        "started_at": ts_str,
                        "entity_id": entity_id,
                    },
                }
                print(json.dumps(derived), flush=True)
                append_jsonl(derived_log, derived)

            if old_state == "on" and new_state == "off":
                duration_s = None
                started = floor_on_since.get(entity_id)
                if started and ts:
                    duration_s = int((ts - started).total_seconds())
                floor_on_since[entity_id] = None

                derived = {
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
                print(json.dumps(derived), flush=True)
                append_jsonl(derived_log, derived)

                # Floor-2 long-call detection
                if (
                    floor_key == "floor_2"
                    and duration_s is not None
                    and duration_s >= floor_2_long_call_threshold_s
                ):
                    long_call_event = {
                        "schema": "homeops.consumer.floor_2_long_call_detected.v1",
                        "source": "consumer.v1",
                        "ts": utc_ts(),
                        "data": {
                            "floor": floor_key,
                            "duration_s": duration_s,
                            "threshold_s": floor_2_long_call_threshold_s,
                            "ended_at": ts_str,
                            "entity_id": entity_id,
                        },
                    }
                    print(json.dumps(long_call_event), flush=True)
                    append_jsonl(derived_log, long_call_event)
                    # Send Telegram alert
                    import subprocess as _sp

                    _sp.Popen(
                        [
                            "openclaw",
                            "message",
                            "send",
                            "--channel",
                            "telegram",
                            "--target",
                            "8637877095",
                            "--message",
                            (
                                f"⚠️ Floor 2 long call detected!\n"
                                f"Duration: {duration_s // 60}m {duration_s % 60}s "
                                f"(threshold: {floor_2_long_call_threshold_s // 60}min)\n"
                                f"Ended: {ts_str}\n"
                                f"Check furnace for overheating (Code 4/7 risk)."
                            ),
                        ]
                    )

        # Whole-home heating sessions are derived from furnace on/off transitions.
        if entity_id == "binary_sensor.furnace_heating":
            if old_state == "off" and new_state == "on":
                furnace_on_since = ts
                derived = {
                    "schema": "homeops.consumer.heating_session_started.v1",
                    "source": "consumer.v1",
                    "ts": utc_ts(),
                    "data": {
                        "started_at": ts_str,
                        "entity_id": entity_id,
                    },
                }
                print(json.dumps(derived), flush=True)
                append_jsonl(derived_log, derived)

            if old_state == "on" and new_state == "off":
                duration_s = None
                if furnace_on_since and ts:
                    duration_s = int((ts - furnace_on_since).total_seconds())
                furnace_on_since = None

                derived = {
                    "schema": "homeops.consumer.heating_session_ended.v1",
                    "source": "consumer.v1",
                    "ts": utc_ts(),
                    "data": {
                        "ended_at": ts_str,
                        "entity_id": entity_id,
                        "duration_s": duration_s,
                    },
                }
                print(json.dumps(derived), flush=True)
                append_jsonl(derived_log, derived)


if __name__ == "__main__":
    main()
