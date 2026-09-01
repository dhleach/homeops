#!/usr/bin/env python3
"""Export point-in-time HomeOps HVAC training rows from JSONL event logs.

Revision history:
  2026-08-29  Add a deterministic, provenance-preserving exporter for heat and
              cooling session rows without changing consumer runtime behavior.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

TRAINING_ROW_SCHEMA = "homeops.thermal.training_row.v1"
OBSERVER_STATE_SCHEMA = "homeops.observer.state_changed.v1"
EXPERIMENT_MARKER_SCHEMA = "homeops.thermal.experiment_marker.v1"
EXPERIMENT_MARKER_MATCH_TOLERANCE_S = 5 * 60

HEATING_CALL_ENTITIES = {
    "binary_sensor.floor_1_heating_call": "floor_1",
    "binary_sensor.floor_2_heating_call": "floor_2",
    "binary_sensor.floor_3_heating_call": "floor_3",
}
COOLING_CALL_ENTITIES = {
    "binary_sensor.floor_1_cooling_call": "floor_1",
    "binary_sensor.floor_2_cooling_call": "floor_2",
    "binary_sensor.floor_3_cooling_call": "floor_3",
}
CLIMATE_ENTITIES = {
    "climate.floor_1_thermostat": "floor_1",
    "climate.floor_2_thermostat": "floor_2",
    "climate.floor_3_thermostat": "floor_3",
}

HEATING_TARGET_SCHEMA = "homeops.consumer.zone_time_to_temp.v1"
HEATING_MISS_SCHEMA = "homeops.consumer.zone_setpoint_miss.v1"
HEATING_OVERSHOOT_SCHEMA = "homeops.consumer.zone_overshoot.v1"
COOLING_START_SCHEMA = "homeops.consumer.thermostat_cooling_session_started.v1"
COOLING_END_SCHEMA = "homeops.consumer.thermostat_cooling_session_ended.v1"
COOLING_TARGET_SCHEMA = "homeops.consumer.zone_time_to_cool.v1"
COOLING_MISS_SCHEMA = "homeops.consumer.zone_cooling_setpoint_miss.v1"
COOLING_UNDERSHOOT_SCHEMA = "homeops.consumer.zone_cooling_undershoot.v1"

COOLING_SESSION_SCHEMAS = {COOLING_START_SCHEMA, COOLING_END_SCHEMA}
OUTCOME_SCHEMAS = {
    HEATING_TARGET_SCHEMA,
    HEATING_MISS_SCHEMA,
    HEATING_OVERSHOOT_SCHEMA,
    COOLING_TARGET_SCHEMA,
    COOLING_MISS_SCHEMA,
    COOLING_UNDERSHOOT_SCHEMA,
}
ACTIVE_ACTIONS = {"heating": "heat", "cooling": "cool"}
OUTDOOR_TEMP_STALE_S = 10_800
EPSILON = 1e-9


def _parse_timestamp(value: Any) -> datetime | None:
    """Return a timezone-aware UTC timestamp, or None for malformed input."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(timestamp: datetime | None) -> str | None:
    return timestamp.isoformat() if timestamp is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _state_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"on", "true", "1", "yes"}:
            return True
        if normalized in {"off", "false", "0", "no"}:
            return False
    return None


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _event_timestamp(event: dict[str, Any], schema: str) -> datetime | None:
    data = _event_data(event)
    if schema == COOLING_START_SCHEMA:
        return _parse_timestamp(data.get("started_at")) or _parse_timestamp(event.get("ts"))
    if schema == COOLING_END_SCHEMA:
        return _parse_timestamp(data.get("ended_at")) or _parse_timestamp(event.get("ts"))
    return _parse_timestamp(event.get("ts"))


def _event_timestamp_text(event: dict[str, Any], schema: str) -> str | None:
    data = _event_data(event)
    if schema == COOLING_START_SCHEMA and isinstance(data.get("started_at"), str):
        return data["started_at"]
    if schema == COOLING_END_SCHEMA and isinstance(data.get("ended_at"), str):
        return data["ended_at"]
    value = event.get("ts")
    return value if isinstance(value, str) else None


def _active_action(data: dict[str, Any]) -> str | None:
    """Return an explicit active HVAC action in canonical form.

    Observer climate events store ``hvac_action`` under ``attributes`` while
    derived events store it directly in their data. Only active actions are
    provenance evidence here; an ``idle`` end-state is not evidence that a
    session was active in either mode.
    """

    attributes = data.get("attributes")
    candidates: list[Any] = []
    if isinstance(attributes, dict):
        candidates.append(attributes.get("hvac_action"))
    candidates.extend((data.get("hvac_action"), data.get("active_action")))
    for value in candidates:
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if normalized in {"heat", "heating"}:
            return "heating"
        if normalized in {"cool", "cooling"}:
            return "cooling"
    return None


