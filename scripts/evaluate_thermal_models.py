#!/usr/bin/env python3
"""Train and evaluate the first offline HomeOps thermal-response models.

The evaluator consumes validated ``homeops.thermal.training_row.v1`` JSONL.
It fits a historical-median reference, a transparent degree-minute response
model, and a small standard-library Ridge regression model on an earlier
chronological partition. It then scores the same candidates on later
validation and test partitions without reading or changing live HomeOps state.

This module deliberately has no NumPy or scikit-learn dependency.  The Ridge
solver and feature encoder are small enough to keep the reproducible offline
boundary self-contained; the resulting coefficients and encoder statistics
are persisted in the artifact output for audit and later reuse.

Revision history:
  2026-08-31  Add the v1 offline baseline/model trainer and time-aware
              evaluator for the validated thermal training-row contract.
  2026-09-01  Add explicit mode-aware chronological partitions for uneven
              heating and cooling histories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

TRAINING_ROW_SCHEMA = "homeops.thermal.training_row.v1"
EVALUATION_SCHEMA = "homeops.thermal.training_evaluation.v1"
ARTIFACT_SCHEMA = "homeops.thermal.model_artifacts.v1"
MODEL_VERSION = "homeops.thermal.baselines.v1"
FEATURE_SCHEMA = "homeops.thermal.features.v1"

ZONES = ("floor_1", "floor_2", "floor_3")
MODES = ("heat", "cool")
TARGETS = (
    ("time_to_setpoint_s", "time_to_setpoint"),
    ("zone_runtime_s", "zone_runtime"),
)

DEFAULT_VALIDATION_FRACTION = 0.20
DEFAULT_TEST_FRACTION = 0.20
DEFAULT_MINIMUM_ELIGIBLE_ROWS = 3
DEFAULT_RIDGE_ALPHA = 1.0
DEFAULT_INTERVAL_LEVEL = 0.80
DEFAULT_SPLIT_STRATEGY = "global"
SPLIT_STRATEGIES = ("global", "mode_aware")

NUMERIC_FEATURES = (
    "start_temp_f",
    "start_setpoint_f",
    "setpoint_delta_f",
    "outdoor_temp_f",
    "outdoor_temp_age_s",
    "concurrent_zone_count",
    "start_minute_of_day_local",
    "prior_zone_runtime_24h_s",
)
BOOLEAN_FEATURES = ("prior_zone_runtime_history_complete",)
OTHER_ZONE_FEATURE = "other_zones_calling"

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

EPSILON = 1e-12


def _finite_number(value: Any) -> float | None:
    """Return a finite number while excluding booleans and numeric strings."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a timezone-aware ISO timestamp and normalize it to UTC."""

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


def _slice_key(row: dict[str, Any]) -> str:
    """Return the stable floor/mode key used for per-slice reporting."""

    return f"{row['zone']}:{row['mode']}"


def _target_key(target: str) -> str:
    """Return the status key corresponding to a label field."""

    for label, status in TARGETS:
        if target == label:
            return status
    raise ValueError(f"unknown target: {target}")


def _row_is_eligible(row: dict[str, Any], target: str) -> bool:
    """Return whether a validated row has a usable completed target label."""

    statuses = row.get("label_status")
    labels = row.get("labels")
    if not isinstance(statuses, dict) or not isinstance(labels, dict):
        return False
    status = statuses.get(_target_key(target))
    value = _finite_number(labels.get(target))
    return status == "eligible" and value is not None and value > 0


