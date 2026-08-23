#!/usr/bin/env python3
"""Build deterministic per-zone time-to-temperature predictions.

The command replays completed ``zone_time_to_temp.v1`` events from the derived
event log without starting the consumer or writing production state.  The
consumer emits this completed event alongside
``thermostat_setpoint_reached.v1``; only the completed event contains the
duration and setpoint delta needed for a time-to-temperature model.

For each zone, the report fits an ordinary-least-squares line to observed
seconds per degree as a function of outdoor temperature.  A prediction for a
requested positive setpoint delta multiplies the modeled seconds per degree by
that delta.  This keeps the model small and interpretable while using both
requested inputs: ``outdoor_temp_f`` selects the weather condition and
``setpoint_delta_f`` scales the expected work.  Outdoor-temperature buckets
are retained in the report as an auditable lookup view; they do not hide the
continuous model used for a prediction.

This is a planning estimate, not a thermostat controller or an equipment
diagnosis.  Sparse zones, missing telemetry, non-positive measurements, and
out-of-range predictions remain explicit in the output.

Usage (last 30 UTC days):
    python3 scripts/time_to_temp.py --log state/consumer/events.jsonl

Usage with a prediction query and an explicit historical range:
    python3 scripts/time_to_temp.py \
        --zone floor_2 --outdoor 30 --delta 3 \
        --start 2026-03-20 --end 2026-05-31 \
        --log state/consumer/events.jsonl \
        --out reports/time-to-temp.json --format json

Revision history:
  2026-08-23  Added a read-only per-zone time-to-temperature model using
              completed heating-cycle events, outdoor-temperature covariates,
              explicit sparse-data guards, and deterministic CLI/report output.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

SCHEMA = "homeops.consumer.zone_time_to_temp.v1"
SETPOINT_REACHED_SCHEMA = "homeops.consumer.thermostat_setpoint_reached.v1"
DEFAULT_LOG = "state/consumer/events.jsonl"
DEFAULT_DAYS = 30
DEFAULT_MIN_OBSERVATIONS = 5
DEFAULT_BUCKET_WIDTH_F = 10.0
KNOWN_ZONES = ("floor_1", "floor_2", "floor_3")


@dataclass(frozen=True)
class TimeToTempObservation:
    """One completed heating cycle with the model's required measurements."""

    timestamp: datetime
    zone: str
    outdoor_temp_f: float
    setpoint_delta_f: float
    duration_s: float
    seconds_per_degree_s: float
    outdoor_bucket_lower_f: float
    outdoor_bucket_upper_f: float


@dataclass(frozen=True)
class LinearModel:
    """Seconds-per-degree least-squares model for one zone."""

    intercept_s_per_degree: float
    slope_s_per_degree_per_f: float
    r_squared: float | None

    def predict_seconds_per_degree(self, outdoor_temp_f: float) -> float:
        """Return the modeled furnace seconds required for one degree."""
        return self.intercept_s_per_degree + (self.slope_s_per_degree_per_f * outdoor_temp_f)

    def predict_duration(self, outdoor_temp_f: float, setpoint_delta_f: float) -> float:
        """Return modeled seconds for a positive requested temperature delta."""
        return self.predict_seconds_per_degree(outdoor_temp_f) * setpoint_delta_f


def _finite_number(value: Any) -> float | None:
    """Return a finite JSON number, excluding booleans and strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize naive values to UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_timestamp(event: dict[str, Any], data: dict[str, Any]) -> datetime | None:
    """Use the event timestamp, with data timestamps supported for fixtures."""
    for value in (event.get("ts"), data.get("ts"), data.get("timestamp")):
        timestamp = _parse_timestamp(value)
        if timestamp is not None:
            return timestamp
    return None


def _event_key(event: dict[str, Any]) -> str:
    """Return a stable identity for exact duplicate JSONL records."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def _zone_from_data(data: dict[str, Any]) -> str | None:
    """Resolve a canonical zone from the explicit zone or climate entity."""
    for key in ("zone", "floor"):
        zone = data.get(key)
        if isinstance(zone, str) and zone:
            return zone
    entity_id = data.get("entity_id")
    if isinstance(entity_id, str):
        for zone in KNOWN_ZONES:
            if entity_id == f"climate.{zone}_thermostat":
                return zone
    return None