@dataclass(frozen=True)
class SourceEvent:
    source: str
    line: int
    event: dict[str, Any]
    schema: str
    data: dict[str, Any]
    timestamp: datetime | None

    def reference(self) -> dict[str, Any]:
        """Return a stable, non-fabricated reference to the source event."""

        event_id = self.event.get("event_id") or self.event.get("id") or self.data.get("event_id")
        reference = {
            "source": self.source,
            "line": self.line,
            "schema": self.schema,
            "event_id": event_id,
            "timestamp": _event_timestamp_text(self.event, self.schema),
        }
        action = _active_action(self.data)
        if action is not None:
            reference["hvac_action"] = action
        return reference

    def key(self) -> tuple[str, int]:
        return self.source, self.line


@dataclass
class OutdoorReading:
    temperature_f: float
    timestamp: datetime
    event: SourceEvent


@dataclass
class Session:
    zone: str
    mode: str
    start_ts: datetime | None
    start_event: SourceEvent | None
    start_temp_f: float | None
    start_setpoint_f: float | None
    start_other_zones: list[str] | None
    start_outdoor: OutdoorReading | None
    end_ts: datetime | None = None
    end_event: SourceEvent | None = None
    end_temp_f: float | None = None
    target_crossing_ts: datetime | None = None
    target_event: SourceEvent | None = None
    setpoint_change_ts: datetime | None = None
    start_boundary_missing: bool = False
    last_temp_f: float | None = None
    observed_duration_s: float | None = None
    outcome_types: set[str] = field(default_factory=set)
    outcome_events: list[SourceEvent] = field(default_factory=list)
    source_start_events: list[SourceEvent] = field(default_factory=list)
    source_end_events: list[SourceEvent] = field(default_factory=list)
    experiment_marker_events: list[SourceEvent] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start_event is not None:
            self.source_start_events.append(self.start_event)
        if self.end_event is not None:
            self.source_end_events.append(self.end_event)
        self.last_temp_f = self.start_temp_f

    @property
    def candidate_start_ts(self) -> datetime | None:
        """Use a candidate event timestamp only for matching, never as a boundary."""

        if self.start_ts is not None:
            return self.start_ts
        return self.start_event.timestamp if self.start_event is not None else None


def _append_unique(events: list[SourceEvent], event: SourceEvent | None) -> None:
    if event is None:
        return
    if all(existing.key() != event.key() for existing in events):
        events.append(event)


def _copy_metadata(session: Session, data: dict[str, Any]) -> None:
    for key in (
        "experiment_id",
        "experiment_name",
        "operation_type",
        "test_id",
        "intervention",
    ):
        if key in data:
            session.metadata[key] = data[key]
    experiment = data.get("experiment")
    if isinstance(experiment, dict):
        session.metadata["experiment"] = experiment


def _normalize_zone_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple, set)):
        return None
    entity_to_zone = {**HEATING_CALL_ENTITIES, **COOLING_CALL_ENTITIES}
    result = sorted(
        {entity_to_zone.get(item, item) for item in value if isinstance(item, str) and item}
    )
    return result


def _call_snapshot(
    mode: str,
    zone: str,
    call_states: dict[str, dict[str, bool | None]],
) -> list[str] | None:
    entity_map = HEATING_CALL_ENTITIES if mode == "heat" else COOLING_CALL_ENTITIES
    states = call_states[mode]
    other_zones: list[str] = []
    for entity_id, other_zone in entity_map.items():
        if other_zone == zone:
            continue
        state = states.get(entity_id)
        if state is None:
            return None
        if state:
            other_zones.append(other_zone)
    return sorted(other_zones)


def _outdoor_as_of(latest: OutdoorReading | None, timestamp: datetime) -> OutdoorReading | None:
    if latest is None or latest.timestamp > timestamp:
        return None
    if (timestamp - latest.timestamp).total_seconds() > OUTDOOR_TEMP_STALE_S:
        return None
    return latest


def _climate_values(event: SourceEvent) -> tuple[float | None, float | None, str | None]:
    attributes = event.data.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    current_temp = _number(attributes.get("current_temperature", attributes.get("temperature_f")))
    setpoint = _number(attributes.get("temperature", attributes.get("setpoint")))
    action = attributes.get("hvac_action")
    action = action.strip().lower() if isinstance(action, str) else None
    return current_temp, setpoint, action


