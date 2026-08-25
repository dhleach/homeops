#!/usr/bin/env python3
"""Build a bounded, read-only multi-zone heating schedule recommendation.

The scheduler composes the existing per-zone time-to-temperature and
cooling-curve reports with the configured floor-2 long-call warning threshold.
It can recommend when a floor-2 call would need to start to reach a target by a
deadline, plus conservative setpoint ceilings for floors 1 and 3 during that
call.  It fails closed when the history cannot support a defensible number.

This is a planning tool, not a thermostat controller.  It never calls Home
Assistant, writes consumer state, emits an event, or treats a historical model
as a guarantee about the future.

Usage::

    python3 scripts/scheduling_query.py \\
        --target 68 --current 65 --outdoor 28 \\
        --by 2026-01-01T07:00:00-05:00 \\
        --floor-1-current 70 --floor-3-current 69 \\
        --start 2025-12-01 --end 2026-01-01 \\
        --log state/consumer/events.jsonl --format text

Revision history:
  2026-08-25  Added a deterministic, read-only multi-zone scheduling query
              that combines the existing time-to-temperature model, conservative
              cooling-curve rates, and the configured floor-2 long-call threshold;
              missing, extrapolated, and unsafe inputs fail closed instead of
              producing invented setpoints or timing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
CONSUMER_DIR = ROOT / "services" / "consumer"
INSIGHTS_DIR = ROOT / "services" / "insights"
for _path in (CONSUMER_DIR, INSIGHTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import thermal_query  # noqa: E402
from rules.config import RulesConfigError, load_rules_config  # noqa: E402

SCHEDULING_SCHEMA = "homeops.multi_zone_schedule.v1"
CURRENT_TEMP_SCHEMA = "homeops.consumer.thermostat_current_temp_updated.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_DAYS = 30
DEFAULT_SAFETY_MARGIN_MINUTES = 5.0
DEFAULT_MAX_SNAPSHOT_AGE_HOURS = 6.0
MAX_HORIZON_HOURS = 48.0
SETPOINT_MARGIN_F = 0.5
SETPOINT_STEP_F = 0.5
KNOWN_ZONES = ("floor_1", "floor_2", "floor_3")
PRIMARY_ZONE = "floor_2"
SECONDARY_ZONES = ("floor_1", "floor_3")
MIN_TEMP_F = 0.0
MAX_TEMP_F = 120.0
MIN_OUTDOOR_TEMP_F = -100.0
MAX_OUTDOOR_TEMP_F = 150.0
MAX_TARGET_DELTA_F = 80.0

TOOL_DEFINITION: dict[str, Any] = {
    "name": "recommend_multi_zone_schedule",
    "description": (
        "Build a bounded, read-only floor-2 heating schedule from historical HomeOps "
        "thermal evidence. It may recommend a floor-2 start time and conservative "
        "floor-1/floor-3 setpoint ceilings, but it never controls thermostats. "
        "Sparse, extrapolated, stale, or unsafe data produces no recommendation."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["target_temp_f", "outdoor_temp_f", "deadline"],
        "properties": {
            "target_temp_f": {
                "type": "number",
                "minimum": MIN_TEMP_F,
                "maximum": MAX_TEMP_F,
                "description": "Desired floor-2 temperature in °F.",
            },
            "outdoor_temp_f": {
                "type": "number",
                "minimum": MIN_OUTDOOR_TEMP_F,
                "maximum": MAX_OUTDOOR_TEMP_F,
                "description": "Outdoor temperature to use for the historical model in °F.",
            },
            "deadline": {
                "type": "string",
                "description": "ISO-8601 deadline by which floor 2 should reach the target.",
            },
            "current_temp_f": {
                "type": "number",
                "minimum": MIN_TEMP_F,
                "maximum": MAX_TEMP_F,
                "description": (
                    "Optional current floor-2 temperature in °F; if omitted, the latest "
                    "fresh thermostat snapshot at or before as_of is used."
                ),
            },
            "floor_1_current_temp_f": {
                "type": "number",
                "minimum": MIN_TEMP_F,
                "maximum": MAX_TEMP_F,
                "description": "Optional current floor-1 temperature in °F.",
            },
            "floor_3_current_temp_f": {
                "type": "number",
                "minimum": MIN_TEMP_F,
                "maximum": MAX_TEMP_F,
                "description": "Optional current floor-3 temperature in °F.",
            },
            "as_of": {
                "type": "string",
                "description": (
                    "Optional ISO-8601 time of the observation; explicit values make replay "
                    "and audit results deterministic."
                ),
            },
        },
    },
}


@dataclass(frozen=True)
class TemperatureSnapshot:
    """One valid thermostat current-temperature snapshot."""

    timestamp: datetime
    zone: str
    temperature_f: float


def _finite_number(value: Any) -> float | None:
    """Return a finite number while excluding booleans and numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_timestamp(value: Any, name: str = "timestamp") -> datetime:
    """Parse an ISO timestamp and normalize naive values to UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_timestamp(event: Mapping[str, Any], data: Mapping[str, Any]) -> datetime | None:
    """Return the first usable timestamp from a current-temperature event."""
    for value in (
        event.get("ts"),
        data.get("ts"),
        data.get("timestamp"),
    ):
        if not isinstance(value, str) or not value:
            continue
        try:
            return _parse_timestamp(value)
        except ValueError:
            continue
    return None


def _event_key(event: Mapping[str, Any]) -> str:
    """Return a stable identity for exact duplicate JSONL records."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def _zone_from_data(data: Mapping[str, Any]) -> str | None:
    """Resolve a canonical floor from explicit or entity-id fields."""
    for key in ("zone", "floor"):
        value = data.get(key)
        if isinstance(value, str) and value in KNOWN_ZONES:
            return value
    entity_id = data.get("entity_id")
    if isinstance(entity_id, str):
        for zone in KNOWN_ZONES:
            if entity_id in {f"climate.{zone}_thermostat", f"sensor.{zone}_temperature"}:
                return zone
    return None


