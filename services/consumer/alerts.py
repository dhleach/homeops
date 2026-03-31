"""Alert checks for floor-2 long-call, escalation, observer silence, and zone temp snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from constants import ZONE_TEMP_SNAPSHOT_LOG
from utils import append_jsonl, utc_ts


def check_floor_2_warning(
    floor_on_since: dict[str, datetime | None],
    floor_2_warn_sent: bool,
    floor_2_warn_threshold_s: int,
    now_ts: datetime,
    climate_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
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

    warn_event: dict[str, Any] = {
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
    climate_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
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
) -> tuple[dict[str, Any] | None, bool]:
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

    warn_event: dict[str, Any] = {
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


def write_zone_temp_snapshot(
    climate_state: dict[str, Any],
    daily_state: dict[str, Any],
    snapshot_log: str = ZONE_TEMP_SNAPSHOT_LOG,
) -> bool:
    """Write a zone_temp_snapshot.v1 record if climate_state has at least one zone.

    Returns True if a snapshot was written, False otherwise.
    """
    if not climate_state:
        return False

    zones: dict[str, Any] = {}
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

    record: dict[str, Any] = {
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