def _new_raw_session(
    event: SourceEvent,
    zone: str,
    mode: str,
    previous_action: str | None,
    call_states: dict[str, dict[str, bool | None]],
    latest_outdoor: OutdoorReading | None,
) -> Session:
    current_temp, setpoint, _ = _climate_values(event)
    active_action = "heating" if mode == "heat" else "cooling"
    valid_start = previous_action is not None and previous_action != active_action
    start_ts = event.timestamp if valid_start else None
    session = Session(
        zone=zone,
        mode=mode,
        start_ts=start_ts,
        start_event=event,
        start_temp_f=current_temp,
        start_setpoint_f=setpoint,
        start_other_zones=_call_snapshot(mode, zone, call_states),
        start_outdoor=_outdoor_as_of(latest_outdoor, event.timestamp)
        if event.timestamp is not None
        else None,
        start_boundary_missing=not valid_start,
    )
    if not valid_start:
        session.outcome_types.add("missing_start_boundary")
    return session


def _new_derived_session(
    event: SourceEvent,
    zone: str,
    mode: str,
    start_ts: datetime | None,
    start_temp_f: float | None,
    setpoint_f: float | None,
    other_zones: list[str] | None,
) -> Session:
    session = Session(
        zone=zone,
        mode=mode,
        start_ts=start_ts,
        start_event=event,
        start_temp_f=start_temp_f,
        start_setpoint_f=setpoint_f,
        start_other_zones=other_zones,
        start_outdoor=None,
        start_boundary_missing=start_ts is None,
    )
    _copy_metadata(session, event.data)
    if start_ts is None:
        session.outcome_types.add("missing_start_boundary")
    return session


def _directional_delta(session: Session) -> float | None:
    if session.start_temp_f is None or session.start_setpoint_f is None:
        return None
    if session.mode == "heat":
        return session.start_setpoint_f - session.start_temp_f
    return session.start_temp_f - session.start_setpoint_f


def _observe_active(session: Session, event: SourceEvent) -> None:
    current_temp, setpoint, _ = _climate_values(event)
    if (
        setpoint is not None
        and session.start_setpoint_f is not None
        and abs(setpoint - session.start_setpoint_f) > EPSILON
        and session.setpoint_change_ts is None
    ):
        session.setpoint_change_ts = event.timestamp

    previous_temp = session.last_temp_f
    session.last_temp_f = current_temp
    if (
        session.target_crossing_ts is not None
        or session.start_boundary_missing
        or session.setpoint_change_ts is not None
        or current_temp is None
        or session.start_setpoint_f is None
        or previous_temp is None
    ):
        return

    delta = _directional_delta(session)
    if delta is None or delta <= EPSILON:
        return
    target = session.start_setpoint_f
    crossed = (session.mode == "heat" and previous_temp < target and current_temp >= target) or (
        session.mode == "cool" and previous_temp > target and current_temp <= target
    )
    if crossed:
        session.target_crossing_ts = event.timestamp
        session.target_event = event


def _close_session(session: Session, event: SourceEvent) -> None:
    if event.timestamp is not None:
        session.end_ts = event.timestamp
    session.end_event = event
    current_temp, _, _ = _climate_values(event)
    if current_temp is not None:
        session.end_temp_f = current_temp
    _append_unique(session.source_end_events, event)