def _validate_temperature(name: str, value: Any) -> float:
    """Validate one thermostat temperature against the shared model bounds."""
    number = _finite_number(value)
    if number is None:
        raise ValueError(f"{name} must be a finite number")
    if not MIN_TEMP_F <= number <= MAX_TEMP_F:
        raise ValueError(f"{name} must be between {MIN_TEMP_F:g} and {MAX_TEMP_F:g}°F")
    return number


def _validate_outdoor_temperature(value: Any) -> float:
    """Validate the outdoor temperature used for model lookup."""
    number = _finite_number(value)
    if number is None:
        raise ValueError("outdoor_temp_f must be a finite number")
    if not MIN_OUTDOOR_TEMP_F <= number <= MAX_OUTDOOR_TEMP_F:
        raise ValueError(
            f"outdoor_temp_f must be between {MIN_OUTDOOR_TEMP_F:g} and {MAX_OUTDOOR_TEMP_F:g}°F"
        )
    return number


def load_current_temperature_snapshots(
    source: str | Path,
) -> tuple[list[TemperatureSnapshot], dict[str, int]]:
    """Load valid thermostat snapshots without modifying the event log."""
    quality = {
        "lines_seen": 0,
        "malformed_json": 0,
        "non_object_events": 0,
        "duplicate_events": 0,
        "relevant_events": 0,
        "events_missing_data": 0,
        "events_missing_timestamp": 0,
        "events_unknown_zone": 0,
        "events_missing_temperature": 0,
        "events_with_invalid_temperature": 0,
    }
    snapshots: list[TemperatureSnapshot] = []
    seen: set[str] = set()
    try:
        with open(source, encoding="utf-8") as event_file:
            for line in event_file:
                quality["lines_seen"] += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    quality["malformed_json"] += 1
                    continue
                if not isinstance(event, dict):
                    quality["non_object_events"] += 1
                    continue
                if event.get("schema") != CURRENT_TEMP_SCHEMA:
                    continue
                quality["relevant_events"] += 1
                key = _event_key(event)
                if key in seen:
                    quality["duplicate_events"] += 1
                    continue
                seen.add(key)
                data = event.get("data")
                if not isinstance(data, dict):
                    quality["events_missing_data"] += 1
                    continue
                timestamp = _event_timestamp(event, data)
                if timestamp is None:
                    quality["events_missing_timestamp"] += 1
                    continue
                zone = _zone_from_data(data)
                if zone is None:
                    quality["events_unknown_zone"] += 1
                    continue
                raw_temperature = data.get("current_temp", data.get("current_temp_f"))
                if raw_temperature is None:
                    quality["events_missing_temperature"] += 1
                    continue
                temperature = _finite_number(raw_temperature)
                if temperature is None or not MIN_TEMP_F <= temperature <= MAX_TEMP_F:
                    quality["events_with_invalid_temperature"] += 1
                    continue
                snapshots.append(TemperatureSnapshot(timestamp, zone, temperature))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"log file not found: {source}") from exc
    except OSError as exc:
        raise OSError(f"error reading log {source}: {exc}") from exc

    snapshots.sort(key=lambda item: (item.timestamp, item.zone, item.temperature_f))
    return snapshots, quality


