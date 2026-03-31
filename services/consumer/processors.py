"""Event processors for floor, furnace, climate, and outdoor temperature events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from constants import (
    _FLOOR_ENTITIES,
    _ZONE_TO_FLOOR_ENTITY,
    CLIMATE_ENTITIES,
    SLOW_TO_HEAT_THRESHOLDS_S,
)
from dateutil.parser import isoparse
from utils import utc_ts


def process_floor_event(
    entity_id: str,
    old_state: str | None,
    new_state: str | None,
    ts: datetime | None,
    ts_str: str | None,
    floor_on_since: dict[str, datetime | None],
    floor_2_warn_sent: bool,
    processing_ts: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, datetime | None], bool]:
    """
    Process a floor heating-call state change.

    Returns (events, updated_floor_on_since, updated_floor_2_warn_sent).
    events is a list of derived event dicts (0 or 1 items).
    """
    floor_key = _FLOOR_ENTITIES.get(entity_id)
    if floor_key is None:
        return [], floor_on_since, floor_2_warn_sent

    events: list[dict[str, Any]] = []
    floor_on_since = dict(floor_on_since)  # avoid mutating caller's dict

    _evt_ts = processing_ts or utc_ts()

    if old_state == "off" and new_state == "on":
        floor_on_since[entity_id] = ts
        events.append(
            {
                "schema": "homeops.consumer.floor_call_started.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
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
        duration_s: int | None = None
        started = floor_on_since.get(entity_id)
        if started and ts:
            duration_s = int((ts - started).total_seconds())
        floor_on_since[entity_id] = None
        events.append(
            {
                "schema": "homeops.consumer.floor_call_ended.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": {
                    "floor": floor_key,
                    "ended_at": ts_str,
                    "entity_id": entity_id,
                    "duration_s": duration_s,
                },
            }
        )

    return events, floor_on_since, floor_2_warn_sent


def process_furnace_event(
    entity_id: str,
    old_state: str | None,
    new_state: str | None,
    ts: datetime | None,
    ts_str: str | None,
    furnace_on_since: datetime | None,
    processing_ts: str | None = None,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """
    Process a furnace heating state change.

    Returns (events, updated_furnace_on_since).
    events is a list of derived event dicts (0 or 1 items).
    """
    events: list[dict[str, Any]] = []
    _evt_ts = processing_ts or utc_ts()

    if old_state == "off" and new_state == "on":
        furnace_on_since = ts
        events.append(
            {
                "schema": "homeops.consumer.heating_session_started.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": {
                    "started_at": ts_str,
                    "entity_id": entity_id,
                },
            }
        )

    if old_state == "on" and new_state == "off":
        duration_s: int | None = None
        if furnace_on_since and ts:
            duration_s = int((ts - furnace_on_since).total_seconds())
        furnace_on_since = None
        events.append(
            {
                "schema": "homeops.consumer.heating_session_ended.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": {
                    "ended_at": ts_str,
                    "entity_id": entity_id,
                    "duration_s": duration_s,
                },
            }
        )

    return events, furnace_on_since


def process_climate_event(
    entity_id: str,
    attributes: dict[str, Any] | None,
    ts_str: str | None,
    climate_state: dict[str, Any],
    new_state: str | None = None,
    floor_on_since: dict[str, datetime | None] | None = None,
    daily_state: dict[str, Any] | None = None,
    processing_ts: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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

    _evt_ts = processing_ts or utc_ts()

    setpoint: float | None = attributes.get("temperature")
    current_temp: float | None = attributes.get("current_temperature")
    hvac_mode: str | None = new_state
    hvac_action: str | None = attributes.get("hvac_action")

    prev: dict[str, Any] = climate_state.get(entity_id) or {}
    events: list[dict[str, Any]] = []

    common: dict[str, Any] = {
        "entity_id": entity_id,
        "zone": zone,
        "ts": ts_str,
        "hvac_mode": hvac_mode,
        "hvac_action": hvac_action,
        "setpoint": setpoint,
        "current_temp": current_temp,
    }

    # Parse event timestamp for session duration tracking.
    ts: datetime | None = None
    if ts_str:
        try:
            ts = isoparse(ts_str)
        except Exception:
            pass

    prev_hvac_action: str | None = prev.get("hvac_action")
    prev_current_temp: float | None = prev.get("current_temp")

    # Load heating session state persisted from the previous call.
    heating_start_temp: float | None = prev.get("heating_start_temp")
    heating_start_ts: datetime | None = prev.get("heating_start_ts")
    setpoint_reached_ts: datetime | None = prev.get("setpoint_reached_ts")
    setpoint_reached_temp: float | None = prev.get("setpoint_reached_temp")
    post_setpoint_temps: list[float] = list(prev.get("post_setpoint_temps") or [])
    heating_start_other_zones: list[str] | None = prev.get("heating_start_other_zones")
    setpoint_changed_during_heating: bool = prev.get("setpoint_changed_during_heating", False)
    session_temps: list[float] = list(prev.get("session_temps") or [])
    slow_to_heat_sent: bool = prev.get("slow_to_heat_sent", False)

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
                "ts": _evt_ts,
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
                "ts": _evt_ts,
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
                "ts": _evt_ts,
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
                "ts": _evt_ts,
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
                    "ts": _evt_ts,
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
                    "ts": _evt_ts,
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
                            "ts": _evt_ts,
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
                    "ts": _evt_ts,
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


def process_outdoor_temp_event(
    entity_id: str,
    new_state: str | None,
    ts_str: str | None,
    processing_ts: str | None = None,
) -> list[dict[str, Any]]:
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
    _evt_ts = processing_ts or utc_ts()
    return [
        {
            "schema": "homeops.consumer.outdoor_temp_updated.v1",
            "source": "consumer.v1",
            "ts": _evt_ts,
            "data": {
                "entity_id": entity_id,
                "temperature_f": temp_f,
                "timestamp": ts_str,
            },
        }
    ]
