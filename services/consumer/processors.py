"""Event processors for floor, furnace, climate, and outdoor temperature events.

Revision history:
  2026-08-28  Add an additive thermostat cooling session/outcome path with
              directional target semantics, one-time target crossing, and
              separate persisted state; leave all heating event names, fields,
              and comparators unchanged.
  2026-08-27  Add isolated cooling-call and aggregate cooling-session processors
              with sibling event schemas, preserving the heating processors and
              their payload contracts unchanged.
  2026-08-25  Validate and translate automatic mitigation rollback events so
              fail-safe shutdowns are durable, replayable, and alertable.
  2026-08-25  Validate and translate the staged Home Assistant mitigation event
              into a durable consumer event so applied/skipped decisions survive
              the observer-to-consumer boundary.
  2026-08-24  Added injectable slow-to-heat thresholds and an enabled gate so
              climate warnings honor the shared rules.yaml configuration.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from dateutil.parser import isoparse

from constants import (
    _COOLING_FLOOR_ENTITIES,
    _FLOOR_ENTITIES,
    _ZONE_TO_FLOOR_ENTITY,
    AC_COOLING_ENTITY,
    CLIMATE_ENTITIES,
    SLOW_TO_HEAT_THRESHOLDS_S,
)
from utils import utc_ts

MITIGATION_EVENT_TYPE = "homeops.mitigation.zone_stagger_applied.v1"
MITIGATION_ROLLBACK_EVENT_TYPE = "homeops.mitigation.rollback.v1"
MITIGATION_SHORT_CYCLE_EVENT_TYPE = "homeops.mitigation.short_cycle_detected.v1"
_MITIGATION_ZONES = frozenset({"floor_1", "floor_2", "floor_3"})
_MITIGATION_OUTCOMES = frozenset({"applied", "skipped"})


def process_mitigation_event(
    event_type: str | None,
    event_data: dict[str, Any] | None,
    processing_ts: str | None = None,
) -> dict[str, Any] | None:
    """Validate and translate a Home Assistant mitigation decision.

    Home Assistant emits this event on its event bus; the observer wraps it in
    ``homeops.observer.event.v1`` before the consumer sees it.  Invalid or
    incomplete payloads return ``None`` so malformed input cannot enter the
    derived event log.
    """
    if event_type != MITIGATION_EVENT_TYPE or not isinstance(event_data, dict):
        return None

    zone = event_data.get("zone")
    reason = event_data.get("reason")
    trigger_event_id = event_data.get("trigger_event_id")
    outcome = event_data.get("outcome")
    delay_raw = event_data.get("delay_minutes")

    if event_data.get("event_type") != event_type:
        return None
    if zone not in _MITIGATION_ZONES:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    if not isinstance(trigger_event_id, str) or not trigger_event_id.strip():
        return None
    if outcome not in _MITIGATION_OUTCOMES:
        return None
    if isinstance(delay_raw, bool) or delay_raw is None:
        return None
    try:
        delay_minutes = float(delay_raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(delay_minutes) or delay_minutes < 0:
        return None
    if delay_minutes.is_integer():
        normalized_delay: int | float = int(delay_minutes)
    else:
        normalized_delay = delay_minutes

    result = {
        "schema": MITIGATION_EVENT_TYPE,
        "event_type": MITIGATION_EVENT_TYPE,
        "source": "consumer.v1",
        "ts": processing_ts or utc_ts(),
        "data": {
            "event_type": MITIGATION_EVENT_TYPE,
            "zone": zone,
            "reason": reason.strip(),
            "delay_minutes": normalized_delay,
            "trigger_event_id": trigger_event_id.strip(),
            "outcome": outcome,
        },
    }
    incident_id = event_data.get("incident_id")
    if incident_id is not None:
        if not isinstance(incident_id, str) or not incident_id.strip():
            return None
        result["data"]["incident_id"] = incident_id.strip()
    attempt_number = event_data.get("attempt_number")
    if attempt_number is not None:
        if isinstance(attempt_number, bool):
            return None
        try:
            normalized_attempt = float(attempt_number)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(normalized_attempt) or not normalized_attempt.is_integer():
            return None
        normalized_attempt_int = int(normalized_attempt)
        if not 1 <= normalized_attempt_int <= 3:
            return None
        result["data"]["attempt_number"] = normalized_attempt_int
    return result


def process_mitigation_rollback_event(
    event_type: str | None,
    event_data: dict[str, Any] | None,
    processing_ts: str | None = None,
) -> dict[str, Any] | None:
    """Validate and translate an automatic mitigation rollback event."""
    if event_type != MITIGATION_ROLLBACK_EVENT_TYPE or not isinstance(event_data, dict):
        return None
    if event_data.get("event_type") != event_type:
        return None

    incident_id = event_data.get("incident_id")
    reason = event_data.get("reason")
    trigger_event_id = event_data.get("trigger_event_id")
    storm_started = event_data.get("storm_window_started_at")
    if not isinstance(incident_id, str) or not incident_id.strip():
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    if not isinstance(trigger_event_id, str) or not trigger_event_id.strip():
        return None
    if not isinstance(storm_started, str) or not storm_started.strip():
        return None
    try:
        isoparse(storm_started)
    except (TypeError, ValueError):
        return None

    failed_attempts = event_data.get("failed_attempts")
    if isinstance(failed_attempts, bool) or failed_attempts is None:
        return None
    try:
        normalized_attempts = float(failed_attempts)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(normalized_attempts)
        or not normalized_attempts.is_integer()
        or normalized_attempts < 3
    ):
        return None
    if event_data.get("mitigation_enabled") is not False:
        return None
    if event_data.get("rollback_state") != "rolled_back":
        return None

    source_event_type = event_data.get("source_event_type")
    if source_event_type != MITIGATION_SHORT_CYCLE_EVENT_TYPE:
        return None

    result_data: dict[str, Any] = {
        "event_type": MITIGATION_ROLLBACK_EVENT_TYPE,
        "incident_id": incident_id.strip(),
        "failed_attempts": int(normalized_attempts),
        "reason": reason.strip(),
        "trigger_event_id": trigger_event_id.strip(),
        "storm_window_started_at": storm_started.strip(),
        "mitigation_enabled": False,
        "rollback_state": "rolled_back",
        "source_event_type": source_event_type,
    }
    for field in ("short_cycle_duration_s", "short_cycle_threshold_s"):
        value = event_data.get(field)
        if value not in (None, ""):
            if isinstance(value, bool):
                return None
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(numeric_value) or numeric_value < 0:
                return None
            result_data[field] = int(numeric_value) if numeric_value.is_integer() else numeric_value

    return {
        "schema": MITIGATION_ROLLBACK_EVENT_TYPE,
        "event_type": MITIGATION_ROLLBACK_EVENT_TYPE,
        "source": "consumer.v1",
        "ts": processing_ts or utc_ts(),
        "data": result_data,
    }


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


def process_cooling_floor_event(
    entity_id: str,
    old_state: str | None,
    new_state: str | None,
    ts: datetime | None,
    ts_str: str | None,
    cooling_floor_on_since: dict[str, datetime | None],
    processing_ts: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, datetime | None]]:
    """Process a floor cooling-call state change.

    Cooling has its own state map and event names so the existing heating
    ``floor_call_*`` stream remains unchanged.  The helper sensors represent
    thermostat demand, not direct compressor feedback.
    """
    floor_key = _COOLING_FLOOR_ENTITIES.get(entity_id)
    if floor_key is None:
        return [], cooling_floor_on_since

    events: list[dict[str, Any]] = []
    cooling_floor_on_since = dict(cooling_floor_on_since)
    _evt_ts = processing_ts or utc_ts()

    if old_state == "off" and new_state == "on":
        cooling_floor_on_since[entity_id] = ts
        events.append(
            {
                "schema": "homeops.consumer.cooling_call_started.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": {
                    "floor": floor_key,
                    "started_at": ts_str,
                    "entity_id": entity_id,
                },
            }
        )

    if old_state == "on" and new_state == "off":
        duration_s: int | None = None
        started = cooling_floor_on_since.get(entity_id)
        if started and ts:
            duration_s = int((ts - started).total_seconds())
        cooling_floor_on_since[entity_id] = None
        events.append(
            {
                "schema": "homeops.consumer.cooling_call_ended.v1",
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

    return events, cooling_floor_on_since


def process_furnace_event(
    entity_id: str,
    old_state: str | None,
    new_state: str | None,
    ts: datetime | None,
    ts_str: str | None,
    furnace_on_since: datetime | None,
    processing_ts: str | None = None,
    last_outdoor_temp_f: float | None = None,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """
    Process a furnace heating state change.

    Returns (events, updated_furnace_on_since).
    events is a list of derived event dicts (0 or 1 items).

    last_outdoor_temp_f is the most recent outdoor temperature reading from daily_state;
    it is included in heating_session_ended.v1 so downstream consumers have thermal
    context without needing a separate lookup.
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
                    "outdoor_temp_f": last_outdoor_temp_f,
                },
            }
        )

    return events, furnace_on_since