def _latest_snapshot(
    snapshots: list[TemperatureSnapshot],
    zone: str,
    as_of: datetime,
    max_age_s: float,
) -> TemperatureSnapshot | None:
    """Return the latest fresh snapshot for a zone at or before ``as_of``."""
    candidates = [
        snapshot for snapshot in snapshots if snapshot.zone == zone and snapshot.timestamp <= as_of
    ]
    if not candidates:
        return None
    latest = candidates[-1]
    age_s = (as_of - latest.timestamp).total_seconds()
    return latest if 0 <= age_s <= max_age_s else None


def _resolve_history_range(
    as_of: datetime,
    days: int,
    start: date | None,
    end: date | None,
) -> tuple[date, date]:
    """Resolve an inclusive history range that cannot include future data."""
    if (start is None) != (end is None):
        raise ValueError("start and end must be provided together")
    if start is None:
        if days < 1:
            raise ValueError("days must be at least 1")
        end = as_of.date()
        start = end - timedelta(days=days - 1)
    assert start is not None and end is not None
    if start > end:
        raise ValueError("start date must be on or before end date")
    if end > as_of.date():
        raise ValueError("history end date cannot be after as_of")
    return start, end


def _round(value: float | None, digits: int = 3) -> float | None:
    """Round a finite result while preserving missingness."""
    return round(value, digits) if value is not None else None


def _zone_report(reports: list[dict[str, Any]], zone: str) -> dict[str, Any]:
    """Find one zone report in a compact thermal-query report."""
    return next(
        (report for report in reports if report.get("zone") == zone),
        {"zone": zone, "status": "insufficient_data", "reason": "zone report missing"},
    )


