"""State persistence and initialization for the HomeOps consumer service."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from constants import _FLOOR_ENTITIES, CLIMATE_ENTITIES, STATE_FILE
from dateutil.parser import isoparse
from utils import _parse_dt, utc_ts


def last_furnace_on_since(path: str) -> datetime | None:
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


def _empty_daily_state() -> dict[str, Any]:
    return {
        "furnace_runtime_s": 0,
        "session_count": 0,
        "floor_runtime_s": {},
        "per_floor_session_count": {eid: 0 for eid in _FLOOR_ENTITIES},
        "per_floor_max_call_s": {eid: None for eid in _FLOOR_ENTITIES},
        "outdoor_temps": [],
        "last_outdoor_temp_f": None,
        "per_floor_setpoint_samples": {eid: [] for eid in CLIMATE_ENTITIES},
        "warnings_triggered": {
            "floor_2_long_call": 0,
            "floor_2_escalation": 0,
            "floor_no_response": 0,
            "zone_slow_to_heat": 0,
            "observer_silence": 0,
            "setpoint_miss": 0,
        },
    }


def _save_state(
    floor_on_since: dict[str, datetime | None],
    furnace_on_since: datetime | None,
    climate_state: dict[str, Any],
    daily_state: dict[str, Any],
    *,
    last_consumed_observer_ts: str | None = None,
    telegram_last_update_id: int | None = None,
    state_file: Path | None = None,
) -> None:
    """Atomically persist consumer runtime state to disk."""

    def _dt(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt is not None else None

    serialized_fos: dict[str, str | None] = {k: _dt(v) for k, v in floor_on_since.items()}

    serialized_cs: dict[str, Any] = {}
    for eid, es in climate_state.items():
        s = dict(es)
        s["heating_start_ts"] = _dt(s.get("heating_start_ts"))
        s["setpoint_reached_ts"] = _dt(s.get("setpoint_reached_ts"))
        serialized_cs[eid] = s

    payload: dict[str, Any] = {
        "floor_on_since": serialized_fos,
        "furnace_on_since": _dt(furnace_on_since),
        "climate_state": serialized_cs,
        "daily_state": daily_state,
        "last_consumed_observer_ts": last_consumed_observer_ts,
        "telegram_last_update_id": telegram_last_update_id,
        "saved_at": utc_ts(),
    }
    sf = state_file or STATE_FILE
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.rename(sf)


def _load_state(*, state_file: Path | None = None) -> dict[str, Any] | None:
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


def _load_last_consumed_ts(*, state_file: Path | None = None) -> str | None:
    """
    Read ``last_consumed_observer_ts`` from the state file regardless of file age.

    Returns None if the file is missing, unreadable, or the field is absent.
    This is intentionally separate from ``_load_state`` (which rejects stale files)
    so that the playback start point is always available even after long downtime.
    """
    sf = state_file or STATE_FILE
    if not sf.exists():
        return None
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        return data.get("last_consumed_observer_ts")
    except Exception:
        return None


__all__ = [
    "last_furnace_on_since",
    "_empty_daily_state",
    "_save_state",
    "_load_state",
    "_load_last_consumed_ts",
    "_parse_dt",
    "STATE_FILE",
]
