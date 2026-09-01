#!/usr/bin/env python3
"""Validate and quarantine normalized HomeOps thermal training rows.

The validator is deliberately separate from the event exporter.  It never
rewrites the input JSONL and never changes observer, consumer, or thermostat
behavior.  Rows that are structurally and semantically usable are emitted
unchanged; rejected rows retain the original object and stable reason codes in
the quarantine output.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

TRAINING_ROW_SCHEMA = "homeops.thermal.training_row.v1"
VALIDATION_REPORT_SCHEMA = "homeops.thermal.training_row_validation.v1"
QUARANTINE_SCHEMA = "homeops.thermal.training_row_quarantine.v1"

VALID_ZONES = frozenset({"floor_1", "floor_2", "floor_3"})
VALID_MODES = frozenset({"heat", "cool"})
OUTDOOR_TEMP_MAX_AGE_S = 10_800.0
DEFAULT_MAX_SESSION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MIN_ELIGIBLE_ROWS = 1
# The exporter serializes duration labels to three decimal places. Keep this
# tolerance separate from EPSILON: directional and feature semantics still
# use the stricter numeric comparison below.
DURATION_TIMESTAMP_TOLERANCE_S = 1e-3
EPSILON = 1e-6

# These bounds are intentionally broad.  They catch unit mistakes and broken
# sensor values without turning an unusual but possible house temperature into
# a rejection.
THERMAL_TEMP_MIN_F = -100.0
THERMAL_TEMP_MAX_F = 150.0

TIME_STATUS_VALUES = frozenset(
    {
        "eligible",
        "right_censored",
        "missing_start_boundary",
        "missing_measurement",
        "invalid_direction",
        "setpoint_changed",
        "already_at_target",
    }
)
RUNTIME_STATUS_VALUES = frozenset(
    {"eligible", "right_censored", "missing_start_boundary", "invalid_timestamp"}
)

HEATING_SOURCE_SCHEMAS = frozenset(
    {
        "homeops.consumer.zone_time_to_temp.v1",
        "homeops.consumer.zone_setpoint_miss.v1",
        "homeops.consumer.zone_overshoot.v1",
    }
)
COOLING_SOURCE_SCHEMAS = frozenset(
    {
        "homeops.consumer.thermostat_cooling_session_started.v1",
        "homeops.consumer.thermostat_cooling_session_ended.v1",
        "homeops.consumer.zone_time_to_cool.v1",
        "homeops.consumer.zone_cooling_setpoint_miss.v1",
        "homeops.consumer.zone_cooling_undershoot.v1",
    }
)
AGGREGATE_COOLING_SCHEMAS = frozenset(
    {
        "homeops.consumer.cooling_session_started.v1",
        "homeops.consumer.cooling_session_ended.v1",
    }
)

# These conditions are useful quality annotations but do not invalidate a row
# when another target remains eligible.  For example, a row with a valid
# time-to-setpoint label and a missing end boundary can still train that first
# target while being excluded from runtime training.
NON_FATAL_REASON_CODES = frozenset(
    {
        "missing_end_boundary",
        "missing_outdoor_temperature",
        "missing_cross_zone_snapshot",
    }
)

FORBIDDEN_FEATURE_KEYS = frozenset(
    {
        "active_end_ts",
        "target_crossing_ts",
        "time_to_setpoint_s",
        "zone_runtime_s",
        "end_temp_f",
        "observed_duration_s",
        "outcome_types",
        "duration_s",
        "final_temperature_f",
        "final_setpoint_f",
        "target_reached",
        "setpoint_miss",
        "overshoot",
        "undershoot",
    }
)


@dataclass(frozen=True)
class ValidationResult:
    """Validated rows, quarantine records, and a deterministic quality report."""

    valid_rows: list[dict[str, Any]]
    quarantined_rows: list[dict[str, Any]]
    report: dict[str, Any]


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp only when it carries timezone information."""

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
        return None
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    """Return a finite JSON number without coercing strings or booleans."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _add(reasons: set[str], *codes: str) -> None:
    reasons.update(codes)


def _has_key(container: Any, key: str) -> bool:
    return isinstance(container, dict) and key in container


def _validate_temperature(
    value: Any,
    reasons: set[str],
    *,
    missing_code: str,
    invalid_code: str,
    range_code: str,
) -> float | None:
    if value is None:
        reasons.add(missing_code)
        return None
    number = _number(value)
    if number is None:
        reasons.add(invalid_code)
        return None
    if not THERMAL_TEMP_MIN_F <= number <= THERMAL_TEMP_MAX_F:
        reasons.add(range_code)
    return number


def _validate_optional_number(
    container: dict[str, Any],
    key: str,
    reasons: set[str],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    invalid_code: str = "invalid_numeric_value",
    range_code: str = "numeric_value_out_of_range",
) -> float | None:
    if key not in container or container[key] is None:
        return None
    value = _number(container[key])
    if value is None:
        reasons.add(invalid_code)
        return None
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        reasons.add(range_code)
    return value


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        if any(key in FORBIDDEN_FEATURE_KEYS for key in value):
            return True
        return any(_contains_forbidden_key(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_key(child) for child in value)
    return False


def _validate_feature_provenance(
    provenance: dict[str, Any],
    prediction_ts: datetime | None,
    reasons: set[str],
) -> None:
    """Validate optional source timestamps that describe feature freshness."""

    if prediction_ts is None:
        return
    for key in (
        "climate_observed_at",
        "zone_call_observed_at",
        "outdoor_observed_at",
        "history_window_start_ts",
        "history_cutoff_ts",
    ):
        if key not in provenance or provenance[key] is None:
            continue
        timestamp = _parse_timestamp(provenance[key])
        if timestamp is None:
            reasons.add("invalid_timestamp")
        elif timestamp > prediction_ts:
            reasons.add("future_feature_timestamp")


def _validate_active_action(
    row: dict[str, Any],
    features: dict[str, Any],
    provenance: dict[str, Any],
    source_events: list[dict[str, Any]],
    mode: str | None,
    reasons: set[str],
) -> None:
    """Validate explicit action fields when present.

    The normalized exporter represents the active action with the row's
    ``mode``.  Older or hand-built rows may also carry ``active_action`` or
    ``hvac_action``; those fields are checked when present.  Source-schema
    evidence below supplies the required cooling-specific guard.
    """

    if not isinstance(mode, str) or mode not in VALID_MODES:
        return
    expected = {"heat", "heating"} if mode == "heat" else {"cool", "cooling"}
    values: list[Any] = []
    for container in (row, features, provenance):
        if not isinstance(container, dict):
            continue
        for key in ("active_action", "hvac_action"):
            if key in container:
                values.append(container[key])
    for reference in source_events:
        if not isinstance(reference, dict):
            continue
        for key in ("active_action", "hvac_action"):
            if key in reference:
                values.append(reference[key])

    if not values:
        return
    for value in values:
        if not isinstance(value, str) or not value.strip():
            reasons.add("missing_active_action")
            continue
        if value.strip().lower() not in expected:
            reasons.add("invalid_active_action")


def _validate_source_evidence(
    mode: str | None,
    provenance: dict[str, Any],
    reasons: set[str],
) -> set[str]:
    source_events = provenance.get("source_events")
    if not isinstance(source_events, list) or not source_events:
        _add(reasons, "missing_source_evidence", "missing_active_action")
        return set()

    schemas: set[str] = set()
    references: set[tuple[str, int]] = set()
    cooling_action_evidence = False
    for reference in source_events:
        if not isinstance(reference, dict):
            reasons.add("invalid_source_reference")
            continue
        source = reference.get("source")
        line = reference.get("line")
        schema = reference.get("schema")
        if (
            not isinstance(source, str)
            or source not in {"observer", "derived"}
            or not isinstance(line, int)
            or isinstance(line, bool)
        ):
            reasons.add("invalid_source_reference")
        elif line <= 0:
            reasons.add("invalid_source_reference")
        else:
            key = (source, line)
            if key in references:
                reasons.add("duplicate_source_reference")
            references.add(key)
        if not isinstance(schema, str) or not schema.strip():
            reasons.add("invalid_source_reference")
        else:
            schemas.add(schema)
        for key in ("active_action", "hvac_action"):
            value = reference.get(key)
            if isinstance(value, str) and value.strip().lower() in {"cool", "cooling"}:
                cooling_action_evidence = True
        if "timestamp" in reference and reference["timestamp"] is not None:
            if _parse_timestamp(reference["timestamp"]) is None:
                reasons.add("invalid_source_timestamp")

    if AGGREGATE_COOLING_SCHEMAS & schemas:
        reasons.add("aggregate_cooling_source")
    if mode == "heat" and COOLING_SOURCE_SCHEMAS & schemas:
        reasons.add("heating_cooling_source_mismatch")
    if mode == "cool" and HEATING_SOURCE_SCHEMAS & schemas:
        reasons.add("heating_cooling_source_mismatch")
    if mode == "cool" and not (COOLING_SOURCE_SCHEMAS & schemas or cooling_action_evidence):
        reasons.add("missing_cooling_source_evidence")
    return schemas


def _validate_features(
    row: dict[str, Any],
    mode: str | None,
    prediction_ts: datetime | None,
    reasons: set[str],
    stale_outdoor_seconds: float,
) -> tuple[dict[str, Any], float | None, float | None, float | None]:
    features = row.get("features")
    if not isinstance(features, dict):
        reasons.add("missing_features")
        return {}, None, None, None

    if _contains_forbidden_key(features):
        reasons.add("feature_target_leakage")

    start_temp = _validate_temperature(
        features.get("start_temp_f"),
        reasons,
        missing_code="missing_start_temperature",
        invalid_code="invalid_start_temperature",
        range_code="start_temperature_out_of_range",
    )
    start_setpoint = _validate_temperature(
        features.get("start_setpoint_f"),
        reasons,
        missing_code="missing_start_setpoint",
        invalid_code="invalid_start_setpoint",
        range_code="start_setpoint_out_of_range",
    )

    delta_value = features.get("setpoint_delta_f")
    delta = _number(delta_value)
    if delta_value is None:
        reasons.add("missing_setpoint_delta")
    elif delta is None:
        reasons.add("invalid_setpoint_delta")

    if (
        isinstance(mode, str)
        and mode in VALID_MODES
        and start_temp is not None
        and start_setpoint is not None
    ):
        expected_delta = (
            start_setpoint - start_temp if mode == "heat" else start_temp - start_setpoint
        )
        if expected_delta < -EPSILON:
            reasons.add("invalid_direction")
        if delta is not None and abs(delta - expected_delta) > EPSILON:
            reasons.add("setpoint_delta_mismatch")

    outdoor = _validate_optional_number(
        features,
        "outdoor_temp_f",
        reasons,
        invalid_code="invalid_outdoor_temperature",
        range_code="outdoor_temperature_out_of_range",
    )
    outdoor_age = _validate_optional_number(
        features,
        "outdoor_temp_age_s",
        reasons,
        minimum=0.0,
        invalid_code="invalid_outdoor_age",
        range_code="invalid_outdoor_age",
    )
    if outdoor is None and outdoor_age is not None:
        reasons.add("outdoor_age_without_temperature")
    if outdoor is not None and "outdoor_temp_age_s" not in features:
        reasons.add("missing_outdoor_age")
    if outdoor_age is not None and outdoor_age > stale_outdoor_seconds:
        reasons.add("stale_outdoor_input")
    if outdoor is None:
        reasons.add("missing_outdoor_temperature")

    other_zones = features.get("other_zones_calling")
    if other_zones is None:
        reasons.add("missing_cross_zone_snapshot")
    elif not isinstance(other_zones, list):
        reasons.add("invalid_cross_zone_snapshot")
    else:
        all_zone_ids = all(isinstance(zone, str) for zone in other_zones)
        if not all_zone_ids or any(zone not in VALID_ZONES for zone in other_zones):
            reasons.add("invalid_cross_zone_snapshot")
        if all_zone_ids and len(set(other_zones)) != len(other_zones):
            reasons.add("invalid_cross_zone_snapshot")
        if all_zone_ids and other_zones != sorted(other_zones):
            reasons.add("invalid_cross_zone_snapshot")
        if isinstance(row.get("zone"), str) and row.get("zone") in other_zones:
            reasons.add("invalid_cross_zone_snapshot")

    concurrent_count = features.get("concurrent_zone_count")
    if concurrent_count is not None:
        if (
            not isinstance(concurrent_count, int)
            or isinstance(concurrent_count, bool)
            or concurrent_count < 0
        ):
            reasons.add("invalid_concurrent_zone_count")
        elif isinstance(other_zones, list) and concurrent_count != len(other_zones):
            reasons.add("cross_zone_count_mismatch")
    elif isinstance(other_zones, list):
        reasons.add("missing_concurrent_zone_count")

    minute = features.get("start_minute_of_day_local")
    if minute is None:
        reasons.add("missing_local_time")
    elif not isinstance(minute, int) or isinstance(minute, bool) or not 0 <= minute <= 1439:
        reasons.add("invalid_local_time")

    history_complete = features.get("prior_zone_runtime_history_complete")
    if not isinstance(history_complete, bool):
        reasons.add("invalid_runtime_history_flag")
    prior_runtime = _validate_optional_number(
        features,
        "prior_zone_runtime_24h_s",
        reasons,
        minimum=0.0,
        invalid_code="invalid_prior_runtime",
        range_code="impossible_prior_runtime",
    )
    if history_complete is False and prior_runtime is not None:
        reasons.add("prior_runtime_without_complete_history")
    if history_complete is True and prior_runtime is None:
        reasons.add("missing_prior_runtime")

    _validate_optional_number(
        features,
        "indoor_humidity_pct",
        reasons,
        minimum=0.0,
        maximum=100.0,
        invalid_code="invalid_humidity",
        range_code="humidity_out_of_range",
    )
    _validate_optional_number(
        features,
        "weather_humidity_pct",
        reasons,
        minimum=0.0,
        maximum=100.0,
        invalid_code="invalid_weather_humidity",
        range_code="weather_humidity_out_of_range",
    )
    _validate_optional_number(
        features,
        "weather_cloud_cover_pct",
        reasons,
        minimum=0.0,
        maximum=100.0,
        invalid_code="invalid_cloud_cover",
        range_code="cloud_cover_out_of_range",
    )
    _validate_optional_number(
        features,
        "weather_wind_speed_mph",
        reasons,
        minimum=0.0,
        invalid_code="invalid_wind_speed",
        range_code="wind_speed_out_of_range",
    )

    if prediction_ts is not None:
        for key in ("outdoor_observed_at", "occupancy_observed_at", "weather_observed_at"):
            if key not in features or features[key] is None:
                continue
            timestamp = _parse_timestamp(features[key])
            if timestamp is None:
                reasons.add("invalid_timestamp")
            elif timestamp > prediction_ts:
                reasons.add("future_feature_timestamp")

    return features, start_temp, start_setpoint, delta


def _validate_labels(
    row: dict[str, Any],
    prediction_ts: datetime | None,
    active_end_ts: datetime | None,
    target_crossing_ts: datetime | None,
    start_temp: float | None,
    start_setpoint: float | None,
    delta: float | None,
    mode: str | None,
    max_session_seconds: float,
    reasons: set[str],
) -> tuple[str | None, str | None]:
    labels = row.get("labels")
    statuses = row.get("label_status")
    if not isinstance(labels, dict):
        reasons.add("missing_labels")
        labels = {}
    if not isinstance(statuses, dict):
        reasons.add("missing_label_status")
        statuses = {}

    time_status = statuses.get("time_to_setpoint")
    runtime_status = statuses.get("zone_runtime")
    if not isinstance(time_status, str) or time_status not in TIME_STATUS_VALUES:
        reasons.add("invalid_time_label_status")
        time_status = None
    if not isinstance(runtime_status, str) or runtime_status not in RUNTIME_STATUS_VALUES:
        reasons.add("invalid_runtime_label_status")
        runtime_status = None

    time_value = labels.get("time_to_setpoint_s")
    runtime_value = labels.get("zone_runtime_s")
    if time_status == "eligible":
        if time_value is None:
            reasons.add("missing_time_label")
        else:
            value = _number(time_value)
            if value is None:
                reasons.add("invalid_time_label")
            elif value <= 0 or value > max_session_seconds:
                reasons.add("impossible_duration")
            if target_crossing_ts is None or prediction_ts is None:
                reasons.add("time_label_without_target_boundary")
            elif (
                value is not None
                and abs(value - (target_crossing_ts - prediction_ts).total_seconds())
                > DURATION_TIMESTAMP_TOLERANCE_S
            ):
                reasons.add("time_label_timestamp_mismatch")
    elif time_value is not None:
        reasons.add("ineligible_time_label_present")

    if runtime_status == "eligible":
        if runtime_value is None:
            reasons.add("missing_runtime_label")
        else:
            value = _number(runtime_value)
            if value is None:
                reasons.add("invalid_runtime_label")
            elif value <= 0 or value > max_session_seconds:
                reasons.add("impossible_duration")
            if active_end_ts is None or prediction_ts is None:
                reasons.add("runtime_label_without_end_boundary")
            elif (
                value is not None
                and abs(value - (active_end_ts - prediction_ts).total_seconds())
                > DURATION_TIMESTAMP_TOLERANCE_S
            ):
                reasons.add("runtime_label_timestamp_mismatch")
    elif runtime_value is not None:
        reasons.add("ineligible_runtime_label_present")

    if time_status == "already_at_target" and delta is not None and abs(delta) > EPSILON:
        reasons.add("label_status_mismatch")
    if time_status == "already_at_target" and target_crossing_ts is not None:
        reasons.add("label_status_mismatch")
    if time_status == "eligible" and target_crossing_ts is None:
        reasons.add("label_status_mismatch")
    if target_crossing_ts is not None and time_status in {
        "right_censored",
        "missing_start_boundary",
        "missing_measurement",
        "invalid_direction",
        "already_at_target",
    }:
        reasons.add("label_status_mismatch")
    if runtime_status == "right_censored" and active_end_ts is not None:
        reasons.add("label_status_mismatch")
    if runtime_status == "eligible" and active_end_ts is None:
        reasons.add("runtime_label_without_end_boundary")

    if (
        isinstance(mode, str)
        and mode in VALID_MODES
        and start_temp is not None
        and start_setpoint is not None
    ):
        expected_delta = (
            start_setpoint - start_temp if mode == "heat" else start_temp - start_setpoint
        )
        if expected_delta <= EPSILON and time_status not in {
            "already_at_target",
            "invalid_direction",
        }:
            reasons.add("label_status_mismatch")

    return time_status, runtime_status


def validate_row(
    row: Any,
    *,
    max_session_seconds: float = DEFAULT_MAX_SESSION_SECONDS,
    stale_outdoor_seconds: float = OUTDOOR_TEMP_MAX_AGE_S,
) -> list[str]:
    """Return stable reason codes for one normalized training row.

    The returned list can contain non-fatal quality annotations.  Callers that
    need the training partition should use :func:`validate_rows`, which
    quarantines only rows with fatal reasons and preserves partial-label rows
    when at least one target is eligible.
    """

    reasons: set[str] = set()
    if not isinstance(row, dict):
        return ["non_object_row"]
    if (
        not math.isfinite(max_session_seconds)
        or not math.isfinite(stale_outdoor_seconds)
        or max_session_seconds <= 0
        or stale_outdoor_seconds < 0
    ):
        raise ValueError("validation thresholds must be finite and in range")

    if row.get("schema") != TRAINING_ROW_SCHEMA:
        reasons.add("invalid_schema")

    row_id = row.get("row_id")
    if not isinstance(row_id, str) or not row_id.strip():
        reasons.add("missing_row_id")

    zone = row.get("zone")
    if zone is None:
        reasons.add("missing_zone")
    elif not isinstance(zone, str) or zone not in VALID_ZONES:
        reasons.add("unknown_zone")

    mode = row.get("mode")
    if mode is None:
        reasons.add("missing_mode")
    elif not isinstance(mode, str) or mode not in VALID_MODES:
        reasons.add("invalid_mode")

    prediction_text = row.get("prediction_ts")
    start_text = row.get("active_start_ts")
    prediction_ts = _parse_timestamp(prediction_text)
    active_start_ts = _parse_timestamp(start_text)
    if prediction_text is None or start_text is None:
        reasons.add("missing_start_boundary")
    elif prediction_ts is None or active_start_ts is None:
        reasons.add("invalid_timestamp")
    elif prediction_ts != active_start_ts:
        reasons.add("prediction_boundary_mismatch")

    active_end_text = row.get("active_end_ts")
    active_end_ts = _parse_timestamp(active_end_text)
    if active_end_text is None:
        reasons.add("missing_end_boundary")
    elif active_end_ts is None:
        reasons.add("invalid_timestamp")
    elif active_start_ts is not None:
        duration = (active_end_ts - active_start_ts).total_seconds()
        if duration <= 0 or duration > max_session_seconds:
            reasons.add("impossible_duration")

    target_text = row.get("target_crossing_ts")
    target_crossing_ts = _parse_timestamp(target_text)
    if target_text is not None and target_crossing_ts is None:
        reasons.add("invalid_timestamp")
    if target_crossing_ts is not None and active_start_ts is not None:
        target_duration = (target_crossing_ts - active_start_ts).total_seconds()
        if target_duration <= 0:
            reasons.add("impossible_timestamp_order")
        elif target_duration > max_session_seconds:
            reasons.add("impossible_duration")
        if active_end_ts is not None and target_crossing_ts > active_end_ts:
            reasons.add("target_after_session_end")

    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        reasons.add("missing_provenance")
        provenance = {}
    if provenance.get("start_boundary") != "observed":
        reasons.add("missing_start_boundary")
    _validate_feature_provenance(provenance, prediction_ts, reasons)
    source_events = provenance.get("source_events")
    source_events_for_action = source_events if isinstance(source_events, list) else []
    _validate_active_action(
        row, row.get("features") or {}, provenance, source_events_for_action, mode, reasons
    )
    _validate_source_evidence(mode, provenance, reasons)

    features, start_temp, start_setpoint, delta = _validate_features(
        row,
        mode,
        prediction_ts,
        reasons,
        stale_outdoor_seconds,
    )

    # The public validator accepts the default freshness contract, while the
    # CLI/API threshold can be tightened for a particular source snapshot.
    time_status, runtime_status = _validate_labels(
        row,
        prediction_ts,
        active_end_ts,
        target_crossing_ts,
        start_temp,
        start_setpoint,
        delta,
        mode,
        max_session_seconds,
        reasons,
    )

    observations = row.get("observations")
    if not isinstance(observations, dict):
        reasons.add("missing_observations")
    else:
        observed_duration = observations.get("observed_duration_s")
        if observed_duration is not None:
            observed_number = _number(observed_duration)
            if observed_number is None:
                reasons.add("invalid_observed_duration")
            elif observed_number <= 0 or observed_number > max_session_seconds:
                reasons.add("impossible_duration")
        outcome_types = observations.get("outcome_types")
        if not isinstance(outcome_types, list) or any(
            not isinstance(value, str) for value in outcome_types
        ):
            reasons.add("invalid_outcome_types")

    quality_flags = row.get("quality_flags")
    if not isinstance(quality_flags, list) or any(
        not isinstance(value, str) for value in quality_flags
    ):
        reasons.add("invalid_quality_flags")

    experiment = provenance.get("experiment")
    if experiment is not None:
        if not isinstance(experiment, dict):
            reasons.add("invalid_experiment_metadata")
        else:
            for key in ("experiment_id", "experiment_name", "operation_type", "test_id"):
                if key in experiment and not isinstance(experiment[key], str):
                    reasons.add("invalid_experiment_metadata")
            if "intervention" in experiment and not isinstance(
                experiment["intervention"], (bool, str, dict, list)
            ):
                reasons.add("invalid_experiment_metadata")

    if time_status != "eligible" and runtime_status != "eligible":
        reasons.add("no_eligible_label")

    return sorted(reasons)


def _row_interval(row: dict[str, Any]) -> tuple[str, str, datetime, datetime | None] | None:
    zone = row.get("zone")
    mode = row.get("mode")
    start = _parse_timestamp(row.get("active_start_ts"))
    if (
        not isinstance(zone, str)
        or zone not in VALID_ZONES
        or not isinstance(mode, str)
        or mode not in VALID_MODES
        or start is None
    ):
        return None
    end = _parse_timestamp(row.get("active_end_ts"))
    if end is not None and end <= start:
        return None
    return zone, mode, start, end


def _slice_key(row: Any) -> tuple[str, str] | None:
    if not isinstance(row, dict):
        return None
    zone = row.get("zone")
    mode = row.get("mode")
    if (
        not isinstance(zone, str)
        or zone not in VALID_ZONES
        or not isinstance(mode, str)
        or mode not in VALID_MODES
    ):
        return None
    return zone, mode


def _add_overlap_reasons(
    records: list[tuple[int, Any]],
    reasons_by_index: dict[int, set[str]],
) -> None:
    grouped: defaultdict[str, list[tuple[datetime, datetime | None, int]]] = defaultdict(list)
    for index, row in records:
        if not isinstance(row, dict):
            continue
        interval = _row_interval(row)
        if interval is None:
            continue
        zone, _, start, end = interval
        grouped[zone].append((start, end, index))

    for intervals in grouped.values():
        active: list[tuple[datetime | None, int]] = []
        for start, end, index in sorted(intervals, key=lambda item: (item[0], item[2])):
            active = [
                (active_end, active_index)
                for active_end, active_index in active
                if active_end is None or active_end > start
            ]
            for _, active_index in active:
                reasons_by_index[index].add("overlapping_session")
                reasons_by_index[active_index].add("overlapping_session")
            active.append((end, index))


def _quarantine_record(
    row: Any,
    source_line: int,
    reason_codes: Iterable[str],
    *,
    raw_line: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": QUARANTINE_SCHEMA,
        "source_line": source_line,
        "row_id": row.get("row_id") if isinstance(row, dict) else None,
        "reason_codes": sorted(set(reason_codes)),
        "row": row,
    }
    if raw_line is not None:
        record["raw_line"] = raw_line.rstrip("\n")
    return record


def _build_report(
    records: list[tuple[int, Any]],
    valid_rows: list[dict[str, Any]],
    quarantined_rows: list[dict[str, Any]],
    reasons_by_index: dict[int, set[str]],
    *,
    malformed_lines: int,
    minimum_eligible_rows: int,
    max_session_seconds: float,
    stale_outdoor_seconds: float,
) -> dict[str, Any]:
    reason_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()
    for index, reasons in reasons_by_index.items():
        reason_counts.update(reasons)
        if not any(reason not in NON_FATAL_REASON_CODES for reason in reasons):
            warning_counts.update(reasons)
    parsed_lines = {index for index, _ in records}
    for record in quarantined_rows:
        # Parsed rows are already represented in reasons_by_index.  Add only
        # malformed/blank-line records, which have no parsed-row entry.
        if record.get("source_line") not in parsed_lines:
            reason_counts.update(record["reason_codes"])

    rows_by_slice: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    valid_by_slice: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    quarantined_by_slice: Counter[tuple[str, str]] = Counter()
    reason_counts_by_slice: defaultdict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    fatal_reason_counts_by_slice: defaultdict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for _, row in records:
        key = _slice_key(row)
        if key is not None:
            rows_by_slice[key].append(row)
    for index, row in records:
        key = _slice_key(row)
        if key is None:
            continue
        reasons = reasons_by_index.get(index, set())
        reason_counts_by_slice[key].update(reasons)
        fatal_reason_counts_by_slice[key].update(
            reason for reason in reasons if reason not in NON_FATAL_REASON_CODES
        )
    for row in valid_rows:
        valid_by_slice[(row["zone"], row["mode"])].append(row)
    for record in quarantined_rows:
        row = record.get("row")
        key = _slice_key(row)
        if key is not None:
            quarantined_by_slice[key] += 1

    by_zone_mode: dict[str, Any] = {}
    any_insufficient = False
    for zone in sorted(VALID_ZONES):
        for mode in sorted(VALID_MODES):
            key = (zone, mode)
            rows = valid_by_slice[key]
            eligible_time = sum(
                1
                for row in rows
                if row.get("label_status", {}).get("time_to_setpoint") == "eligible"
                and row.get("labels", {}).get("time_to_setpoint_s") is not None
            )
            eligible_runtime = sum(
                1
                for row in rows
                if row.get("label_status", {}).get("zone_runtime") == "eligible"
                and row.get("labels", {}).get("zone_runtime_s") is not None
            )
            time_status = "ok" if eligible_time >= minimum_eligible_rows else "insufficient_data"
            runtime_status = (
                "ok" if eligible_runtime >= minimum_eligible_rows else "insufficient_data"
            )
            if "insufficient_data" in {time_status, runtime_status}:
                any_insufficient = True
            by_zone_mode[f"{zone}:{mode}"] = {
                "input_rows": len(rows_by_slice[key]),
                "valid_rows": len(rows),
                "quarantined_rows": quarantined_by_slice[key],
                "reason_counts": dict(sorted(reason_counts_by_slice[key].items())),
                "fatal_reason_counts": dict(sorted(fatal_reason_counts_by_slice[key].items())),
                "eligible_time_to_setpoint": eligible_time,
                "eligible_zone_runtime": eligible_runtime,
                "time_to_setpoint_status": time_status,
                "zone_runtime_status": runtime_status,
                "status": "ok" if time_status == runtime_status == "ok" else "insufficient_data",
            }

    return {
        "schema": VALIDATION_REPORT_SCHEMA,
        "status": "ok" if not quarantined_rows else "quarantine_present",
        "coverage_status": "insufficient_data" if any_insufficient else "ok",
        "input_lines": len(records) + malformed_lines,
        "parsed_rows": len(records),
        "valid_rows": len(valid_rows),
        "quarantined_rows": len(quarantined_rows),
        "malformed_lines": malformed_lines,
        "minimum_eligible_rows": minimum_eligible_rows,
        "max_session_seconds": max_session_seconds,
        "stale_outdoor_seconds": stale_outdoor_seconds,
        "reason_counts": dict(sorted(reason_counts.items())),
        "fatal_reason_counts": dict(
            sorted(
                (reason, count)
                for reason, count in reason_counts.items()
                if reason not in NON_FATAL_REASON_CODES
            )
        ),
        "valid_row_warning_counts": dict(sorted(warning_counts.items())),
        "by_zone_mode": by_zone_mode,
    }


def _validate_records(
    records: list[tuple[int, Any]],
    *,
    malformed_quarantines: list[dict[str, Any]] | None = None,
    minimum_eligible_rows: int = DEFAULT_MIN_ELIGIBLE_ROWS,
    max_session_seconds: float = DEFAULT_MAX_SESSION_SECONDS,
    stale_outdoor_seconds: float = OUTDOOR_TEMP_MAX_AGE_S,
) -> ValidationResult:
    if minimum_eligible_rows < 1:
        raise ValueError("minimum_eligible_rows must be at least 1")
    if (
        not math.isfinite(max_session_seconds)
        or not math.isfinite(stale_outdoor_seconds)
        or max_session_seconds <= 0
        or stale_outdoor_seconds < 0
    ):
        raise ValueError("validation thresholds are invalid")

    reasons_by_index: dict[int, set[str]] = {}
    for index, row in records:
        reasons_by_index[index] = set(
            validate_row(
                row,
                max_session_seconds=max_session_seconds,
                stale_outdoor_seconds=stale_outdoor_seconds,
            )
        )

    row_id_indices: defaultdict[str, list[int]] = defaultdict(list)
    for index, row in records:
        if isinstance(row, dict) and isinstance(row.get("row_id"), str) and row["row_id"].strip():
            row_id_indices[row["row_id"]].append(index)
    for indices in row_id_indices.values():
        if len(indices) > 1:
            for index in indices:
                reasons_by_index[index].add("duplicate_row_id")

    _add_overlap_reasons(records, reasons_by_index)

    valid_rows: list[dict[str, Any]] = []
    quarantined_rows = list(malformed_quarantines or [])
    for index, row in records:
        reasons = reasons_by_index[index]
        fatal_reasons = reasons - NON_FATAL_REASON_CODES
        if isinstance(row, dict) and not fatal_reasons:
            valid_rows.append(row)
        else:
            quarantined_rows.append(_quarantine_record(row, index, reasons))

    report = _build_report(
        records,
        valid_rows,
        quarantined_rows,
        reasons_by_index,
        malformed_lines=len(malformed_quarantines or []),
        minimum_eligible_rows=minimum_eligible_rows,
        max_session_seconds=max_session_seconds,
        stale_outdoor_seconds=stale_outdoor_seconds,
    )
    return ValidationResult(valid_rows, quarantined_rows, report)


def validate_rows(
    rows: Iterable[dict[str, Any]],
    *,
    minimum_eligible_rows: int = DEFAULT_MIN_ELIGIBLE_ROWS,
    max_session_seconds: float = DEFAULT_MAX_SESSION_SECONDS,
    stale_outdoor_seconds: float = OUTDOOR_TEMP_MAX_AGE_S,
) -> ValidationResult:
    """Validate already-decoded rows and return unchanged valid rows."""

    records = list(enumerate(rows, start=1))
    return _validate_records(
        records,
        minimum_eligible_rows=minimum_eligible_rows,
        max_session_seconds=max_session_seconds,
        stale_outdoor_seconds=stale_outdoor_seconds,
    )


def validate_jsonl(
    input_stream: TextIO,
    *,
    minimum_eligible_rows: int = DEFAULT_MIN_ELIGIBLE_ROWS,
    max_session_seconds: float = DEFAULT_MAX_SESSION_SECONDS,
    stale_outdoor_seconds: float = OUTDOOR_TEMP_MAX_AGE_S,
) -> ValidationResult:
    """Validate JSONL, quarantining malformed and non-object lines as well."""

    records: list[tuple[int, Any]] = []
    malformed: list[dict[str, Any]] = []
    for line_number, line in enumerate(input_stream, start=1):
        if not line.strip():
            malformed.append(_quarantine_record(None, line_number, ["blank_line"], raw_line=line))
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(
                _quarantine_record(None, line_number, ["malformed_json"], raw_line=line)
            )
            continue
        records.append((line_number, value))
    return _validate_records(
        records,
        malformed_quarantines=malformed,
        minimum_eligible_rows=minimum_eligible_rows,
        max_session_seconds=max_session_seconds,
        stale_outdoor_seconds=stale_outdoor_seconds,
    )


def _write_jsonl(rows: Iterable[Any], output: TextIO) -> None:
    for row in rows:
        output.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
        output.write("\n")


def _write_report(report: dict[str, Any], path: str) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if path == "-":
        sys.stdout.write(rendered)
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")


def _write_rows(rows: Iterable[Any], path: str) -> None:
    if path == "-":
        _write_jsonl(rows, sys.stdout)
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        _write_jsonl(rows, output)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate normalized HomeOps thermal rows and quarantine bad data."
    )
    parser.add_argument("--input", required=True, help="Input JSONL path, or '-' for stdin.")
    parser.add_argument(
        "--valid-out",
        required=True,
        help="Validated JSONL output path, or '-' for stdout.",
    )
    parser.add_argument(
        "--quarantine-out",
        required=True,
        help="Quarantine JSONL output path, or '-' for stdout.",
    )
    parser.add_argument(
        "--report-out",
        required=True,
        help="Quality report JSON path, or '-' for stdout.",
    )
    parser.add_argument(
        "--minimum-eligible-rows",
        type=int,
        default=DEFAULT_MIN_ELIGIBLE_ROWS,
        help="Minimum eligible labels required for an ok floor/mode coverage status.",
    )
    parser.add_argument(
        "--max-session-seconds",
        type=float,
        default=DEFAULT_MAX_SESSION_SECONDS,
        help="Maximum physically plausible session/label duration.",
    )
    parser.add_argument(
        "--stale-outdoor-seconds",
        type=float,
        default=OUTDOOR_TEMP_MAX_AGE_S,
        help="Maximum accepted age for an outdoor-temperature feature.",
    )
    parser.add_argument(
        "--fail-on-quarantine",
        action="store_true",
        help="Return exit code 1 when any row is quarantined.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.valid_out == args.quarantine_out and args.valid_out == "-":
        print("validation failed: valid and quarantine output cannot share stdout", file=sys.stderr)
        return 2
    if args.report_out == "-" and (args.valid_out == "-" or args.quarantine_out == "-"):
        print(
            "validation failed: report stdout cannot share stdout with JSONL output",
            file=sys.stderr,
        )
        return 2
    if args.input != "-" and args.input in {args.valid_out, args.quarantine_out, args.report_out}:
        print("validation failed: output paths must not overwrite the input", file=sys.stderr)
        return 2

    try:
        if args.minimum_eligible_rows < 1:
            raise ValueError("minimum-eligible-rows must be at least 1")
        if (
            not math.isfinite(args.max_session_seconds)
            or not math.isfinite(args.stale_outdoor_seconds)
            or args.max_session_seconds <= 0
            or args.stale_outdoor_seconds < 0
        ):
            raise ValueError("validation thresholds are invalid")
        if args.input == "-":
            result = validate_jsonl(
                sys.stdin,
                minimum_eligible_rows=args.minimum_eligible_rows,
                max_session_seconds=args.max_session_seconds,
                stale_outdoor_seconds=args.stale_outdoor_seconds,
            )
        else:
            with Path(args.input).open("r", encoding="utf-8") as input_stream:
                result = validate_jsonl(
                    input_stream,
                    minimum_eligible_rows=args.minimum_eligible_rows,
                    max_session_seconds=args.max_session_seconds,
                    stale_outdoor_seconds=args.stale_outdoor_seconds,
                )
        _write_rows(result.valid_rows, args.valid_out)
        _write_rows(result.quarantined_rows, args.quarantine_out)
        _write_report(result.report, args.report_out)
    except (OSError, ValueError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "valid_rows": result.report["valid_rows"],
                "quarantined_rows": result.report["quarantined_rows"],
                "coverage_status": result.report["coverage_status"],
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 1 if args.fail_on_quarantine and result.quarantined_rows else 0


if __name__ == "__main__":
    raise SystemExit(main())