def _build_raw_sessions(events: Iterable[SourceEvent]) -> list[Session]:
    call_states: dict[str, dict[str, bool | None]] = {
        "heat": {},
        "cool": {},
    }
    previous_action: dict[str, str | None] = {}
    latest_outdoor: OutdoorReading | None = None
    active: dict[str, Session] = {}
    sessions: list[Session] = []

    ordered = sorted(
        (event for event in events if event.timestamp is not None),
        key=lambda event: (event.timestamp, event.line),
    )
    for event in ordered:
        if event.schema != OBSERVER_STATE_SCHEMA:
            continue
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            continue

        if entity_id in HEATING_CALL_ENTITIES:
            call_states["heat"][entity_id] = _state_value(event.data.get("new_state"))
            continue
        if entity_id in COOLING_CALL_ENTITIES:
            call_states["cool"][entity_id] = _state_value(event.data.get("new_state"))
            continue
        if entity_id == "sensor.outdoor_temperature":
            value = _number(event.data.get("new_state"))
            if value is None and isinstance(event.data.get("attributes"), dict):
                value = _number(event.data["attributes"].get("temperature"))
            if value is not None and event.timestamp is not None:
                latest_outdoor = OutdoorReading(value, event.timestamp, event)
            continue
        if entity_id not in CLIMATE_ENTITIES:
            continue

        zone = CLIMATE_ENTITIES[entity_id]
        _, _, action = _climate_values(event)
        previous = previous_action.get(zone)
        current_session = active.get(zone)
        if action in ACTIVE_ACTIONS:
            mode = ACTIVE_ACTIONS[action]
            if current_session is None:
                active[zone] = _new_raw_session(
                    event,
                    zone,
                    mode,
                    previous,
                    call_states,
                    latest_outdoor,
                )
            elif current_session.mode != mode:
                _observe_active(current_session, event)
                _close_session(current_session, event)
                sessions.append(current_session)
                active[zone] = _new_raw_session(
                    event,
                    zone,
                    mode,
                    previous,
                    call_states,
                    latest_outdoor,
                )
            else:
                _observe_active(current_session, event)
        elif action is not None and current_session is not None:
            _observe_active(current_session, event)
            _close_session(current_session, event)
            sessions.append(current_session)
            active.pop(zone, None)

        if action is not None:
            previous_action[zone] = action

    sessions.extend(active.values())
    return sessions


def _build_derived_cooling_sessions(events: Iterable[SourceEvent]) -> list[Session]:
    starts: dict[str, deque[Session]] = defaultdict(deque)
    sessions: list[Session] = []
    ordered = sorted(
        (
            event
            for event in events
            if event.schema in COOLING_SESSION_SCHEMAS and event.timestamp is not None
        ),
        key=lambda event: (event.timestamp, event.line),
    )
    for event in ordered:
        data = event.data
        zone = data.get("zone")
        if not isinstance(zone, str):
            continue
        if event.schema == COOLING_START_SCHEMA:
            session = _new_derived_session(
                event=event,
                zone=zone,
                mode="cool",
                start_ts=_parse_timestamp(data.get("started_at")),
                start_temp_f=_number(data.get("current_temp")),
                setpoint_f=_number(data.get("setpoint")),
                other_zones=_normalize_zone_list(data.get("other_zones_calling")),
            )
            session.observed_duration_s = _number(data.get("duration_s"))
            starts[zone].append(session)
            continue

        if starts[zone]:
            session = starts[zone].popleft()
        else:
            session = _new_derived_session(
                event=event,
                zone=zone,
                mode="cool",
                start_ts=None,
                start_temp_f=_number(data.get("start_temp")),
                setpoint_f=_number(data.get("setpoint")),
                other_zones=_normalize_zone_list(data.get("other_zones_calling")),
            )
        session.end_ts = _parse_timestamp(data.get("ended_at")) or event.timestamp
        session.end_event = event
        session.end_temp_f = _number(data.get("current_temp", data.get("end_temp")))
        session.observed_duration_s = _number(data.get("duration_s"))
        _copy_metadata(session, data)
        _append_unique(session.source_end_events, event)
        sessions.append(session)

    for pending in starts.values():
        sessions.extend(pending)
    return sessions


def _same_start(left: Session, right: Session) -> bool:
    left_ts = left.candidate_start_ts
    right_ts = right.candidate_start_ts
    if left_ts is None or right_ts is None:
        return False
    return abs((left_ts - right_ts).total_seconds()) <= 2


def _merge_session(base: Session, overlay: Session) -> None:
    if base.start_ts is None and overlay.start_ts is not None:
        base.start_ts = overlay.start_ts
        base.start_boundary_missing = False
    if base.start_temp_f is None:
        base.start_temp_f = overlay.start_temp_f
    if base.start_setpoint_f is None:
        base.start_setpoint_f = overlay.start_setpoint_f
    if overlay.start_other_zones is not None:
        base.start_other_zones = overlay.start_other_zones
    if base.end_ts is None:
        base.end_ts = overlay.end_ts
    if base.end_event is None:
        base.end_event = overlay.end_event
    if base.end_temp_f is None:
        base.end_temp_f = overlay.end_temp_f
    if base.observed_duration_s is None:
        base.observed_duration_s = overlay.observed_duration_s
    base.metadata.update(overlay.metadata)
    for event in overlay.source_start_events:
        _append_unique(base.source_start_events, event)
    for event in overlay.source_end_events:
        _append_unique(base.source_end_events, event)
    if overlay.start_boundary_missing:
        base.outcome_types.add("missing_start_boundary")
    else:
        base.outcome_types.discard("missing_start_boundary")