def _bucket_bounds(temperature_f: float, width_f: float) -> tuple[float, float]:
    """Return the half-open outdoor-temperature bucket containing a reading."""
    lower = math.floor(temperature_f / width_f) * width_f
    upper = lower + width_f
    return round(lower, 6), round(upper, 6)


def _format_number(value: float) -> str:
    """Format integral bucket bounds without unnecessary decimal places."""
    return f"{value:g}"


def _bucket_label(lower: float, upper: float) -> str:
    """Render a stable half-open bucket label."""
    return f"[{_format_number(lower)}, {_format_number(upper)})°F"


def load_observations(
    log_path: str | Path,
    start: date,
    end: date,
    *,
    bucket_width_f: float = DEFAULT_BUCKET_WIDTH_F,
) -> tuple[list[TimeToTempObservation], dict[str, int]]:
    """Load valid completed heating observations and data-quality counters.

    The range is applied to the event's UTC date.  Exact duplicate records are
    ignored, while malformed, incomplete, and physically invalid records are
    counted instead of becoming silently misleading model inputs.
    """
    if start > end:
        raise ValueError("start date must be on or before end date")
    if not math.isfinite(bucket_width_f) or bucket_width_f <= 0:
        raise ValueError("bucket_width_f must be greater than 0")

    quality = {
        "lines_seen": 0,
        "relevant_events": 0,
        "duplicate_events": 0,
        "malformed_json": 0,
        "non_object_events": 0,
        "events_missing_data": 0,
        "events_missing_timestamp": 0,
        "events_out_of_range": 0,
        "events_unknown_zone": 0,
        "events_missing_measurement": 0,
        "events_with_invalid_measurement": 0,
        "eligible_observations": 0,
    }
    observations: list[TimeToTempObservation] = []
    seen: set[str] = set()

    try:
        with open(log_path, encoding="utf-8") as events_file:
            for line in events_file:
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
                if event.get("schema") != SCHEMA:
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
                if timestamp.date() < start or timestamp.date() > end:
                    quality["events_out_of_range"] += 1
                    continue

                zone = _zone_from_data(data)
                if zone is None:
                    quality["events_unknown_zone"] += 1
                    continue
                duration_s = _finite_number(data.get("duration_s"))
                setpoint_delta_f = _finite_number(data.get("setpoint_delta"))
                outdoor_temp_f = _finite_number(data.get("outdoor_temp_f"))
                if duration_s is None or setpoint_delta_f is None or outdoor_temp_f is None:
                    quality["events_missing_measurement"] += 1
                    continue
                if duration_s <= 0 or setpoint_delta_f <= 0:
                    quality["events_with_invalid_measurement"] += 1
                    continue
                seconds_per_degree_s = duration_s / setpoint_delta_f
                if not math.isfinite(seconds_per_degree_s) or seconds_per_degree_s <= 0:
                    quality["events_with_invalid_measurement"] += 1
                    continue
                lower, upper = _bucket_bounds(outdoor_temp_f, bucket_width_f)
                observations.append(
                    TimeToTempObservation(
                        timestamp=timestamp,
                        zone=zone,
                        outdoor_temp_f=outdoor_temp_f,
                        setpoint_delta_f=setpoint_delta_f,
                        duration_s=duration_s,
                        seconds_per_degree_s=seconds_per_degree_s,
                        outdoor_bucket_lower_f=lower,
                        outdoor_bucket_upper_f=upper,
                    )
                )
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"log file not found: {log_path}") from exc
    except OSError as exc:
        raise OSError(f"error reading log {log_path}: {exc}") from exc

    observations.sort(key=lambda item: (item.timestamp, item.zone))
    quality["eligible_observations"] = len(observations)
    return observations, quality