def process_cooling_session_event(
    entity_id: str,
    old_state: str | None,
    new_state: str | None,
    ts: datetime | None,
    ts_str: str | None,
    ac_cooling_on_since: datetime | None,
    processing_ts: str | None = None,
    last_outdoor_temp_f: float | None = None,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """Process the inferred whole-home AC/cooling helper state change.

    This is a parallel path to :func:`process_furnace_event`; it deliberately
    does not share or mutate ``furnace_on_since`` so heating and cooling
    sessions remain independently durable contracts.
    """
    if entity_id != AC_COOLING_ENTITY:
        return [], ac_cooling_on_since

    events: list[dict[str, Any]] = []
    _evt_ts = processing_ts or utc_ts()

    if old_state == "off" and new_state == "on":
        ac_cooling_on_since = ts
        events.append(
            {
                "schema": "homeops.consumer.cooling_session_started.v1",
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
        if ac_cooling_on_since and ts:
            duration_s = int((ts - ac_cooling_on_since).total_seconds())
        ac_cooling_on_since = None
        events.append(
            {
                "schema": "homeops.consumer.cooling_session_ended.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": {
                    "ended_at": ts_str,
                    "entity_id": entity_id,
                    "duration_s": duration_s,
                    "outdoor_temp_f": last_outdoor_temp_f,
                },
            }
        )

    return events, ac_cooling_on_since


def process_climate_event(
    entity_id: str,
    attributes: dict[str, Any] | None,
    ts_str: str | None,
    climate_state: dict[str, Any],
    new_state: str | None = None,
    floor_on_since: dict[str, datetime | None] | None = None,
    daily_state: dict[str, Any] | None = None,
    processing_ts: str | None = None,
    slow_to_heat_thresholds_s: dict[str, float] | None = None,
    slow_to_heat_enabled: bool = True,
    cooling_floor_on_since: dict[str, datetime | None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Process a climate entity state_changed event.

    Emits shared thermostat change events when setpoint, current_temp, or HVAC
    mode/action changes.  It also emits the heating performance events when
    their existing heating boundaries are met, plus additive cooling
    session/performance events when ``hvac_action`` is ``"cooling"``. Heating
    state and event contracts are intentionally kept separate from the cooling
    state machine.

    climate_state is a dict keyed by entity_id with previous known values.
    new_state is the top-level HA state (e.g. "heat", "off", "cool") used as hvac_mode.
    floor_on_since is passed through from main() for other_zones_calling computation.
    daily_state is passed through from main() for outdoor_temp_f lookup.
    slow_to_heat_thresholds_s and slow_to_heat_enabled override the default
    thresholds for the service configuration.
    cooling_floor_on_since is the independent cooling-call map used for the
    cooling session-start snapshot; heating call entities are never reused for
    cooling outcomes.

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
    if cooling_floor_on_since is None:
        cooling_floor_on_since = {}

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

    # Cooling session state is deliberately parallel to, and independent from,
    # the established heating fields above.  The starting setpoint is captured
    # because a later thermostat adjustment must not change the target for the
    # already-running cooling session.
    cooling_start_temp: float | None = prev.get("cooling_start_temp")
    cooling_start_setpoint: float | None = prev.get("cooling_start_setpoint")
    cooling_start_ts: datetime | None = prev.get("cooling_start_ts")
    cooling_setpoint_reached_ts: datetime | None = prev.get("cooling_setpoint_reached_ts")
    cooling_setpoint_reached_temp: float | None = prev.get("cooling_setpoint_reached_temp")
    cooling_post_setpoint_temps: list[float] = list(prev.get("cooling_post_setpoint_temps") or [])
    cooling_session_temps: list[float] = list(prev.get("cooling_session_temps") or [])
    cooling_start_other_zones: list[str] | None = prev.get("cooling_start_other_zones")
    setpoint_changed_during_cooling: bool = prev.get("setpoint_changed_during_cooling", False)

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

    # Detect a per-zone cooling session start.  The aggregate AC helper has its
    # own whole-home event path; these events describe the thermostat/climate
    # action for one zone and are therefore intentionally distinct.
    if prev_hvac_action != "cooling" and hvac_action == "cooling":
        cooling_start_temp = current_temp
        cooling_start_setpoint = setpoint
        cooling_start_ts = ts
        cooling_setpoint_reached_ts = None
        cooling_setpoint_reached_temp = None
        cooling_post_setpoint_temps = []
        cooling_session_temps = []
        setpoint_changed_during_cooling = False
        this_cooling_floor_entity = next(
            (eid for eid, floor_name in _COOLING_FLOOR_ENTITIES.items() if floor_name == zone),
            None,
        )
        cooling_start_other_zones = [
            eid
            for eid, started in cooling_floor_on_since.items()
            if started is not None and eid != this_cooling_floor_entity
        ]
        events.append(
            {
                "schema": "homeops.consumer.thermostat_cooling_session_started.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": {
                    "entity_id": entity_id,
                    "zone": zone,
                    "started_at": ts_str,
                    "mode": "cool",
                    "hvac_mode": hvac_mode,
                    "hvac_action": hvac_action,
                    "setpoint": setpoint,
                    "current_temp": current_temp,
                    "other_zones_calling": cooling_start_other_zones or [],
                },
            }
        )

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
        if prev_hvac_action == "cooling" and hvac_action == "cooling":
            setpoint_changed_during_cooling = True

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
        if hvac_action == "cooling":
            cooling_session_temps.append(current_temp)

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
        if prev_hvac_action == "cooling" and hvac_action == "cooling":
            setpoint_changed_during_cooling = True

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

    # Cooling reaches its session-start target from above.  This is a separate
    # event and comparator from the historical heating setpoint-reached path:
    # cooling is satisfied at or below the captured target, not at or above the
    # current thermostat setpoint.
    cooling_setpoint_just_reached = False
    cooling_target = cooling_start_setpoint
    if (
        prev_hvac_action == "cooling"
        and cooling_setpoint_reached_ts is None
        and current_temp is not None
        and cooling_target is not None
        and current_temp <= cooling_target
        and (prev_current_temp is None or prev_current_temp > cooling_target)
    ):
        cooling_common = dict(common)
        cooling_common["mode"] = "cool"
        cooling_common["setpoint"] = cooling_target
        events.append(
            {
                "schema": "homeops.consumer.thermostat_cooling_setpoint_reached.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": cooling_common,
            }
        )

        # A missing start boundary can still produce the directional crossing
        # event, but it cannot produce a valid time-to-cool measurement.
        if (
            cooling_start_ts is not None
            and cooling_start_temp is not None
            and cooling_start_temp > cooling_target
        ):
            duration_s = int((ts - cooling_start_ts).total_seconds()) if ts else 0
            degrees_cooled = cooling_start_temp - current_temp
            degrees_per_min = (
                round(degrees_cooled / (duration_s / 60), 3) if duration_s > 0 else 0.0
            )
            this_cooling_floor_entity = next(
                (eid for eid, floor_name in _COOLING_FLOOR_ENTITIES.items() if floor_name == zone),
                None,
            )
            other_zones_calling = [
                eid
                for eid, started in cooling_floor_on_since.items()
                if started is not None and eid != this_cooling_floor_entity
            ]
            events.append(
                {
                    "schema": "homeops.consumer.zone_time_to_cool.v1",
                    "source": "consumer.v1",
                    "ts": _evt_ts,
                    "data": {
                        "entity_id": entity_id,
                        "zone": zone,
                        "mode": "cool",
                        "start_temp": cooling_start_temp,
                        "setpoint": cooling_target,
                        "setpoint_delta": cooling_start_temp - cooling_target,
                        "duration_s": duration_s,
                        "end_temp": current_temp,
                        "degrees_cooled": degrees_cooled,
                        "degrees_per_min": degrees_per_min,
                        "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                        "other_zones_calling": other_zones_calling,
                    },
                }
            )

        cooling_setpoint_reached_ts = ts
        cooling_setpoint_reached_temp = current_temp
        cooling_post_setpoint_temps.append(current_temp)
        cooling_setpoint_just_reached = True

    # Keep the post-target window separate from heating's peak-temperature
    # state.  Include an action-ending reading so the cooling trough reflects
    # the last observed temperature before the session closed.
    if (
        not cooling_setpoint_just_reached
        and prev.get("cooling_setpoint_reached_ts") is not None
        and prev_hvac_action == "cooling"
        and current_temp is not None
        and current_temp != prev_current_temp
    ):
        cooling_post_setpoint_temps.append(current_temp)

    # End the per-zone cooling session on the first action transition away from
    # cooling.  Thermal outcomes are emitted before the session-end event so a
    # replay consumer sees the target/miss evidence before the boundary record.
    if prev_hvac_action == "cooling" and hvac_action != "cooling":
        cooling_end_setpoint = cooling_start_setpoint
        duration_s = (
            int((ts - cooling_start_ts).total_seconds())
            if ts is not None and cooling_start_ts is not None
            else None
        )

        if cooling_setpoint_reached_ts is not None:
            if current_temp is not None and (
                not cooling_post_setpoint_temps or cooling_post_setpoint_temps[-1] != current_temp
            ):
                cooling_post_setpoint_temps.append(current_temp)
            trough_temp = (
                min(cooling_post_setpoint_temps) if len(cooling_post_setpoint_temps) > 1 else None
            )
            undershoot_s = (
                int((ts - cooling_setpoint_reached_ts).total_seconds()) if ts is not None else 0
            )
            events.append(
                {
                    "schema": "homeops.consumer.zone_cooling_undershoot.v1",
                    "source": "consumer.v1",
                    "ts": _evt_ts,
                    "data": {
                        "entity_id": entity_id,
                        "zone": zone,
                        "mode": "cool",
                        "start_temp": cooling_start_temp,
                        "setpoint": cooling_end_setpoint,
                        "setpoint_delta": (
                            cooling_start_temp - cooling_end_setpoint
                            if cooling_start_temp is not None and cooling_end_setpoint is not None
                            else None
                        ),
                        "end_temp": current_temp,
                        "undershoot_s": undershoot_s,
                        "trough_temp": trough_temp,
                        "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                        "other_zones_calling": cooling_start_other_zones or [],
                    },
                }
            )
        elif (
            cooling_end_setpoint is not None
            and cooling_start_temp is not None
            and cooling_start_temp > cooling_end_setpoint
        ):
            cooling_samples = list(cooling_session_temps)
            if current_temp is not None and (
                not cooling_samples or cooling_samples[-1] != current_temp
            ):
                cooling_samples.append(current_temp)
            closest_temp = min(cooling_samples) if cooling_samples else cooling_start_temp
            setpoint_delta = cooling_start_temp - cooling_end_setpoint
            # If the fallback sample reached the target, the crossing event was
            # likely missed; do not emit a contradictory miss.
            if closest_temp > cooling_end_setpoint:
                events.append(
                    {
                        "schema": "homeops.consumer.zone_cooling_setpoint_miss.v1",
                        "source": "consumer.v1",
                        "ts": _evt_ts,
                        "data": {
                            "entity_id": entity_id,
                            "zone": zone,
                            "mode": "cool",
                            "start_temp": cooling_start_temp,
                            "setpoint": cooling_end_setpoint,
                            "setpoint_delta": setpoint_delta,
                            "duration_s": duration_s if duration_s is not None else 0,
                            "closest_temp": closest_temp,
                            "delta": closest_temp - cooling_end_setpoint,
                            "outdoor_temp_f": daily_state.get("last_outdoor_temp_f"),
                            "other_zones_calling": cooling_start_other_zones or [],
                            "likely_cause": (
                                "thermostat_adjustment"
                                if setpoint_changed_during_cooling
                                else "unknown"
                            ),
                        },
                    }
                )

        events.append(
            {
                "schema": "homeops.consumer.thermostat_cooling_session_ended.v1",
                "source": "consumer.v1",
                "ts": _evt_ts,
                "data": {
                    "entity_id": entity_id,
                    "zone": zone,
                    "ended_at": ts_str,
                    "mode": "cool",
                    "hvac_mode": hvac_mode,
                    "hvac_action": hvac_action,
                    "start_temp": cooling_start_temp,
                    "setpoint": cooling_end_setpoint,
                    "current_temp": current_temp,
                    "duration_s": duration_s,
                    "target_reached": cooling_setpoint_reached_ts is not None,
                    "other_zones_calling": cooling_start_other_zones or [],
                },
            }
        )

        cooling_start_temp = None
        cooling_start_setpoint = None
        cooling_start_ts = None
        cooling_setpoint_reached_ts = None
        cooling_setpoint_reached_temp = None
        cooling_post_setpoint_temps = []
        cooling_session_temps = []
        cooling_start_other_zones = None
        setpoint_changed_during_cooling = False

    # Slow-to-heat check: zone has been calling longer than threshold without reaching setpoint.
    if (
        hvac_action == "heating"
        and heating_start_ts is not None
        and setpoint_reached_ts is None
        and not slow_to_heat_sent
        and ts is not None
        and slow_to_heat_enabled
        and zone in (slow_to_heat_thresholds_s or SLOW_TO_HEAT_THRESHOLDS_S)
    ):
        threshold_s = (slow_to_heat_thresholds_s or SLOW_TO_HEAT_THRESHOLDS_S)[zone]
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
        "cooling_start_temp": cooling_start_temp,
        "cooling_start_setpoint": cooling_start_setpoint,
        "cooling_start_ts": cooling_start_ts,
        "cooling_setpoint_reached_ts": cooling_setpoint_reached_ts,
        "cooling_setpoint_reached_temp": cooling_setpoint_reached_temp,
        "cooling_post_setpoint_temps": cooling_post_setpoint_temps,
        "cooling_session_temps": cooling_session_temps,
        "cooling_start_other_zones": cooling_start_other_zones,
        "setpoint_changed_during_cooling": setpoint_changed_during_cooling,
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