def _find_session(
    sessions: list[Session],
    zone: str,
    mode: str,
    timestamp: datetime | None,
) -> Session | None:
    if timestamp is None:
        return None
    candidates = [
        session
        for session in sessions
        if session.zone == zone
        and session.mode == mode
        and (
            session.candidate_start_ts is None
            or timestamp >= session.candidate_start_ts - timedelta(seconds=2)
        )
        and (session.end_ts is None or timestamp <= session.end_ts + timedelta(seconds=2))
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda session: session.candidate_start_ts or datetime.min.replace(tzinfo=UTC),
    )


def _outcome_mode(event: SourceEvent) -> str | None:
    if event.schema in {COOLING_TARGET_SCHEMA, COOLING_MISS_SCHEMA, COOLING_UNDERSHOOT_SCHEMA}:
        return "cool"
    if event.schema in {HEATING_TARGET_SCHEMA, HEATING_MISS_SCHEMA, HEATING_OVERSHOOT_SCHEMA}:
        return "heat"
    return None


def _apply_outcomes(
    sessions: list[Session],
    events: Iterable[SourceEvent],
) -> None:
    ordered = sorted(
        (event for event in events if event.schema in OUTCOME_SCHEMAS),
        key=lambda event: (event.timestamp or datetime.max.replace(tzinfo=UTC), event.line),
    )
    for event in ordered:
        zone = event.data.get("zone")
        mode = _outcome_mode(event)
        if not isinstance(zone, str) or mode is None:
            continue
        session = _find_session(sessions, zone, mode, event.timestamp)
        if session is None:
            session = _new_derived_session(
                event=event,
                zone=zone,
                mode=mode,
                start_ts=None,
                start_temp_f=_number(event.data.get("start_temp")),
                setpoint_f=_number(event.data.get("setpoint")),
                other_zones=None,
            )
            sessions.append(session)

        _copy_metadata(session, event.data)
        _append_unique(session.outcome_events, event)
        if event.schema in {HEATING_TARGET_SCHEMA, COOLING_TARGET_SCHEMA}:
            session.outcome_types.add("target_reached")
            if session.target_crossing_ts is None:
                session.target_crossing_ts = event.timestamp
                session.target_event = event
            if session.observed_duration_s is None:
                session.observed_duration_s = _number(event.data.get("duration_s"))
        else:
            if event.schema in {HEATING_MISS_SCHEMA, COOLING_MISS_SCHEMA}:
                session.outcome_types.add("setpoint_miss")
            else:
                session.outcome_types.add("overshoot" if mode == "heat" else "undershoot")
            if session.end_ts is None and event.timestamp is not None:
                session.end_ts = event.timestamp
                session.end_event = event
            if session.end_temp_f is None:
                session.end_temp_f = _number(
                    event.data.get("end_temp", event.data.get("closest_temp"))
                )
            if session.observed_duration_s is None:
                session.observed_duration_s = _number(event.data.get("duration_s"))


def _merge_cooling_sessions(
    raw_sessions: list[Session],
    derived_sessions: list[Session],
) -> list[Session]:
    sessions = list(raw_sessions)
    for derived in derived_sessions:
        match = next(
            (
                raw
                for raw in sessions
                if raw.mode == "cool" and raw.zone == derived.zone and _same_start(raw, derived)
            ),
            None,
        )
        if match is None:
            sessions.append(derived)
        else:
            _merge_session(match, derived)
    return sessions


def _build_experiment_runs(events: Iterable[SourceEvent]) -> list[dict[str, Any]]:
    """Reconstruct marker intervals from the append-only experiment log."""

    runs: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        (event for event in events if event.schema == EXPERIMENT_MARKER_SCHEMA),
        key=lambda event: (event.timestamp or datetime.max.replace(tzinfo=UTC), event.line),
    )
    for event in ordered:
        data = event.data
        experiment_id = data.get("experiment_id")
        action = data.get("action")
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            continue
        if action in {"start", "retroactive"}:
            run = dict(data)
            run["marker_events"] = [event]
            runs[experiment_id] = run
            continue
        if action not in {"end", "abort"} or experiment_id not in runs:
            continue
        run = runs[experiment_id]
        run["status"] = "completed" if action == "end" else "aborted"
        run["end_ts"] = data.get("end_ts")
        run["received_at"] = data.get("received_at")
        if action == "abort":
            run["abort_reason"] = data.get("abort_reason")
        _append_unique(run["marker_events"], event)
    return list(runs.values())