def fit_linear_model(rows: Iterable[TimeToTempObservation]) -> LinearModel | None:
    """Fit seconds per degree against outdoor temperature, or return ``None``."""
    points = list(rows)
    if len(points) < 2:
        return None
    x_values = [row.outdoor_temp_f for row in points]
    y_values = [row.seconds_per_degree_s for row in points]
    mean_x = statistics.fmean(x_values)
    mean_y = statistics.fmean(y_values)
    denominator = sum((x - mean_x) ** 2 for x in x_values)
    if denominator <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_values, y_values)) / denominator
    intercept = mean_y - slope * mean_x
    predicted = [intercept + slope * x for x in x_values]
    residual_sum_squares = sum((y - expected) ** 2 for y, expected in zip(y_values, predicted))
    total_sum_squares = sum((y - mean_y) ** 2 for y in y_values)
    r_squared = 1.0 - residual_sum_squares / total_sum_squares if total_sum_squares > 0 else None
    return LinearModel(intercept, slope, r_squared)


def _round(value: float | None, digits: int = 2) -> float | None:
    """Round a report value while preserving missingness."""
    return round(value, digits) if value is not None else None


def _percentile(values: list[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile without dependencies."""
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _model_dict(
    model: LinearModel | None,
    rows: list[TimeToTempObservation],
) -> dict[str, Any] | None:
    """Serialize a model and the range in which it was trained."""
    if model is None or not rows:
        return None
    return {
        "intercept_s_per_degree": _round(model.intercept_s_per_degree),
        "slope_s_per_degree_per_f": _round(model.slope_s_per_degree_per_f),
        "r_squared": _round(model.r_squared, 4),
        "training_outdoor_temp_range_f": {
            "min": _round(min(row.outdoor_temp_f for row in rows), 1),
            "max": _round(max(row.outdoor_temp_f for row in rows), 1),
        },
        "training_setpoint_delta_range_f": {
            "min": _round(min(row.setpoint_delta_f for row in rows), 2),
            "max": _round(max(row.setpoint_delta_f for row in rows), 2),
        },
        "equation": (
            "predicted_duration_s = (intercept_s_per_degree + "
            "slope_s_per_degree_per_f * outdoor_temp_f) * setpoint_delta_f"
        ),
    }


def _observation_dict(row: TimeToTempObservation) -> dict[str, Any]:
    """Serialize one observation for the JSON report."""
    return {
        "timestamp": row.timestamp.isoformat(),
        "zone": row.zone,
        "outdoor_temp_f": _round(row.outdoor_temp_f, 1),
        "setpoint_delta_f": _round(row.setpoint_delta_f, 2),
        "duration_s": _round(row.duration_s, 1),
        "seconds_per_degree_s": _round(row.seconds_per_degree_s, 2),
        "outdoor_temp_bucket": _bucket_label(
            row.outdoor_bucket_lower_f,
            row.outdoor_bucket_upper_f,
        ),
    }


def _bucket_report(
    rows: list[TimeToTempObservation],
    min_observations: int,
) -> dict[str, Any]:
    """Aggregate observations for one zone and outdoor-temperature bucket."""
    first = rows[0]
    durations = [row.duration_s for row in rows]
    deltas = [row.setpoint_delta_f for row in rows]
    rates = [row.seconds_per_degree_s for row in rows]
    return {
        "outdoor_temp_bucket": _bucket_label(
            first.outdoor_bucket_lower_f,
            first.outdoor_bucket_upper_f,
        ),
        "lower_bound_f": first.outdoor_bucket_lower_f,
        "upper_bound_f": first.outdoor_bucket_upper_f,
        "observation_count": len(rows),
        "min_observations": min_observations,
        "median_duration_s": _round(statistics.median(durations), 1),
        "median_setpoint_delta_f": _round(statistics.median(deltas), 2),
        "median_seconds_per_degree_s": _round(statistics.median(rates), 2),
        "p25_seconds_per_degree_s": _round(_percentile(rates, 0.25), 2),
        "p75_seconds_per_degree_s": _round(_percentile(rates, 0.75), 2),
        "status": "ok" if len(rows) >= min_observations else "insufficient_data",
        "observations": [_observation_dict(row) for row in rows],
    }


def _prediction_report(
    zone: str,
    outdoor_temp_f: float,
    setpoint_delta_f: float,
    rows: list[TimeToTempObservation],
    model: LinearModel | None,
    min_observations: int,
    bucket_width_f: float,
) -> dict[str, Any]:
    """Build an explicit prediction result, including sparse-data guards."""
    lower, upper = _bucket_bounds(outdoor_temp_f, bucket_width_f)
    result: dict[str, Any] = {
        "zone": zone,
        "outdoor_temp_f": _round(outdoor_temp_f, 1),
        "outdoor_temp_bucket": _bucket_label(lower, upper),
        "setpoint_delta_f": _round(setpoint_delta_f, 2),
        "min_observations": min_observations,
        "observation_count": len(rows),
        "predicted_seconds_per_degree_s": None,
        "predicted_duration_s": None,
        "predicted_duration_min": None,
        "status": "insufficient_data",
        "reason": "zone does not have a stable model in the selected range",
    }
    if model is None:
        return result

    modeled_rate = model.predict_seconds_per_degree(outdoor_temp_f)
    if not math.isfinite(modeled_rate) or modeled_rate <= 0:
        result["status"] = "invalid_model_prediction"
        result["reason"] = "model predicts a non-positive seconds-per-degree value"
        return result

    outdoor_values = [row.outdoor_temp_f for row in rows]
    delta_values = [row.setpoint_delta_f for row in rows]
    extrapolated_dimensions: list[str] = []
    if outdoor_temp_f < min(outdoor_values) or outdoor_temp_f > max(outdoor_values):
        extrapolated_dimensions.append("outdoor_temp_f")
    if setpoint_delta_f < min(delta_values) or setpoint_delta_f > max(delta_values):
        extrapolated_dimensions.append("setpoint_delta_f")
    duration_s = model.predict_duration(outdoor_temp_f, setpoint_delta_f)
    if not math.isfinite(duration_s) or duration_s <= 0:
        result["status"] = "invalid_model_prediction"
        result["reason"] = "model predicts a non-positive duration"
        return result
    result.update(
        {
            "predicted_seconds_per_degree_s": _round(modeled_rate),
            "predicted_duration_s": _round(duration_s, 1),
            "predicted_duration_min": _round(duration_s / 60.0, 1),
            "status": "extrapolated" if extrapolated_dimensions else "ok",
            "reason": (
                "query is outside the observed training range for: "
                + ", ".join(extrapolated_dimensions)
                if extrapolated_dimensions
                else "prediction is within the observed training ranges"
            ),
            "extrapolated_dimensions": extrapolated_dimensions,
        }
    )
    return result


def build_report(
    observations: Iterable[TimeToTempObservation],
    start: date,
    end: date,
    *,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    bucket_width_f: float = DEFAULT_BUCKET_WIDTH_F,
    source: str = "events.jsonl",
    data_quality: dict[str, int] | None = None,
    query_zone: str | None = None,
    query_outdoor_temp_f: float | None = None,
    query_setpoint_delta_f: float | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable model report and optional prediction."""
    if start > end:
        raise ValueError("start date must be on or before end date")
    if min_observations < 2:
        raise ValueError("min_observations must be at least 2")
    if not math.isfinite(bucket_width_f) or bucket_width_f <= 0:
        raise ValueError("bucket_width_f must be greater than 0")
    query_values = (query_zone, query_outdoor_temp_f, query_setpoint_delta_f)
    if any(value is not None for value in query_values) and not all(
        value is not None for value in query_values
    ):
        raise ValueError("--zone, --outdoor, and --delta must be provided together")
    if query_outdoor_temp_f is not None and not math.isfinite(query_outdoor_temp_f):
        raise ValueError("outdoor temperature must be finite")
    if query_setpoint_delta_f is not None and (
        not math.isfinite(query_setpoint_delta_f) or query_setpoint_delta_f <= 0
    ):
        raise ValueError("setpoint delta must be greater than 0")

    rows = sorted(
        [row for row in observations if start <= row.timestamp.date() <= end],
        key=lambda row: (row.timestamp, row.zone),
    )
    grouped: defaultdict[str, list[TimeToTempObservation]] = defaultdict(list)
    for row in rows:
        grouped[row.zone].append(row)

    zones = sorted(set(KNOWN_ZONES) | set(grouped))
    zone_reports: list[dict[str, Any]] = []
    model_by_zone: dict[str, LinearModel | None] = {}
    rows_by_zone: dict[str, list[TimeToTempObservation]] = {}
    bucket_count = 0
    for zone in zones:
        zone_rows = grouped.get(zone, [])
        model = fit_linear_model(zone_rows) if len(zone_rows) >= min_observations else None
        model_by_zone[zone] = model
        rows_by_zone[zone] = zone_rows
        by_bucket: defaultdict[tuple[float, float], list[TimeToTempObservation]] = defaultdict(list)
        for row in zone_rows:
            by_bucket[(row.outdoor_bucket_lower_f, row.outdoor_bucket_upper_f)].append(row)
        buckets = [
            _bucket_report(by_bucket[bounds], min_observations) for bounds in sorted(by_bucket)
        ]
        bucket_count += len(buckets)
        if len(zone_rows) < min_observations:
            status = "insufficient_data"
            reason = "fewer than the configured minimum observations"
        elif model is None:
            status = "no_outdoor_variation"
            reason = "training observations do not span more than one outdoor temperature"
        else:
            status = "ok"
            reason = "stable per-zone outdoor-temperature model available"
        zone_reports.append(
            {
                "zone": zone,
                "observation_count": len(zone_rows),
                "min_observations": min_observations,
                "status": status,
                "reason": reason,
                "model": _model_dict(model, zone_rows),
                "bucket_count": len(buckets),
                "buckets": buckets,
            }
        )

    quality = dict(data_quality or {})
    quality.setdefault("eligible_observations", len(rows))
    prediction = None
    if query_zone is not None and query_outdoor_temp_f is not None and query_setpoint_delta_f:
        prediction = _prediction_report(
            query_zone,
            query_outdoor_temp_f,
            query_setpoint_delta_f,
            rows_by_zone.get(query_zone, []),
            model_by_zone.get(query_zone),
            min_observations,
            bucket_width_f,
        )

    return {
        "schema": "homeops.time-to-temp-model-report.v1",
        "source": source,
        "method": (
            "Fit one ordinary-least-squares model per zone to seconds per degree "
            "(duration_s / setpoint_delta_f) versus outdoor temperature. "
            "Multiply the modeled seconds per degree by the requested positive "
            "setpoint delta; retain half-open outdoor buckets for auditability."
        ),
        "coverage": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "eligible_observations": len(rows),
            "zones": len(zones),
            "zone_outdoor_buckets": bucket_count,
        },
        "configuration": {
            "min_observations": min_observations,
            "bucket_width_f": bucket_width_f,
            "duration_units": "seconds to reach setpoint",
            "setpoint_delta_units": "degrees Fahrenheit",
            "outdoor_temperature_units": "degrees Fahrenheit",
            "model_target": "seconds per degree",
        },
        "prediction": prediction,
        "data_quality": quality,
        "zones": zone_reports,
        "interpretation_guard": (
            "This is an observed planning estimate, not a thermostat control signal, "
            "guaranteed arrival time, furnace-efficiency rating, or proof of an "
            "insulation/equipment fault. Predictions marked extrapolated are outside "
            "the zone's observed training range; confirm any operational decision "
            "against current telemetry, schedules, weather, and maintenance history."
        ),
    }


def _fmt_seconds(value: float | int | None) -> str:
    """Format seconds compactly for Markdown output."""
    if value is None:
        return "—"
    total = int(round(float(value)))
    minutes, seconds = divmod(total, 60)
    return f"{minutes}m {seconds:02d}s"


def _fmt_number(value: float | int | None, digits: int = 2) -> str:
    """Format an optional numeric Markdown value."""
    return "—" if value is None else f"{float(value):.{digits}f}"


def render_markdown(report: dict[str, Any], file: TextIO | None = None) -> str:
    """Render a compact human-readable model report."""
    lines = [
        "# Zone time-to-temperature model",
        "",
        f"Source: `{report['source']}`",
        (
            f"Coverage: `{report['coverage']['start']}` → `{report['coverage']['end']}`; "
            f"{report['coverage']['eligible_observations']} eligible observations"
        ),
        "",
        (
            "The model estimates seconds per degree from outdoor temperature, then scales by "
            "the requested setpoint delta."
        ),
        "",
    ]
    prediction = report.get("prediction")
    if prediction is not None:
        lines.extend(
            [
                "## Prediction",
                "",
                (
                    f"- Query: `{prediction['zone']}`, `{prediction['outdoor_temp_f']:.1f}°F`, "
                    f"`{prediction['setpoint_delta_f']:.2f}°F`"
                ),
                f"- Predicted duration: **{_fmt_seconds(prediction['predicted_duration_s'])}**",
                f"- Status: `{prediction['status']}` — {prediction['reason']}",
                "",
            ]
        )

    lines.extend(
        [
            "## Zone models",
            "",
            "| Zone | Observations | Outdoor slope (s/°F/°F) | R² | Buckets | Status |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for zone in report["zones"]:
        model = zone["model"] or {}
        lines.append(
            f"| {zone['zone']} | {zone['observation_count']} | "
            f"{_fmt_number(model.get('slope_s_per_degree_per_f'))} | "
            f"{_fmt_number(model.get('r_squared'), 3)} | {zone['bucket_count']} | "
            f"{zone['status']} |"
        )

    lines.extend(
        [
            "",
            "## Outdoor-temperature bucket observations",
            "",
            "| Zone | Bucket | Observations | Median duration | Median seconds/°F | Status |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    for zone in report["zones"]:
        if not zone["buckets"]:
            lines.append(f"| {zone['zone']} | — | 0 | — | — | {zone['status']} |")
            continue
        for bucket in zone["buckets"]:
            lines.append(
                f"| {zone['zone']} | {bucket['outdoor_temp_bucket']} | "
                f"{bucket['observation_count']} | "
                f"{_fmt_seconds(bucket['median_duration_s'])} | "
                f"{_fmt_number(bucket['median_seconds_per_degree_s'])} | {bucket['status']} |"
            )

    quality = report["data_quality"]
    lines.extend(["", "## Data quality", ""])
    for key, label in (
        ("lines_seen", "Lines seen"),
        ("relevant_events", "Completed time-to-temp events"),
        ("duplicate_events", "Exact duplicate events"),
        ("malformed_json", "Malformed JSON lines"),
        ("events_missing_data", "Events missing data"),
        ("events_missing_timestamp", "Events missing timestamp"),
        ("events_out_of_range", "Events outside selected range"),
        ("events_unknown_zone", "Events with unknown zone"),
        ("events_missing_measurement", "Events missing model measurement"),
        ("events_with_invalid_measurement", "Events with invalid measurement"),
    ):
        if key in quality:
            lines.append(f"- {label}: {quality[key]}")
    lines.extend(
        [
            f"- Eligible observations: {report['coverage']['eligible_observations']}",
            "",
            "## Interpretation guard",
            "",
            report["interpretation_guard"],
        ]
    )
    output = "\n".join(lines).rstrip("\n")
    if file is not None:
        print(output, file=file)
    return output


def _parse_date(value: str) -> date:
    """Parse an ISO calendar date for argparse."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from exc


def _positive_int(value: str) -> int:
    """Parse a positive integer."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _minimum_observations(value: str) -> int:
    """Parse a model minimum that supports a line fit."""
    parsed = _positive_int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("min-observations must be at least 2")
    return parsed


def _positive_float(value: str) -> float:
    """Parse a finite positive float."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive number: {value}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _finite_float(value: str) -> float:
    """Parse a finite float, allowing negative outdoor temperatures."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a finite number: {value}") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("value must be finite")
    return parsed


def _resolve_range(
    days: int = DEFAULT_DAYS,
    start: date | None = None,
    end: date | None = None,
) -> tuple[date, date]:
    """Resolve an inclusive UTC date range from explicit dates or trailing days."""
    if (start is None) != (end is None):
        raise ValueError("--start and --end must be provided together")
    if start is not None and end is not None:
        if start > end:
            raise ValueError("start date must be on or before end date")
        return start, end
    if days < 1:
        raise ValueError("days must be at least 1")
    end = datetime.now(UTC).date()
    return end - timedelta(days=days - 1), end


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    min_observations_help = "Minimum observations required for a zone model"
    min_observations_help += f" (default: {DEFAULT_MIN_OBSERVATIONS})"
    parser.add_argument(
        "--days",
        type=_positive_int,
        default=DEFAULT_DAYS,
        help=f"Number of trailing UTC days to include (default: {DEFAULT_DAYS})",
    )
    parser.add_argument("--start", type=_parse_date, help="Inclusive UTC start date")
    parser.add_argument("--end", type=_parse_date, help="Inclusive UTC end date")
    parser.add_argument(
        "--zone",
        help="Zone to predict (for example, floor_2); omit to render all models",
    )
    parser.add_argument(
        "--outdoor",
        type=_finite_float,
        help="Outdoor temperature in °F for a prediction query",
    )
    parser.add_argument(
        "--delta",
        type=_positive_float,
        help="Requested positive setpoint delta in °F for a prediction query",
    )
    parser.add_argument(
        "--min-observations",
        type=_minimum_observations,
        default=DEFAULT_MIN_OBSERVATIONS,
        help=min_observations_help,
    )
    parser.add_argument(
        "--bucket-width-f",
        type=_positive_float,
        default=DEFAULT_BUCKET_WIDTH_F,
        help=f"Outdoor bucket width in °F (default: {DEFAULT_BUCKET_WIDTH_F})",
    )
    parser.add_argument("--log", default=None, help="Derived event JSONL path")
    parser.add_argument("--out", help="Optional output path; defaults to stdout")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the time-to-temperature model CLI."""
    args = _parse_args(argv)
    try:
        start, end = _resolve_range(args.days, args.start, args.end)
        log_path = args.log or os.environ.get("DERIVED_EVENT_LOG", DEFAULT_LOG)
        observations, quality = load_observations(
            log_path,
            start,
            end,
            bucket_width_f=args.bucket_width_f,
        )
        report = build_report(
            observations,
            start,
            end,
            min_observations=args.min_observations,
            bucket_width_f=args.bucket_width_f,
            source=str(log_path),
            data_quality=quality,
            query_zone=args.zone,
            query_outdoor_temp_f=args.outdoor,
            query_setpoint_delta_f=args.delta,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output = (
        json.dumps(report, indent=2, sort_keys=True)
        if args.format == "json"
        else render_markdown(report)
    )
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        prediction = report.get("prediction")
        detail = f" ({prediction['status']} prediction)" if prediction is not None else ""
        print(
            f"Report written → {output_path} "
            f"({report['coverage']['eligible_observations']} eligible observations){detail}"
        )
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