def _heat_loss_rate(zone_report: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """Choose a conservative observed cooling rate for one secondary zone."""
    if zone_report.get("status") != "ok":
        return None, None
    for field, basis in (
        ("p75_heat_loss_rate_f_per_min", "p75"),
        ("median_heat_loss_rate_f_per_min", "median"),
    ):
        rate = _finite_number(zone_report.get(field))
        if rate is not None and rate > 0:
            return rate, basis
    return None, None


def _floor_setpoint(value: float) -> float:
    """Round a ceiling down to the thermostat's half-degree planning grid."""
    return math.floor((value + 1e-9) / SETPOINT_STEP_F) * SETPOINT_STEP_F


def _secondary_projection(
    zone: str,
    current_temp_f: float | None,
    heat_report: Mapping[str, Any],
    window_seconds: float,
) -> tuple[dict[str, Any], str | None, str | None]:
    """Project one secondary zone and return detail, status, and failure reason."""
    heat_zone = _zone_report(heat_report.get("zones", []), zone)
    detail: dict[str, Any] = {
        "zone": zone,
        "current_temp_f": _round(current_temp_f, 1),
        "heat_loss_status": heat_zone.get("status"),
        "heat_loss_reason": heat_zone.get("reason"),
        "heat_loss_rate_f_per_min": None,
        "heat_loss_rate_basis": None,
        "projected_temp_at_deadline_f": None,
        "candidate_max_setpoint_f": None,
        "status": "insufficient_data",
    }
    if current_temp_f is None:
        detail["reason"] = "no fresh current-temperature snapshot or explicit temperature"
        return detail, "insufficient_data", detail["reason"]
    rate, basis = _heat_loss_rate(heat_zone)
    if rate is None:
        detail["reason"] = "secondary zone has no usable qualifying heat-loss rate"
        return detail, "insufficient_data", detail["reason"]

    projected = current_temp_f - rate * window_seconds / 60.0
    candidate_raw = projected - SETPOINT_MARGIN_F
    candidate = _floor_setpoint(candidate_raw)
    detail.update(
        {
            "heat_loss_rate_f_per_min": _round(rate, 5),
            "heat_loss_rate_basis": basis,
            "projected_temp_at_deadline_f": _round(projected, 1),
            "candidate_max_setpoint_f": _round(candidate, 1),
            "setpoint_margin_f": SETPOINT_MARGIN_F,
        }
    )
    if not math.isfinite(projected) or not math.isfinite(candidate):
        detail["reason"] = "heat-loss projection is not finite"
        return detail, "unsafe_to_recommend", detail["reason"]
    if candidate < MIN_TEMP_F:
        detail["reason"] = "projected temperature leaves no valid bounded setpoint ceiling"
        return detail, "unsafe_to_recommend", detail["reason"]
    detail["status"] = "ok"
    detail["instruction"] = (
        f"Keep {zone} at or below {candidate:g}°F and do not allow it to call during "
        "the floor-2 heating window."
    )
    return detail, None, None


def _build_recommendation(
    *,
    target_temp_f: float,
    current_temp_f: float | None,
    deadline: datetime,
    as_of: datetime,
    prediction: Mapping[str, Any] | None,
    heat_report: Mapping[str, Any],
    secondary_current_temps: Mapping[str, float | None],
    threshold: Mapping[str, Any],
    safety_margin_minutes: float,
) -> tuple[str, dict[str, Any] | None, list[str], dict[str, Any]]:
    """Build a schedule or fail closed with explicit analysis reasons."""
    reasons: list[str] = []
    unsafe = False
    primary_analysis: dict[str, Any] = {
        "zone": PRIMARY_ZONE,
        "target_temp_f": _round(target_temp_f, 1),
        "current_temp_f": _round(current_temp_f, 1),
        "rise_f": _round(target_temp_f - current_temp_f, 2) if current_temp_f is not None else None,
        "prediction": dict(prediction) if prediction is not None else None,
        "candidate_start": None,
        "max_safe_duration_s": None,
    }

    threshold_s = _finite_number(threshold.get("threshold_s"))
    if threshold.get("status") != "ok" or not threshold.get("enabled"):
        unsafe = True
        reasons.append("floor-2 long-call safety threshold is unavailable or disabled")
    elif threshold_s is None or threshold_s <= 0:
        unsafe = True
        reasons.append("floor-2 long-call safety threshold is invalid")
    else:
        max_safe_duration_s = threshold_s - safety_margin_minutes * 60.0
        primary_analysis["max_safe_duration_s"] = _round(max_safe_duration_s, 1)
        if max_safe_duration_s <= 0:
            unsafe = True
            reasons.append("configured safety margin leaves no safe continuous-call window")

    predicted_duration_s = (
        _finite_number(prediction.get("predicted_duration_s")) if prediction is not None else None
    )
    prediction_status = prediction.get("status") if prediction is not None else None
    if prediction_status == "extrapolated":
        unsafe = True
        reasons.append("floor-2 prediction is outside the observed training range")
    elif prediction_status == "invalid_model_prediction":
        unsafe = True
        reasons.append("floor-2 model produced an invalid prediction")
    elif prediction_status != "ok":
        reasons.append(
            f"floor-2 time-to-temperature prediction is {prediction_status or 'unavailable'}"
        )
    if predicted_duration_s is None or predicted_duration_s <= 0:
        reasons.append("floor-2 time-to-temperature model cannot produce a positive prediction")

    candidate_start: datetime | None = None
    if predicted_duration_s is not None and predicted_duration_s > 0:
        candidate_start = deadline - timedelta(seconds=predicted_duration_s)
        primary_analysis["candidate_start"] = candidate_start.isoformat()
        if candidate_start <= as_of:
            unsafe = True
            reasons.append("deadline is too soon for the predicted floor-2 heating duration")

    if (
        threshold_s is not None
        and predicted_duration_s is not None
        and predicted_duration_s > 0
        and predicted_duration_s >= threshold_s - safety_margin_minutes * 60.0
    ):
        unsafe = True
        reasons.append(
            "predicted floor-2 call reaches the configured threshold safety reserve; "
            "no schedule is recommended"
        )

    secondary_details: dict[str, Any] = {}
    secondary_statuses: list[str] = []
    window_seconds = predicted_duration_s if predicted_duration_s and candidate_start else 0.0
    for zone in SECONDARY_ZONES:
        detail, status, reason = _secondary_projection(
            zone,
            secondary_current_temps.get(zone),
            heat_report,
            window_seconds,
        )
        secondary_details[zone] = detail
        if status is not None:
            secondary_statuses.append(status)
        if reason:
            reasons.append(f"{zone}: {reason}")

    analysis = {
        "primary": primary_analysis,
        "secondary_zones": secondary_details,
        "safety_threshold": dict(threshold),
        "safety_margin_minutes": _round(safety_margin_minutes, 1),
    }

    if unsafe:
        status = "unsafe_to_recommend"
    elif reasons or secondary_statuses:
        status = "insufficient_data"
    else:
        status = "ready"

    if status != "ready":
        return status, None, reasons, analysis

    assert threshold_s is not None
    assert predicted_duration_s is not None
    assert candidate_start is not None
    assert current_temp_f is not None
    for detail in secondary_details.values():
        detail["blocked_window"] = {
            "start": candidate_start.isoformat(),
            "end": deadline.isoformat(),
        }
        detail["allowed_call_timing"] = {
            "before_primary_start": candidate_start.isoformat(),
            "after_primary_deadline": deadline.isoformat(),
        }
    recommendation = {
        "primary_zone": PRIMARY_ZONE,
        "target_temp_f": _round(target_temp_f, 1),
        "current_temp_f": _round(current_temp_f, 1),
        "predicted_duration_s": _round(predicted_duration_s, 1),
        "predicted_duration_min": _round(predicted_duration_s / 60.0, 1),
        "recommended_start": candidate_start.isoformat(),
        "deadline": deadline.isoformat(),
        "primary_call_window": {
            "start": candidate_start.isoformat(),
            "end": deadline.isoformat(),
        },
        "safety": {
            "configured_long_call_threshold_s": _round(threshold_s, 1),
            "configured_long_call_threshold_min": _round(threshold_s / 60.0, 1),
            "reserved_margin_s": _round(safety_margin_minutes * 60.0, 1),
            "max_recommended_duration_s": _round(threshold_s - safety_margin_minutes * 60.0, 1),
        },
        "secondary_zones": secondary_details,
        "instructions": [
            (
                f"Start {PRIMARY_ZONE} at {candidate_start.isoformat()} and allow it to call "
                f"until the {deadline.isoformat()} deadline."
            ),
            "Do not allow floors 1 or 3 to call during the primary floor-2 window.",
        ],
    }
    return status, recommendation, reasons, analysis


def _threshold_settings(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Load the validated floor-2 long-call threshold used by the consumer."""
    try:
        config = load_rules_config(path)
        rule = config.rule("floor_2_long_call")
    except RulesConfigError as exc:
        return {
            "status": "unavailable",
            "enabled": False,
            "threshold_s": None,
            "reason": f"could not load validated safety configuration: {exc}",
        }
    threshold_minutes = _finite_number(rule.get("threshold_minutes"))
    return {
        "status": "ok" if threshold_minutes is not None and threshold_minutes > 0 else "invalid",
        "rule": "rules.floor_2_long_call",
        "enabled": bool(rule.get("enabled")),
        "threshold_minutes": _round(threshold_minutes, 1),
        "threshold_s": _round(threshold_minutes * 60.0, 1)
        if threshold_minutes is not None
        else None,
        "source": str(config.path),
    }


def build_schedule_query(
    target_temp_f: float,
    outdoor_temp_f: float,
    deadline: datetime | str,
    *,
    current_temp_f: float | None = None,
    floor_1_current_temp_f: float | None = None,
    floor_3_current_temp_f: float | None = None,
    as_of: datetime | str | None = None,
    log_path: str | Path = DEFAULT_LOG,
    days: int = DEFAULT_DAYS,
    start: date | None = None,
    end: date | None = None,
    rules_config_path: str | os.PathLike[str] | None = None,
    safety_margin_minutes: float = DEFAULT_SAFETY_MARGIN_MINUTES,
    max_snapshot_age_hours: float = DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
    min_time_to_temp_observations: int = 5,
    min_heat_loss_observations: int = 3,
    min_runtime_observations: int = 3,
    max_evidence_events: int = thermal_query.DEFAULT_MAX_EVIDENCE_EVENTS,
) -> dict[str, Any]:
    """Return a deterministic schedule query result without live side effects."""
    target = _validate_temperature("target_temp_f", target_temp_f)
    outdoor = _validate_outdoor_temperature(outdoor_temp_f)
    deadline_dt = _parse_timestamp(deadline, "deadline")
    if as_of is not None:
        as_of_dt = _parse_timestamp(as_of, "as_of")
    else:
        as_of_dt = None
    if not math.isfinite(safety_margin_minutes) or safety_margin_minutes < 0:
        raise ValueError("safety_margin_minutes must be a finite non-negative number")
    if not math.isfinite(max_snapshot_age_hours) or max_snapshot_age_hours <= 0:
        raise ValueError("max_snapshot_age_hours must be a finite positive number")
    if deadline_dt <= (as_of_dt or datetime.min.replace(tzinfo=UTC)):
        if as_of_dt is not None:
            raise ValueError("deadline must be after as_of")
    snapshots, snapshot_quality = load_current_temperature_snapshots(log_path)
    if as_of_dt is None:
        as_of_dt = max(
            (snapshot.timestamp for snapshot in snapshots),
            default=datetime.now(UTC).replace(microsecond=0),
        )
    if deadline_dt <= as_of_dt:
        raise ValueError("deadline must be after as_of")
    horizon_hours = (deadline_dt - as_of_dt).total_seconds() / 3600.0
    if horizon_hours > MAX_HORIZON_HOURS:
        raise ValueError(f"deadline must be within {MAX_HORIZON_HOURS:g} hours of as_of")
    history_start, history_end = _resolve_history_range(as_of_dt, days, start, end)
    max_snapshot_age_s = max_snapshot_age_hours * 3600.0
    snapshot_quality["stale_for_as_of"] = sum(
        1
        for snapshot in snapshots
        if snapshot.timestamp <= as_of_dt
        and (as_of_dt - snapshot.timestamp).total_seconds() > max_snapshot_age_s
    )
    snapshot_quality["future_for_as_of"] = sum(
        1 for snapshot in snapshots if snapshot.timestamp > as_of_dt
    )

    explicit_current = current_temp_f is not None
    primary_current = (
        _validate_temperature("current_temp_f", current_temp_f) if explicit_current else None
    )
    secondary_current: dict[str, float | None] = {}
    explicit_secondary = {
        "floor_1": floor_1_current_temp_f,
        "floor_3": floor_3_current_temp_f,
    }
    for zone, value in explicit_secondary.items():
        secondary_current[zone] = (
            _validate_temperature(f"{zone}_current_temp_f", value) if value is not None else None
        )

    inferred_sources: dict[str, str] = {}
    if primary_current is None:
        snapshot = _latest_snapshot(snapshots, PRIMARY_ZONE, as_of_dt, max_snapshot_age_s)
        if snapshot is not None:
            primary_current = snapshot.temperature_f
            inferred_sources[PRIMARY_ZONE] = snapshot.timestamp.isoformat()
    for zone in SECONDARY_ZONES:
        if secondary_current[zone] is None:
            snapshot = _latest_snapshot(snapshots, zone, as_of_dt, max_snapshot_age_s)
            if snapshot is not None:
                secondary_current[zone] = snapshot.temperature_f
                inferred_sources[zone] = snapshot.timestamp.isoformat()

    if primary_current is not None:
        if target <= primary_current:
            raise ValueError("target_temp_f must be greater than current_temp_f")
        if target - primary_current > MAX_TARGET_DELTA_F:
            raise ValueError("target_temp_f minus current_temp_f exceeds the allowed delta")

    thermal_context = thermal_query.build_query_context(
        "Can floor 2 reach the requested target without a long call?",
        PRIMARY_ZONE,
        outdoor,
        target_temp_f=target,
        current_temp_f=primary_current,
        log_path=log_path,
        start=history_start,
        end=history_end,
        min_time_to_temp_observations=min_time_to_temp_observations,
        min_heat_loss_observations=min_heat_loss_observations,
        min_runtime_observations=min_runtime_observations,
        max_evidence_events=max_evidence_events,
    )
    prediction = thermal_context["model_outputs"]["time_to_temperature"].get("prediction")
    threshold = _threshold_settings(rules_config_path)
    heat_report = thermal_context["model_outputs"]["heat_loss"]
    status, recommendation, reasons, analysis = _build_recommendation(
        target_temp_f=target,
        current_temp_f=primary_current,
        deadline=deadline_dt,
        as_of=as_of_dt,
        prediction=prediction,
        heat_report=heat_report,
        secondary_current_temps=secondary_current,
        threshold=threshold,
        safety_margin_minutes=safety_margin_minutes,
    )
    if primary_current is None:
        reasons.insert(0, "no fresh floor-2 current temperature was available")
    if not reasons and status != "ready":
        reasons.append("schedule inputs are not answerable from the selected history")
    return {
        "schema": SCHEDULING_SCHEMA,
        "tool": TOOL_DEFINITION["name"],
        "read_only": True,
        "request": {
            "primary_zone": PRIMARY_ZONE,
            "target_temp_f": _round(target, 1),
            "current_temp_f": _round(primary_current, 1),
            "current_temp_source": (
                "explicit" if explicit_current else inferred_sources.get(PRIMARY_ZONE)
            ),
            "outdoor_temp_f": _round(outdoor, 1),
            "deadline": deadline_dt.isoformat(),
            "as_of": as_of_dt.isoformat(),
            "history_start": history_start.isoformat(),
            "history_end": history_end.isoformat(),
            "secondary_current_temp_f": {
                zone: _round(secondary_current[zone], 1) for zone in SECONDARY_ZONES
            },
            "secondary_current_temp_sources": {
                zone: (
                    "explicit"
                    if explicit_secondary[zone] is not None
                    else inferred_sources.get(zone)
                )
                for zone in SECONDARY_ZONES
            },
            "safety_margin_minutes": _round(safety_margin_minutes, 1),
        },
        "answerability": {
            "status": status,
            "can_recommend": status == "ready",
            "reasons": reasons,
        },
        "recommendation": recommendation,
        "analysis": analysis,
        "model_outputs": thermal_context["model_outputs"],
        "metadata": {
            "source": "derived consumer event log",
            "analysis_schemas": thermal_context["metadata"]["analysis_schemas"],
            "safety_rule": "rules.floor_2_long_call",
            "setpoint_rounding_f": SETPOINT_STEP_F,
        },
        "source_event_evidence": thermal_context["source_event_evidence"],
        "data_quality": {
            **thermal_context["data_quality"],
            "current_temperature": snapshot_quality,
        },
        "limitations": [
            "This is a historical planning estimate, not a guaranteed arrival time.",
            (
                "The configured floor-2 long-call threshold is a warning boundary, not proof "
                "of the physical furnace high-limit behavior."
            ),
            (
                "Secondary setpoint ceilings use the p75 observed cooling rate when available "
                "and subtract a 0.5°F planning margin."
            ),
            (
                "The result never calls Home Assistant, changes thermostat state, emits "
                "events, or writes consumer state."
            ),
            *reasons,
        ],
    }


def recommend_multi_zone_schedule(
    arguments: dict[str, Any],
    *,
    log_path: str | Path = DEFAULT_LOG,
    days: int = DEFAULT_DAYS,
    start: date | None = None,
    end: date | None = None,
    rules_config_path: str | os.PathLike[str] | None = None,
    safety_margin_minutes: float = DEFAULT_SAFETY_MARGIN_MINUTES,
    max_snapshot_age_hours: float = DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
    min_time_to_temp_observations: int = 5,
    min_heat_loss_observations: int = 3,
    min_runtime_observations: int = 3,
    max_evidence_events: int = thermal_query.DEFAULT_MAX_EVIDENCE_EVENTS,
) -> dict[str, Any]:
    """Dispatch one validated provider-neutral scheduling-tool argument object."""
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be a JSON object")
    properties = TOOL_DEFINITION["parameters"]["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ValueError(f"unknown tool argument(s): {', '.join(unknown)}")
    missing = [name for name in TOOL_DEFINITION["parameters"]["required"] if name not in arguments]
    if missing:
        raise ValueError(f"missing required tool argument(s): {', '.join(missing)}")
    return build_schedule_query(
        arguments["target_temp_f"],
        arguments["outdoor_temp_f"],
        arguments["deadline"],
        current_temp_f=arguments.get("current_temp_f"),
        floor_1_current_temp_f=arguments.get("floor_1_current_temp_f"),
        floor_3_current_temp_f=arguments.get("floor_3_current_temp_f"),
        as_of=arguments.get("as_of"),
        log_path=log_path,
        days=days,
        start=start,
        end=end,
        rules_config_path=rules_config_path,
        safety_margin_minutes=safety_margin_minutes,
        max_snapshot_age_hours=max_snapshot_age_hours,
        min_time_to_temp_observations=min_time_to_temp_observations,
        min_heat_loss_observations=min_heat_loss_observations,
        min_runtime_observations=min_runtime_observations,
        max_evidence_events=max_evidence_events,
    )


def render_text(result: Mapping[str, Any], file: TextIO | None = None) -> str:
    """Render a concise human-readable schedule result."""
    lines = [
        "# HomeOps multi-zone scheduling query",
        "",
        f"Status: `{result['answerability']['status']}`",
        f"Target: floor_2 to {result['request']['target_temp_f']:g}°F",
        f"Outdoor: {result['request']['outdoor_temp_f']:g}°F",
        f"Deadline: {result['request']['deadline']} (UTC)",
        "",
    ]
    recommendation = result.get("recommendation")
    if recommendation:
        lines.extend(
            [
                f"Start floor_2: `{recommendation['recommended_start']}` (UTC)",
                f"Predicted duration: {recommendation['predicted_duration_min']:g} minutes",
                (
                    "Safety reserve: "
                    f"{recommendation['safety']['reserved_margin_s'] / 60:g} minutes "
                    "below the "
                    f"{recommendation['safety']['configured_long_call_threshold_min']:g}-minute "
                    "threshold"
                ),
                "",
                "Secondary-zone ceilings:",
            ]
        )
        for zone in SECONDARY_ZONES:
            detail = recommendation["secondary_zones"][zone]
            lines.append(
                f"- {zone}: ≤ {detail['candidate_max_setpoint_f']:g}°F; "
                "no call during the floor-2 window"
            )
    else:
        lines.append("No safe schedule produced.")
        for reason in result["answerability"]["reasons"]:
            lines.append(f"- {reason}")
    rendered = "\n".join(lines)
    if file is not None:
        file.write(rendered)
    return rendered


def _finite_float(value: str) -> float:
    """Parse a finite command-line float."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a finite number: {value}") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("value must be finite")
    return parsed


def _positive_int(value: str) -> int:
    """Parse a positive command-line integer."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _parse_date(value: str) -> date:
    """Parse an ISO calendar date for argparse."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=_finite_float, required=True, help="Floor-2 target °F")
    parser.add_argument("--current", type=_finite_float, help="Explicit floor-2 current °F")
    parser.add_argument("--outdoor", type=_finite_float, required=True, help="Outdoor °F")
    parser.add_argument(
        "--by",
        required=True,
        help="ISO-8601 deadline, including date and optional timezone offset",
    )
    parser.add_argument("--as-of", help="ISO-8601 observation time; defaults to latest snapshot")
    parser.add_argument("--floor-1-current", type=_finite_float, help="Explicit floor-1 current °F")
    parser.add_argument("--floor-3-current", type=_finite_float, help="Explicit floor-3 current °F")
    parser.add_argument("--days", type=_positive_int, default=DEFAULT_DAYS)
    parser.add_argument("--start", type=_parse_date, help="Inclusive UTC history start date")
    parser.add_argument("--end", type=_parse_date, help="Inclusive UTC history end date")
    parser.add_argument("--log", default=None, help="Derived event JSONL path")
    parser.add_argument("--rules-config", default=None, help="Validated rules.yaml path")
    parser.add_argument(
        "--safety-margin-minutes",
        type=_finite_float,
        default=DEFAULT_SAFETY_MARGIN_MINUTES,
        help=(
            "Minutes reserved below the long-call threshold "
            f"(default: {DEFAULT_SAFETY_MARGIN_MINUTES:g})"
        ),
    )
    parser.add_argument(
        "--max-snapshot-age-hours",
        type=_finite_float,
        default=DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
        help=f"Maximum age of inferred temperatures (default: {DEFAULT_MAX_SNAPSHOT_AGE_HOURS:g})",
    )
    parser.add_argument("--out", help="Optional output path; defaults to stdout")
    parser.add_argument("--format", choices=("text", "json"), default="json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the scheduling query CLI."""
    args = _parse_args(argv)
    try:
        log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
        result = build_schedule_query(
            args.target,
            args.outdoor,
            args.by,
            current_temp_f=args.current,
            floor_1_current_temp_f=args.floor_1_current,
            floor_3_current_temp_f=args.floor_3_current,
            as_of=args.as_of,
            log_path=log_path,
            days=args.days,
            start=args.start,
            end=args.end,
            rules_config_path=args.rules_config,
            safety_margin_minutes=args.safety_margin_minutes,
            max_snapshot_age_hours=args.max_snapshot_age_hours,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output = (
        json.dumps(result, indent=2, sort_keys=True)
        if args.format == "json"
        else render_text(result)
    )
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print(f"Scheduling query written → {output_path} ({result['answerability']['status']})")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