def _experiment_interval(run: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Return the declared marker interval, or None when it cannot be bounded."""

    start = _parse_timestamp(run.get("start_ts"))
    if start is None:
        return None
    end = _parse_timestamp(run.get("end_ts"))
    if end is None:
        duration = _number(run.get("planned_duration_s"))
        if duration is None or duration <= 0:
            return None
        end = start + timedelta(seconds=duration)
    if end < start:
        return None
    return start, end


def _experiment_metadata(run: dict[str, Any]) -> dict[str, Any]:
    """Select marker metadata that belongs in training-row provenance."""

    metadata: dict[str, Any] = {}
    for key in (
        "experiment_id",
        "configuration_id",
        "experiment_name",
        "test_id",
        "operation_type",
        "mode",
        "active_zones",
        "suppressed_zones",
        "planned_duration_s",
        "target_f",
        "start_ts",
        "end_ts",
        "status",
        "confidence",
        "source_type",
        "duration_defaulted",
        "abort_reason",
    ):
        if key in run:
            metadata[key] = run[key]
    if "intervention" in run:
        metadata["intervention"] = run["intervention"]
    return metadata


def _marker_overlaps_session(run: dict[str, Any], session: Session) -> bool:
    interval = _experiment_interval(run)
    session_start = session.candidate_start_ts
    if interval is None or session_start is None:
        return False
    marker_start, marker_end = interval
    session_end = session.end_ts or session_start
    tolerance = timedelta(seconds=EXPERIMENT_MARKER_MATCH_TOLERANCE_S)
    return session_start <= marker_end + tolerance and session_end >= marker_start - tolerance


def _apply_experiment_markers(
    sessions: list[Session],
    events: Iterable[SourceEvent],
) -> None:
    """Attach bounded operator provenance to active-floor sessions."""

    for run in _build_experiment_runs(events):
        active_zones = run.get("active_zones")
        mode = run.get("mode")
        if (
            not isinstance(active_zones, list)
            or mode not in {"heat", "cool"}
            or run.get("status") not in {"active", "completed", "aborted", "needs_review"}
        ):
            continue
        metadata = _experiment_metadata(run)
        for session in sessions:
            if (
                session.mode != mode
                or session.zone not in active_zones
                or not _marker_overlaps_session(run, session)
            ):
                continue
            existing_id = session.metadata.get("experiment_id")
            marker_id = metadata.get("experiment_id")
            if existing_id is not None and existing_id != marker_id:
                session.outcome_types.add("multiple_experiment_markers")
                continue
            session.metadata.update(metadata)
            for event in run["marker_events"]:
                _append_unique(session.experiment_marker_events, event)


def _local_minute(timestamp: datetime | None, timezone: ZoneInfo) -> int | None:
    if timestamp is None:
        return None
    local = timestamp.astimezone(timezone)
    return local.hour * 60 + local.minute


def _target_status(session: Session) -> str:
    if session.start_boundary_missing or session.start_ts is None:
        return "missing_start_boundary"
    delta = _directional_delta(session)
    if delta is None:
        return "missing_measurement"
    if delta < -EPSILON:
        return "invalid_direction"
    if abs(delta) <= EPSILON:
        return "already_at_target"
    if _setpoint_changed_before_target(session):
        return "setpoint_changed"
    if session.target_crossing_ts is not None and session.target_crossing_ts > session.start_ts:
        return "eligible"
    return "right_censored"


def _runtime_status(session: Session) -> str:
    if session.start_boundary_missing or session.start_ts is None:
        return "missing_start_boundary"
    if session.end_ts is None:
        return "right_censored"
    if session.end_ts <= session.start_ts:
        return "invalid_timestamp"
    return "eligible"


def _setpoint_changed_before_target(session: Session) -> bool:
    if session.setpoint_change_ts is None:
        return False
    return (
        session.target_crossing_ts is None
        or session.setpoint_change_ts <= session.target_crossing_ts
    )


def _quality_flags(
    session: Session,
    target_status: str,
    runtime_status: str,
) -> list[str]:
    flags: set[str] = set()
    if session.start_boundary_missing:
        flags.add("missing_start_boundary")
    if session.end_ts is None:
        flags.add("missing_end_boundary")
    if _setpoint_changed_before_target(session):
        flags.add("setpoint_changed_during_session")
    if session.start_temp_f is None or session.start_setpoint_f is None:
        flags.add("missing_start_measurement")
    if session.start_outdoor is None:
        flags.add("missing_outdoor_temperature")
    if session.start_other_zones is None:
        flags.add("missing_cross_zone_snapshot")
    if target_status in {"invalid_direction", "missing_measurement"}:
        flags.add("invalid_target_inputs")
    if runtime_status == "invalid_timestamp":
        flags.add("invalid_session_timestamps")
    return sorted(flags)


def _source_events(session: Session) -> list[SourceEvent]:
    events: list[SourceEvent] = []
    for event in session.source_start_events + session.source_end_events + session.outcome_events:
        _append_unique(events, event)
    if session.start_outdoor is not None:
        _append_unique(events, session.start_outdoor.event)
    return sorted(
        events,
        key=lambda event: (
            event.timestamp or datetime.max.replace(tzinfo=UTC),
            event.source,
            event.line,
        ),
    )


def _row_id(session: Session) -> str:
    timestamp = session.start_ts or session.candidate_start_ts
    if timestamp is None:
        source = next(iter(_source_events(session)), None)
        suffix = f"{source.source}:{source.line}" if source else "unknown"
        return f"{session.zone}:{session.mode}:missing-start:{suffix}"
    return f"{session.zone}:{session.mode}:{timestamp.isoformat()}"


def _runtime_history(
    sessions: list[Session],
    target_start: datetime,
    zone: str,
    mode: str,
) -> float | None:
    window_start = target_start - timedelta(hours=24)
    total = 0.0
    for session in sessions:
        if (
            session.zone != zone
            or session.mode != mode
            or session.start_ts is None
            or session.end_ts is None
        ):
            continue
        if not window_start <= session.start_ts < target_start:
            continue
        if session.end_ts <= target_start and session.end_ts > session.start_ts:
            total += (session.end_ts - session.start_ts).total_seconds()
    return round(total, 3)


def _session_to_row(
    session: Session,
    all_sessions: list[Session],
    timezone: ZoneInfo,
    history_complete: bool,
) -> dict[str, Any]:
    target_status = _target_status(session)
    runtime_status = _runtime_status(session)
    target_label = (
        round((session.target_crossing_ts - session.start_ts).total_seconds(), 3)
        if target_status == "eligible"
        else None
    )
    runtime_label = (
        round((session.end_ts - session.start_ts).total_seconds(), 3)
        if runtime_status == "eligible"
        else None
    )
    other_zones = session.start_other_zones
    concurrent_count = len(other_zones) if other_zones is not None else None
    history_available = history_complete and session.start_ts is not None
    prior_runtime = (
        _runtime_history(all_sessions, session.start_ts, session.zone, session.mode)
        if history_available
        else None
    )
    source_events = _source_events(session)
    source_refs = [event.reference() for event in source_events]
    provenance: dict[str, Any] = {
        "source_events": source_refs,
        "start_boundary": "observed"
        if session.start_ts is not None and not session.start_boundary_missing
        else "missing",
    }
    if session.metadata:
        provenance["experiment"] = session.metadata
    if session.experiment_marker_events:
        provenance["experiment_marker_events"] = [
            event.reference() for event in session.experiment_marker_events
        ]

    row = {
        "schema": TRAINING_ROW_SCHEMA,
        "row_id": _row_id(session),
        "zone": session.zone,
        "mode": session.mode,
        "prediction_ts": _iso(session.start_ts),
        "active_start_ts": _iso(session.start_ts),
        "active_end_ts": _iso(session.end_ts),
        "target_crossing_ts": _iso(session.target_crossing_ts),
        "features": {
            "start_temp_f": session.start_temp_f,
            "start_setpoint_f": session.start_setpoint_f,
            "setpoint_delta_f": _directional_delta(session),
            "outdoor_temp_f": (
                session.start_outdoor.temperature_f if session.start_outdoor else None
            ),
            "outdoor_temp_age_s": (
                round(
                    (session.start_ts - session.start_outdoor.timestamp).total_seconds(),
                    3,
                )
                if session.start_ts is not None and session.start_outdoor is not None
                else None
            ),
            "other_zones_calling": other_zones,
            "concurrent_zone_count": concurrent_count,
            "start_minute_of_day_local": _local_minute(session.start_ts, timezone),
            "prior_zone_runtime_24h_s": prior_runtime,
            "prior_zone_runtime_history_complete": history_available,
        },
        "labels": {
            "time_to_setpoint_s": target_label,
            "zone_runtime_s": runtime_label,
        },
        "label_status": {
            "time_to_setpoint": target_status,
            "zone_runtime": runtime_status,
        },
        "observations": {
            "end_temp_f": session.end_temp_f,
            "observed_duration_s": session.observed_duration_s,
            "outcome_types": sorted(session.outcome_types),
        },
        "quality_flags": _quality_flags(session, target_status, runtime_status),
        "provenance": provenance,
    }
    return row


def build_dataset(
    observer_events: Iterable[SourceEvent],
    derived_events: Iterable[SourceEvent],
    *,
    timezone: ZoneInfo = ZoneInfo("America/New_York"),
    history_complete: bool = False,
    experiment_events: Iterable[SourceEvent] | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic rows from already-loaded observer and derived events."""

    observer_events = list(observer_events)
    derived_events = list(derived_events)
    raw_sessions = _build_raw_sessions(observer_events)
    cooling_sessions = _build_derived_cooling_sessions(derived_events)
    sessions = _merge_cooling_sessions(raw_sessions, cooling_sessions)
    _apply_outcomes(sessions, derived_events)
    _apply_experiment_markers(sessions, experiment_events or [])
    rows = [
        _session_to_row(session, sessions, timezone, history_complete)
        for session in sessions
        if session.zone and session.mode in {"heat", "cool"}
    ]
    rows.sort(
        key=lambda row: (
            row["prediction_ts"] is None,
            row["prediction_ts"] or "",
            row["zone"],
            row["mode"],
            row["row_id"],
        )
    )
    return rows


def load_jsonl_events(path: Path, source: str) -> tuple[list[SourceEvent], dict[str, int]]:
    """Load JSONL while retaining line-level provenance and basic read counts."""

    stats = {"lines": 0, "malformed": 0, "non_object": 0, "invalid_timestamp": 0}
    events: list[SourceEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stats["lines"] += 1
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed"] += 1
                continue
            if not isinstance(event, dict):
                stats["non_object"] += 1
                continue
            schema = event.get("schema")
            if not isinstance(schema, str):
                stats["malformed"] += 1
                continue
            data = _event_data(event)
            timestamp = _event_timestamp(event, schema)
            if timestamp is None:
                stats["invalid_timestamp"] += 1
            events.append(
                SourceEvent(
                    source=source,
                    line=line_number,
                    event=event,
                    schema=schema,
                    data=data,
                    timestamp=timestamp,
                )
            )
    return events, stats


def load_optional_jsonl_events(
    path: Path,
    source: str,
) -> tuple[list[SourceEvent], dict[str, int]]:
    """Load an optional sidecar log without breaking histories collected before it existed."""

    if not path.exists():
        return [], {
            "lines": 0,
            "malformed": 0,
            "non_object": 0,
            "invalid_timestamp": 0,
            "missing": 1,
        }
    events, stats = load_jsonl_events(path, source)
    stats["missing"] = 0
    return events, stats


def _write_rows(rows: Iterable[dict[str, Any]], output: TextIO) -> None:
    for row in rows:
        output.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
        output.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export deterministic HomeOps heat/cooling training rows."
    )
    parser.add_argument(
        "--observer-log",
        type=Path,
        default=Path("state/observer/events.jsonl"),
        help="Raw observer JSONL path.",
    )
    parser.add_argument(
        "--derived-log",
        type=Path,
        default=Path("state/consumer/events.jsonl"),
        help="Consumer-derived JSONL path.",
    )
    parser.add_argument(
        "--experiment-log",
        type=Path,
        default=Path("state/experiments/markers.jsonl"),
        help="Optional natural-language experiment marker JSONL path.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="-",
        help="Output JSONL path, or '-' for stdout.",
    )
    parser.add_argument(
        "--timezone",
        default="America/New_York",
        help="IANA timezone used for local calendar features.",
    )
    parser.add_argument(
        "--history-complete",
        action="store_true",
        help="Mark the supplied logs complete and calculate prior 24-hour runtime.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        timezone = ZoneInfo(args.timezone)
        observer_events, observer_stats = load_jsonl_events(args.observer_log, "observer")
        derived_events, derived_stats = load_jsonl_events(args.derived_log, "derived")
        experiment_events, experiment_stats = load_optional_jsonl_events(
            args.experiment_log, "experiment"
        )
        rows = build_dataset(
            observer_events,
            derived_events,
            timezone=timezone,
            history_complete=args.history_complete,
            experiment_events=experiment_events,
        )
    except (OSError, ValueError) as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 2

    if args.out == "-":
        _write_rows(rows, sys.stdout)
    else:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as output:
            _write_rows(rows, output)

    summary = {
        "observer": observer_stats,
        "derived": derived_stats,
        "experiment": experiment_stats,
        "rows": len(rows),
    }
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