def _contains_forbidden_key(value: Any) -> bool:
    """Detect a future label accidentally placed inside the feature object."""

    if isinstance(value, dict):
        if any(key in FORBIDDEN_FEATURE_KEYS for key in value):
            return True
        return any(_contains_forbidden_key(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_key(child) for child in value)
    return False


def _row_sort_key(row: dict[str, Any]) -> tuple[datetime, str]:
    timestamp = _parse_timestamp(row.get("prediction_ts"))
    if timestamp is None:
        raise ValueError(f"row {row.get('row_id', '<unknown>')!r} has an invalid prediction_ts")
    row_id = row.get("row_id")
    if not isinstance(row_id, str) or not row_id:
        raise ValueError("every training row must have a non-empty row_id")
    return timestamp, row_id


def _experiment_group_key(row: dict[str, Any]) -> str:
    """Keep rows from one deliberate experiment in one chronological split."""

    provenance = row.get("provenance")
    experiment = provenance.get("experiment") if isinstance(provenance, dict) else None
    if isinstance(experiment, dict):
        for field in ("experiment_id", "test_id", "experiment_name"):
            value = experiment.get(field)
            if isinstance(value, str) and value.strip():
                return f"experiment:{field}:{value.strip()}"
    return f"row:{row['row_id']}"


@dataclass(frozen=True)
class LoadedDataset:
    """Input rows plus the content hash used for reproducibility metadata."""

    rows: list[dict[str, Any]]
    sha256: str
    source_lines: int


@dataclass(frozen=True)
class ChronologicalSplit:
    """A chronological split whose group boundaries cannot leak experiments."""

    train: list[dict[str, Any]]
    validation: list[dict[str, Any]]
    test: list[dict[str, Any]]
    train_groups: tuple[str, ...]
    validation_groups: tuple[str, ...]
    test_groups: tuple[str, ...]

    @staticmethod
    def _partition_metadata(rows: list[dict[str, Any]], groups: tuple[str, ...]) -> dict[str, Any]:
        timestamps = [_parse_timestamp(row["prediction_ts"]) for row in rows]
        parsed = [timestamp for timestamp in timestamps if timestamp is not None]
        return {
            "rows": len(rows),
            "groups": len(groups),
            "min_prediction_ts": min(parsed).isoformat() if parsed else None,
            "max_prediction_ts": max(parsed).isoformat() if parsed else None,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return deterministic split boundaries without embedding raw rows."""

        return {
            "strategy": "chronological",
            "train": self._partition_metadata(self.train, self.train_groups),
            "validation": self._partition_metadata(self.validation, self.validation_groups),
            "test": self._partition_metadata(self.test, self.test_groups),
            "group_isolation": True,
        }


@dataclass(frozen=True)
class ModeAwareSplit:
    """Chronological partitions computed independently for each HVAC mode."""

    by_mode: dict[str, ChronologicalSplit]
    train: list[dict[str, Any]]
    validation: list[dict[str, Any]]
    test: list[dict[str, Any]]
    train_groups: tuple[str, ...]
    validation_groups: tuple[str, ...]
    test_groups: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return per-mode boundaries without implying one global time boundary."""

        return {
            "strategy": "mode_aware_chronological",
            "group_isolation": True,
            "partitions": {
                "train": {"rows": len(self.train), "groups": len(self.train_groups)},
                "validation": {
                    "rows": len(self.validation),
                    "groups": len(self.validation_groups),
                },
                "test": {"rows": len(self.test), "groups": len(self.test_groups)},
            },
            "by_mode": {mode: split.to_dict() for mode, split in sorted(self.by_mode.items())},
        }


def load_dataset(path: str | Path) -> LoadedDataset:
    """Load a validator-produced JSONL file and reject unsafe input early."""

    if str(path) == "-":
        raw = sys.stdin.buffer.read()
        source_name = "stdin"
    else:
        input_path = Path(path)
        raw = input_path.read_bytes()
        source_name = str(input_path)

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    source_lines = 0
    for source_lines, line_bytes in enumerate(raw.splitlines(), start=1):
        if not line_bytes.strip():
            continue
        try:
            value = json.loads(line_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {source_lines} of {source_name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"line {source_lines} of {source_name} is not a JSON object")
        if value.get("schema") != TRAINING_ROW_SCHEMA:
            raise ValueError(f"line {source_lines} of {source_name} is not {TRAINING_ROW_SCHEMA}")
        row_id = value.get("row_id")
        if not isinstance(row_id, str) or not row_id.strip():
            raise ValueError(f"line {source_lines} of {source_name} has no row_id")
        if row_id in seen_ids:
            raise ValueError(f"duplicate row_id {row_id!r} on line {source_lines}")
        if value.get("zone") not in ZONES or value.get("mode") not in MODES:
            raise ValueError(f"row {row_id!r} has an unknown zone or mode")
        if _parse_timestamp(value.get("prediction_ts")) is None:
            raise ValueError(f"row {row_id!r} has an invalid prediction_ts")
        features = value.get("features")
        if not isinstance(features, dict):
            raise ValueError(f"row {row_id!r} has no feature object")
        if _contains_forbidden_key(features):
            raise ValueError(f"row {row_id!r} contains a future value inside features")
        if not any(_row_is_eligible(value, target) for target, _ in TARGETS):
            raise ValueError(f"row {row_id!r} has no eligible target label")
        seen_ids.add(row_id)
        rows.append(value)

    rows.sort(key=_row_sort_key)
    if not rows:
        raise ValueError("training dataset contains no rows")
    return LoadedDataset(rows, hashlib.sha256(raw).hexdigest(), source_lines)


def _group_rows(rows: Iterable[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_experiment_group_key(row)].append(row)
    ordered: list[tuple[str, list[dict[str, Any]]]] = []
    for key, group in grouped.items():
        group.sort(key=_row_sort_key)
        ordered.append((key, group))
    ordered.sort(key=lambda item: (_row_sort_key(item[1][0]), item[0]))
    return ordered


def _boundary_for_target(
    group_sizes: list[int],
    target_rows: float,
    *,
    minimum_groups: int,
    maximum_groups: int,
) -> int:
    """Choose the earliest group boundary near a desired row-count boundary."""

    if minimum_groups > maximum_groups:
        return maximum_groups
    cumulative = 0
    for end, size in enumerate(group_sizes, start=1):
        cumulative += size
        if end >= minimum_groups and cumulative >= target_rows:
            return min(end, maximum_groups)
    return maximum_groups


def chronological_split(
    rows: Iterable[dict[str, Any]],
    *,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    test_fraction: float = DEFAULT_TEST_FRACTION,
) -> ChronologicalSplit:
    """Split earlier rows from later rows while preserving experiment groups."""

    if not math.isfinite(validation_fraction) or not 0 <= validation_fraction < 1:
        raise ValueError("validation_fraction must be in [0, 1)")
    if not math.isfinite(test_fraction) or not 0 <= test_fraction < 1:
        raise ValueError("test_fraction must be in [0, 1)")
    if validation_fraction + test_fraction >= 1:
        raise ValueError("validation_fraction + test_fraction must be less than 1")

    groups = _group_rows(rows)
    if not groups:
        raise ValueError("cannot split an empty dataset")
    group_sizes = [len(group) for _, group in groups]
    total_rows = sum(group_sizes)
    group_count = len(groups)

    if group_count == 1:
        train_end = 1
        validation_end = 1
    elif group_count == 2:
        train_end = 1
        validation_end = 1
    else:
        train_target = total_rows * (1 - validation_fraction - test_fraction)
        validation_target = total_rows * (1 - test_fraction)
        train_end = _boundary_for_target(
            group_sizes,
            train_target,
            minimum_groups=1,
            maximum_groups=group_count - 2,
        )
        validation_end = _boundary_for_target(
            group_sizes,
            validation_target,
            minimum_groups=train_end + 1,
            maximum_groups=group_count - 1,
        )

    train_groups = tuple(key for key, _ in groups[:train_end])
    validation_groups = tuple(key for key, _ in groups[train_end:validation_end])
    test_groups = tuple(key for key, _ in groups[validation_end:])
    train = [row for _, group in groups[:train_end] for row in group]
    validation = [row for _, group in groups[train_end:validation_end] for row in group]
    test = [row for _, group in groups[validation_end:] for row in group]
    return ChronologicalSplit(
        train=train,
        validation=validation,
        test=test,
        train_groups=train_groups,
        validation_groups=validation_groups,
        test_groups=test_groups,
    )


def mode_aware_split(
    rows: Iterable[dict[str, Any]],
    *,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    test_fraction: float = DEFAULT_TEST_FRACTION,
) -> ModeAwareSplit:
    """Split each HVAC mode chronologically while failing closed on shared groups."""

    materialized = list(rows)
    if not materialized:
        raise ValueError("cannot split an empty dataset")
    unknown_modes = sorted({row.get("mode") for row in materialized} - set(MODES))
    if unknown_modes:
        raise ValueError(f"mode-aware split has unknown modes: {unknown_modes}")

    by_mode = {
        mode: chronological_split(
            [row for row in materialized if row.get("mode") == mode],
            validation_fraction=validation_fraction,
            test_fraction=test_fraction,
        )
        for mode in MODES
        if any(row.get("mode") == mode for row in materialized)
    }

    group_partitions: defaultdict[str, set[str]] = defaultdict(set)
    for split in by_mode.values():
        for partition, groups in (
            ("train", split.train_groups),
            ("validation", split.validation_groups),
            ("test", split.test_groups),
        ):
            for group in groups:
                group_partitions[group].add(partition)
    conflicting_groups = sorted(
        group for group, partitions in group_partitions.items() if len(partitions) > 1
    )
    if conflicting_groups:
        sample = ", ".join(repr(group) for group in conflicting_groups[:3])
        raise ValueError(
            "mode-aware split would divide shared groups across partitions: "
            f"{sample}; use the global strategy or align the experiment boundaries"
        )

    def _combined_rows(partition: str) -> list[dict[str, Any]]:
        rows_by_partition = {
            "train": lambda split: split.train,
            "validation": lambda split: split.validation,
            "test": lambda split: split.test,
        }
        return sorted(
            [row for split in by_mode.values() for row in rows_by_partition[partition](split)],
            key=_row_sort_key,
        )

    train = _combined_rows("train")
    validation = _combined_rows("validation")
    test = _combined_rows("test")
    train_groups = tuple(
        sorted({group for split in by_mode.values() for group in split.train_groups})
    )
    validation_groups = tuple(
        sorted({group for split in by_mode.values() for group in split.validation_groups})
    )
    test_groups = tuple(
        sorted({group for split in by_mode.values() for group in split.test_groups})
    )
    return ModeAwareSplit(
        by_mode=by_mode,
        train=train,
        validation=validation,
        test=test,
        train_groups=train_groups,
        validation_groups=validation_groups,
        test_groups=test_groups,
    )


def _quantile(values: Iterable[float], level: float) -> float | None:
    """Return a deterministic linearly interpolated empirical quantile."""

    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    if not 0 <= level <= 1:
        raise ValueError("quantile level must be in [0, 1]")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * level
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _ordinary_least_squares(samples: list[tuple[float, float]]) -> tuple[float, float]:
    """Fit y = intercept + slope*x with a stable constant-x fallback."""

    if not samples:
        raise ValueError("cannot fit an empty regression")
    mean_x = sum(x for x, _ in samples) / len(samples)
    mean_y = sum(y for _, y in samples) / len(samples)
    denominator = sum((x - mean_x) ** 2 for x, _ in samples)
    if denominator <= EPSILON:
        return mean_y, 0.0
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in samples)
    slope = numerator / denominator
    return mean_y - slope * mean_x, slope


@dataclass(frozen=True)
class MedianFit:
    target: str
    slice_key: str
    median: float
    training_rows: int
    interval_width: float | None
    interval_level: float

    def predict(self, row: dict[str, Any]) -> float:
        return self.median

    def artifact(self) -> dict[str, Any]:
        return {
            "kind": "historical_median",
            "target": self.target,
            "slice": self.slice_key,
            "median_s": self.median,
            "training_rows": self.training_rows,
            "interval_level": self.interval_level,
            "calibration_abs_error_quantile_s": self.interval_width,
            "calibration_source": "training_residuals",
        }


@dataclass(frozen=True)
class DegreeMinuteFit:
    target: str
    slice_key: str
    intercept_s_per_degree: float
    slope_s_per_degree_per_f: float
    training_rows: int
    fit_rows: int
    interval_width: float | None
    interval_level: float

    def predict(self, row: dict[str, Any]) -> float | None:
        features = row.get("features")
        if not isinstance(features, dict):
            return None
        gap = _finite_number(features.get("setpoint_delta_f"))
        outdoor = _finite_number(features.get("outdoor_temp_f"))
        if gap is None or gap <= 0 or outdoor is None:
            return None
        seconds_per_degree = self.intercept_s_per_degree + (self.slope_s_per_degree_per_f * outdoor)
        prediction = seconds_per_degree * gap
        if not math.isfinite(prediction) or prediction <= 0:
            return None
        return prediction

    def artifact(self) -> dict[str, Any]:
        return {
            "kind": "degree_minute_thermal_response",
            "target": self.target,
            "slice": self.slice_key,
            "formula": (
                "predicted_duration_s = "
                "(intercept_s_per_degree + slope_s_per_degree_per_f * outdoor_temp_f) "
                "* setpoint_delta_f"
            ),
            "intercept_s_per_degree": self.intercept_s_per_degree,
            "slope_s_per_degree_per_f": self.slope_s_per_degree_per_f,
            "training_rows": self.training_rows,
            "fit_rows": self.fit_rows,
            "interval_level": self.interval_level,
            "calibration_abs_error_quantile_s": self.interval_width,
            "calibration_source": "training_residuals",
        }


def _fit_median_models(
    rows: Iterable[dict[str, Any]],
    target: str,
    *,
    minimum_rows: int,
    interval_level: float,
) -> dict[str, MedianFit]:
    grouped: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        if _row_is_eligible(row, target):
            grouped[_slice_key(row)].append(float(row["labels"][target]))

    fits: dict[str, MedianFit] = {}
    for slice_key, values in sorted(grouped.items()):
        if len(values) < minimum_rows:
            continue
        median = _quantile(values, 0.5)
        if median is None:
            continue
        residuals = [abs(value - median) for value in values]
        fits[slice_key] = MedianFit(
            target=target,
            slice_key=slice_key,
            median=median,
            training_rows=len(values),
            interval_width=_quantile(residuals, interval_level),
            interval_level=interval_level,
        )
    return fits


def _fit_degree_minute_models(
    rows: Iterable[dict[str, Any]],
    target: str,
    *,
    minimum_rows: int,
    interval_level: float,
) -> dict[str, DegreeMinuteFit]:
    grouped: defaultdict[str, list[tuple[float, float, float]]] = defaultdict(list)
    training_counts: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        if not _row_is_eligible(row, target):
            continue
        slice_key = _slice_key(row)
        training_counts[slice_key] += 1
        features = row.get("features")
        if not isinstance(features, dict):
            continue
        gap = _finite_number(features.get("setpoint_delta_f"))
        outdoor = _finite_number(features.get("outdoor_temp_f"))
        label = _finite_number(row.get("labels", {}).get(target))
        if gap is None or gap <= 0 or outdoor is None or label is None or label <= 0:
            continue
        grouped[slice_key].append((outdoor, label / gap, gap))

    fits: dict[str, DegreeMinuteFit] = {}
    for slice_key, samples in sorted(grouped.items()):
        if len(samples) < minimum_rows:
            continue
        intercept, slope = _ordinary_least_squares([(x, rate) for x, rate, _ in samples])
        residuals: list[float] = []
        for outdoor, rate, gap in samples:
            prediction = (intercept + slope * outdoor) * gap
            residuals.append(abs(prediction - (rate * gap)))
        fits[slice_key] = DegreeMinuteFit(
            target=target,
            slice_key=slice_key,
            intercept_s_per_degree=intercept,
            slope_s_per_degree_per_f=slope,
            training_rows=training_counts[slice_key],
            fit_rows=len(samples),
            interval_width=_quantile(residuals, interval_level),
            interval_level=interval_level,
        )
    return fits


def _raw_numeric_features(row: dict[str, Any]) -> dict[str, float | None]:
    features = row.get("features")
    features = features if isinstance(features, dict) else {}
    return {name: _finite_number(features.get(name)) for name in NUMERIC_FEATURES}


def _raw_boolean_features(row: dict[str, Any]) -> dict[str, float | None]:
    features = row.get("features")
    features = features if isinstance(features, dict) else {}
    result: dict[str, float | None] = {}
    for name in BOOLEAN_FEATURES:
        value = features.get(name)
        result[name] = float(value) if isinstance(value, bool) else None
    return result


@dataclass(frozen=True)
class RidgeEncoder:
    numeric_means: dict[str, float]
    numeric_scales: dict[str, float]
    feature_names: tuple[str, ...]

    def encode(self, row: dict[str, Any]) -> list[float]:
        numeric = _raw_numeric_features(row)
        boolean = _raw_boolean_features(row)
        features = row.get("features")
        features = features if isinstance(features, dict) else {}
        vector: list[float] = []
        for name in NUMERIC_FEATURES:
            value = numeric[name]
            vector.append(
                (value - self.numeric_means[name]) / self.numeric_scales[name]
                if value is not None
                else 0.0
            )
            vector.append(0.0 if value is not None else 1.0)
        for name in BOOLEAN_FEATURES:
            value = boolean[name]
            vector.append(value if value is not None else 0.0)
            vector.append(0.0 if value is not None else 1.0)

        other_zones = features.get(OTHER_ZONE_FEATURE)
        known_other_zones = (
            {value for value in other_zones if value in ZONES}
            if isinstance(other_zones, list)
            else set()
        )
        vector.append(0.0 if isinstance(other_zones, list) else 1.0)
        vector.extend(1.0 if zone in known_other_zones else 0.0 for zone in ZONES)
        vector.extend(1.0 if row.get("zone") == zone else 0.0 for zone in ZONES)
        vector.extend(1.0 if row.get("mode") == mode else 0.0 for mode in MODES)
        return vector

    def artifact(self) -> dict[str, Any]:
        return {
            "feature_schema": FEATURE_SCHEMA,
            "feature_names": list(self.feature_names),
            "numeric_means": self.numeric_means,
            "numeric_scales": self.numeric_scales,
            "missing_numeric_values_are_mean_imputed": True,
            "missing_indicators_included": True,
            "categorical_values": {
                "zone": list(ZONES),
                "mode": list(MODES),
                "other_zones_calling": list(ZONES),
            },
        }


def _build_ridge_encoder(rows: list[dict[str, Any]]) -> RidgeEncoder:
    numeric_means: dict[str, float] = {}
    numeric_scales: dict[str, float] = {}
    for name in NUMERIC_FEATURES:
        values = [
            value
            for value in (_raw_numeric_features(row)[name] for row in rows)
            if value is not None
        ]
        mean = sum(values) / len(values) if values else 0.0
        variance = sum((value - mean) ** 2 for value in values) / len(values) if values else 0.0
        scale = math.sqrt(variance)
        numeric_means[name] = mean
        numeric_scales[name] = scale if math.isfinite(scale) and scale > EPSILON else 1.0

    feature_names: list[str] = []
    for name in NUMERIC_FEATURES:
        feature_names.extend((name, f"{name}__missing"))
    for name in BOOLEAN_FEATURES:
        feature_names.extend((name, f"{name}__missing"))
    feature_names.append(f"{OTHER_ZONE_FEATURE}__missing")
    feature_names.extend(f"{OTHER_ZONE_FEATURE}={zone}" for zone in ZONES)
    feature_names.extend(f"zone={zone}" for zone in ZONES)
    feature_names.extend(f"mode={mode}" for mode in MODES)
    return RidgeEncoder(numeric_means, numeric_scales, tuple(feature_names))


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small dense system with deterministic partial-pivot elimination."""

    size = len(vector)
    if size == 0 or len(matrix) != size or any(len(row) != size for row in matrix):
        raise ValueError("linear system has inconsistent dimensions")
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= EPSILON:
            raise ValueError("Ridge linear system is singular")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        augmented[column] = [value / pivot_value for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) <= EPSILON:
                continue
            augmented[row] = [
                left - factor * right for left, right in zip(augmented[row], augmented[column])
            ]
    return [augmented[index][-1] for index in range(size)]


@dataclass(frozen=True)
class RidgeFit:
    target: str
    alpha: float
    encoder: RidgeEncoder
    coefficients: tuple[float, ...]
    training_rows: int
    interval_width: float | None
    interval_level: float

    def predict(self, row: dict[str, Any]) -> float | None:
        values = [1.0, *self.encoder.encode(row)]
        if len(values) != len(self.coefficients):
            return None
        prediction = sum(
            coefficient * value for coefficient, value in zip(self.coefficients, values)
        )
        if not math.isfinite(prediction):
            return None
        return max(0.0, prediction)

    def artifact(self) -> dict[str, Any]:
        return {
            "kind": "ridge_regression",
            "target": self.target,
            "alpha": self.alpha,
            "intercept": self.coefficients[0],
            "coefficients": list(self.coefficients[1:]),
            "training_rows": self.training_rows,
            "interval_level": self.interval_level,
            "calibration_abs_error_quantile_s": self.interval_width,
            "calibration_source": "training_residuals",
            "encoder": self.encoder.artifact(),
        }


def _fit_ridge_model(
    rows: Iterable[dict[str, Any]],
    target: str,
    *,
    minimum_rows: int,
    alpha: float,
    interval_level: float,
) -> RidgeFit | None:
    training_rows = [row for row in rows if _row_is_eligible(row, target)]
    if len(training_rows) < minimum_rows:
        return None
    encoder = _build_ridge_encoder(training_rows)
    matrix_rows = [[1.0, *encoder.encode(row)] for row in training_rows]
    labels = [float(row["labels"][target]) for row in training_rows]
    size = len(matrix_rows[0])
    gram = [[0.0 for _ in range(size)] for _ in range(size)]
    rhs = [0.0 for _ in range(size)]
    for values, label in zip(matrix_rows, labels):
        for left in range(size):
            rhs[left] += values[left] * label
            for right in range(size):
                gram[left][right] += values[left] * values[right]
    for index in range(1, size):
        gram[index][index] += alpha
    coefficients = tuple(_solve_linear_system(gram, rhs))
    fitted = RidgeFit(
        target=target,
        alpha=alpha,
        encoder=encoder,
        coefficients=coefficients,
        training_rows=len(training_rows),
        interval_width=None,
        interval_level=interval_level,
    )
    residuals = []
    for row, label in zip(training_rows, labels):
        prediction = fitted.predict(row)
        if prediction is not None:
            residuals.append(abs(prediction - label))
    return RidgeFit(
        target=target,
        alpha=alpha,
        encoder=encoder,
        coefficients=coefficients,
        training_rows=len(training_rows),
        interval_width=_quantile(residuals, interval_level),
        interval_level=interval_level,
    )


def _empty_partition_result(
    rows: list[dict[str, Any]],
    *,
    fit_status: str,
    minimum_rows: int,
) -> dict[str, Any]:
    return {
        "status": "insufficient_data",
        "eligible_rows": len(rows),
        "predicted_rows": 0,
        "skipped_rows": 0,
        "minimum_eligible_rows": minimum_rows,
        "fit_status": fit_status,
        "mae_s": None,
        "p95_absolute_error_s": None,
        "signed_bias_s": None,
        "interval_coverage": None,
        "interval_width_s": None,
        "interval_level": None,
    }


def _evaluate_partition(
    model: MedianFit | DegreeMinuteFit | RidgeFit | None,
    rows: list[dict[str, Any]],
    target: str,
    *,
    minimum_rows: int,
    fit_status: str,
) -> dict[str, Any]:
    eligible_rows = [row for row in rows if _row_is_eligible(row, target)]
    if model is None:
        return _empty_partition_result(
            eligible_rows,
            fit_status=fit_status,
            minimum_rows=minimum_rows,
        )

    errors: list[float] = []
    intervals: list[bool] = []
    skipped = 0
    for row in eligible_rows:
        actual = float(row["labels"][target])
        prediction = model.predict(row)
        if prediction is None or not math.isfinite(prediction):
            skipped += 1
            continue
        error = prediction - actual
        errors.append(error)
        if model.interval_width is not None:
            lower = max(0.0, prediction - model.interval_width)
            upper = prediction + model.interval_width
            intervals.append(lower <= actual <= upper)

    predicted_rows = len(errors)
    status = "ok" if predicted_rows >= minimum_rows else "insufficient_data"
    if status != "ok":
        return {
            "status": status,
            "eligible_rows": len(eligible_rows),
            "predicted_rows": predicted_rows,
            "skipped_rows": skipped,
            "minimum_eligible_rows": minimum_rows,
            "fit_status": fit_status,
            "mae_s": None,
            "p95_absolute_error_s": None,
            "signed_bias_s": None,
            "interval_coverage": None,
            "interval_width_s": model.interval_width,
            "interval_level": model.interval_level,
        }

    absolute_errors = [abs(error) for error in errors]
    return {
        "status": status,
        "eligible_rows": len(eligible_rows),
        "predicted_rows": predicted_rows,
        "skipped_rows": skipped,
        "minimum_eligible_rows": minimum_rows,
        "fit_status": fit_status,
        "mae_s": sum(absolute_errors) / len(absolute_errors),
        "p95_absolute_error_s": _quantile(absolute_errors, 0.95),
        "signed_bias_s": sum(errors) / len(errors),
        "interval_coverage": sum(intervals) / len(intervals) if intervals else None,
        "interval_width_s": model.interval_width,
        "interval_level": model.interval_level if intervals else None,
    }


def _slice_rows(rows: list[dict[str, Any]], slice_key: str, target: str) -> list[dict[str, Any]]:
    return [row for row in rows if _slice_key(row) == slice_key and _row_is_eligible(row, target)]


def _all_slice_keys() -> tuple[str, ...]:
    return tuple(f"{zone}:{mode}" for zone in ZONES for mode in MODES)


def _mode_from_slice_key(slice_key: str) -> str:
    return slice_key.rsplit(":", 1)[1]


def _candidate_slice_result(
    model: MedianFit | DegreeMinuteFit | RidgeFit | None,
    train_rows: list[dict[str, Any]],
    validation_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
    target: str,
    *,
    minimum_rows: int,
) -> dict[str, Any]:
    fit_status = (
        "ok" if model is not None and len(train_rows) >= minimum_rows else "insufficient_data"
    )
    scoring_model = model if fit_status == "ok" else None
    return {
        "training_eligible_rows": len(train_rows),
        "validation_eligible_rows": len(validation_rows),
        "test_eligible_rows": len(test_rows),
        "fit_status": fit_status,
        "validation": _evaluate_partition(
            scoring_model,
            validation_rows,
            target,
            minimum_rows=minimum_rows,
            fit_status=fit_status,
        ),
        "test": _evaluate_partition(
            scoring_model,
            test_rows,
            target,
            minimum_rows=minimum_rows,
            fit_status=fit_status,
        ),
    }


def train_and_evaluate(
    rows: list[dict[str, Any]],
    *,
    dataset_sha256: str = "unspecified",
    code_version: str = "unknown",
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    minimum_eligible_rows: int = DEFAULT_MINIMUM_ELIGIBLE_ROWS,
    ridge_alpha: float = DEFAULT_RIDGE_ALPHA,
    interval_level: float = DEFAULT_INTERVAL_LEVEL,
    split_strategy: str = DEFAULT_SPLIT_STRATEGY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fit the v1 ladder and return a report plus reproducible artifacts."""

    if minimum_eligible_rows < 1:
        raise ValueError("minimum_eligible_rows must be at least 1")
    if not math.isfinite(ridge_alpha) or ridge_alpha <= 0:
        raise ValueError("ridge_alpha must be finite and greater than 0")
    if not math.isfinite(interval_level) or not 0 < interval_level < 1:
        raise ValueError("interval_level must be in (0, 1)")
    if split_strategy not in SPLIT_STRATEGIES:
        raise ValueError(f"split_strategy must be one of {SPLIT_STRATEGIES}")
    if not rows:
        raise ValueError("cannot evaluate an empty dataset")

    split: ChronologicalSplit | ModeAwareSplit
    if split_strategy == "global":
        split = chronological_split(
            rows,
            validation_fraction=validation_fraction,
            test_fraction=test_fraction,
        )
    else:
        split = mode_aware_split(
            rows,
            validation_fraction=validation_fraction,
            test_fraction=test_fraction,
        )
    median_models: dict[str, dict[str, MedianFit]] = {}
    degree_models: dict[str, dict[str, DegreeMinuteFit]] = {}
    ridge_models: dict[str, RidgeFit | None] = {}
    ridge_models_by_mode: dict[str, dict[str, RidgeFit | None]] = {}
    for target, _ in TARGETS:
        median_models[target] = _fit_median_models(
            split.train,
            target,
            minimum_rows=minimum_eligible_rows,
            interval_level=interval_level,
        )
        degree_models[target] = _fit_degree_minute_models(
            split.train,
            target,
            minimum_rows=minimum_eligible_rows,
            interval_level=interval_level,
        )
        if split_strategy == "mode_aware":
            ridge_models_by_mode[target] = {
                mode: _fit_ridge_model(
                    split.by_mode[mode].train,
                    target,
                    minimum_rows=minimum_eligible_rows,
                    alpha=ridge_alpha,
                    interval_level=interval_level,
                )
                for mode in sorted(split.by_mode)
            }
        else:
            ridge_models[target] = _fit_ridge_model(
                split.train,
                target,
                minimum_rows=minimum_eligible_rows,
                alpha=ridge_alpha,
                interval_level=interval_level,
            )

    by_slice: dict[str, Any] = {}
    for slice_key in _all_slice_keys():
        slice_result: dict[str, Any] = {}
        for target, _ in TARGETS:
            train_rows = _slice_rows(split.train, slice_key, target)
            validation_rows = _slice_rows(split.validation, slice_key, target)
            test_rows = _slice_rows(split.test, slice_key, target)
            ridge_model = (
                ridge_models_by_mode[target].get(_mode_from_slice_key(slice_key))
                if split_strategy == "mode_aware"
                else ridge_models[target]
            )
            slice_result[target] = {
                "historical_median": _candidate_slice_result(
                    median_models[target].get(slice_key),
                    train_rows,
                    validation_rows,
                    test_rows,
                    target,
                    minimum_rows=minimum_eligible_rows,
                ),
                "degree_minute_thermal_response": _candidate_slice_result(
                    degree_models[target].get(slice_key),
                    train_rows,
                    validation_rows,
                    test_rows,
                    target,
                    minimum_rows=minimum_eligible_rows,
                ),
                "ridge_regression": _candidate_slice_result(
                    ridge_model,
                    train_rows,
                    validation_rows,
                    test_rows,
                    target,
                    minimum_rows=minimum_eligible_rows,
                ),
            }
        by_slice[slice_key] = slice_result

    model_names = (
        "historical_median",
        "degree_minute_thermal_response",
        "ridge_regression",
    )
    candidates: dict[str, Any] = {}
    for model_name in model_names:
        fit_count = 0
        slice_count = 0
        for slice_result in by_slice.values():
            for target_result in slice_result.values():
                if target_result[model_name]["fit_status"] == "ok":
                    fit_count += 1
                if target_result[model_name]["test"]["status"] == "ok":
                    slice_count += 1
        candidates[model_name] = {
            "fit_target_slices": fit_count,
            "scored_test_slices": slice_count,
            "status": "ok" if fit_count else "insufficient_data",
        }

    configuration = {
        "validation_fraction": validation_fraction,
        "test_fraction": test_fraction,
        "minimum_eligible_rows": minimum_eligible_rows,
        "ridge_alpha": ridge_alpha,
        "interval_level": interval_level,
        "split_strategy": split_strategy,
        "ridge_model_scope": "mode_specific" if split_strategy == "mode_aware" else "pooled",
    }
    split_dict = split.to_dict()
    report = {
        "schema": EVALUATION_SCHEMA,
        "model_version": MODEL_VERSION,
        "feature_schema": FEATURE_SCHEMA,
        "training_row_schema": TRAINING_ROW_SCHEMA,
        "code_version": code_version,
        "dataset_sha256": dataset_sha256,
        "configuration": configuration,
        "input": {
            "rows": len(rows),
            "source_rows_with_any_eligible_target": sum(
                1 for row in rows if any(_row_is_eligible(row, target) for target, _ in TARGETS)
            ),
        },
        "split": split_dict,
        "candidates": candidates,
        "by_zone_mode": by_slice,
        "coverage_status": "insufficient_data",
    }
    all_test_results = [
        target_result[model_name]["test"]
        for slice_result in by_slice.values()
        for target_result in slice_result.values()
        for model_name in model_names
    ]
    if all_test_results and all(result["status"] == "ok" for result in all_test_results):
        report["coverage_status"] = "ok"
    if split_strategy == "mode_aware":
        ridge_artifacts: dict[str, Any] = {
            target: {
                "kind": "ridge_regression_by_mode",
                "target": target,
                "scope": "mode_specific",
                "by_mode": {
                    mode: (model.artifact() if model is not None else None)
                    for mode, model in sorted(ridge_models_by_mode[target].items())
                },
            }
            for target in sorted(ridge_models_by_mode)
        }
    else:
        ridge_artifacts = {
            target: model.artifact() if model is not None else None
            for target, model in sorted(ridge_models.items())
        }
    artifacts = {
        "schema": ARTIFACT_SCHEMA,
        "model_version": MODEL_VERSION,
        "feature_schema": FEATURE_SCHEMA,
        "training_row_schema": TRAINING_ROW_SCHEMA,
        "code_version": code_version,
        "dataset_sha256": dataset_sha256,
        "configuration": configuration,
        "split": split_dict,
        "candidates": {
            "historical_median": {
                target: {slice_key: model.artifact() for slice_key, model in sorted(models.items())}
                for target, models in sorted(median_models.items())
            },
            "degree_minute_thermal_response": {
                target: {slice_key: model.artifact() for slice_key, model in sorted(models.items())}
                for target, models in sorted(degree_models.items())
            },
            "ridge_regression": ridge_artifacts,
        },
    }
    return report, artifacts


def _write_json(payload: dict[str, Any], path: str | Path, *, stream: TextIO | None = None) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if str(path) == "-":
        if stream is None:
            raise ValueError("a stream is required when writing JSON to stdout")
        stream.write(rendered)
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train and evaluate offline HomeOps thermal-response models."
    )
    parser.add_argument("--input", required=True, help="Validated training-row JSONL path, or '-'.")
    parser.add_argument("--report-out", required=True, help="Evaluation JSON path, or '-'.")
    parser.add_argument("--artifacts-out", required=True, help="Model artifact JSON path.")
    parser.add_argument(
        "--code-version",
        default=os.environ.get("HOMEOPS_CODE_VERSION", "unknown"),
        help="Repository/code identifier stored in reproducibility metadata.",
    )
    parser.add_argument(
        "--split-strategy",
        choices=SPLIT_STRATEGIES,
        default=DEFAULT_SPLIT_STRATEGY,
        help="Evaluation partition strategy; global is the stable default.",
    )
    parser.add_argument(
        "--validation-fraction",
        type=float,
        default=DEFAULT_VALIDATION_FRACTION,
        help="Fraction of chronological rows reserved for validation/calibration.",
    )
    parser.add_argument(
        "--test-fraction",
        type=float,
        default=DEFAULT_TEST_FRACTION,
        help="Fraction of chronological rows reserved for the locked test partition.",
    )
    parser.add_argument(
        "--minimum-eligible-rows",
        type=int,
        default=DEFAULT_MINIMUM_ELIGIBLE_ROWS,
        help="Minimum fit and scoring rows required for an ok result.",
    )
    parser.add_argument(
        "--ridge-alpha",
        type=float,
        default=DEFAULT_RIDGE_ALPHA,
        help="Positive Ridge regularization strength.",
    )
    parser.add_argument(
        "--interval-level",
        type=float,
        default=DEFAULT_INTERVAL_LEVEL,
        help="Empirical training-residual prediction interval level.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.report_out == "-" and args.artifacts_out == "-":
        print("evaluation failed: report and artifacts cannot share stdout", file=sys.stderr)
        return 2
    try:
        dataset = load_dataset(args.input)
        report, artifacts = train_and_evaluate(
            dataset.rows,
            dataset_sha256=dataset.sha256,
            code_version=args.code_version,
            validation_fraction=args.validation_fraction,
            test_fraction=args.test_fraction,
            minimum_eligible_rows=args.minimum_eligible_rows,
            ridge_alpha=args.ridge_alpha,
            interval_level=args.interval_level,
            split_strategy=args.split_strategy,
        )
        _write_json(report, args.report_out, stream=sys.stdout)
        _write_json(artifacts, args.artifacts_out)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"evaluation failed: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "schema": EVALUATION_SCHEMA,
                "rows": report["input"]["rows"],
                "coverage_status": report["coverage_status"],
                "report_out": args.report_out,
                "artifacts_out": args.artifacts_out,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
