"""Tests for the offline thermal baseline trainer/evaluator."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from evaluate_thermal_models import (
    ARTIFACT_SCHEMA,
    EVALUATION_SCHEMA,
    _fit_degree_minute_models,
    _fit_median_models,
    _fit_ridge_model,
    chronological_split,
    load_dataset,
    main,
    train_and_evaluate,
)

BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _row(
    index: int,
    *,
    zone: str = "floor_1",
    mode: str = "heat",
    experiment_id: str | None = None,
    outdoor_temp: float | None = None,
    time_status: str = "eligible",
    runtime_status: str = "eligible",
) -> dict:
    start = BASE + timedelta(hours=index)
    end = start + timedelta(minutes=20 + index)
    crossing = start + timedelta(minutes=10 + index)
    if mode == "heat":
        start_temp, setpoint = 68.0, 70.0
    else:
        start_temp, setpoint = 75.0, 73.0
    gap = abs(setpoint - start_temp)
    outdoor = 25.0 + index if outdoor_temp is None else outdoor_temp
    labels = {
        "time_to_setpoint_s": 600.0 + (index * 30.0) if time_status == "eligible" else None,
        "zone_runtime_s": 1_200.0 + (index * 40.0) if runtime_status == "eligible" else None,
    }
    return {
        "schema": "homeops.thermal.training_row.v1",
        "row_id": f"{zone}:{mode}:{index}",
        "zone": zone,
        "mode": mode,
        "prediction_ts": start.isoformat(),
        "active_start_ts": start.isoformat(),
        "active_end_ts": end.isoformat(),
        "target_crossing_ts": crossing.isoformat(),
        "features": {
            "start_temp_f": start_temp,
            "start_setpoint_f": setpoint,
            "setpoint_delta_f": gap,
            "outdoor_temp_f": outdoor,
            "outdoor_temp_age_s": 60.0,
            "other_zones_calling": [],
            "concurrent_zone_count": 0,
            "start_minute_of_day_local": (index * 60) % 1440,
            "prior_zone_runtime_24h_s": float(index * 10),
            "prior_zone_runtime_history_complete": True,
        },
        "labels": labels,
        "label_status": {
            "time_to_setpoint": time_status,
            "zone_runtime": runtime_status,
        },
        "observations": {
            "end_temp_f": setpoint,
            "observed_duration_s": float((20 + index) * 60),
            "outcome_types": ["target_reached"],
        },
        "quality_flags": [],
        "provenance": {
            "start_boundary": "observed",
            "source_events": [],
            **({"experiment": {"experiment_id": experiment_id}} if experiment_id else {}),
        },
    }


def _group_ids(rows: list[dict]) -> set[str]:
    result = set()
    for row in rows:
        experiment = row.get("provenance", {}).get("experiment", {})
        result.add(experiment.get("experiment_id", row["row_id"]))
    return result


def test_chronological_split_is_ordered_and_keeps_experiments_together():
    rows = [
        _row(0),
        _row(1, experiment_id="experiment-a"),
        _row(2, experiment_id="experiment-a"),
        _row(3),
        _row(4),
        _row(5),
        _row(6),
        _row(7),
        _row(8),
        _row(9),
    ]

    split = chronological_split(rows, validation_fraction=0.2, test_fraction=0.2)

    assert not _group_ids(split.train) & _group_ids(split.validation)
    assert not _group_ids(split.train) & _group_ids(split.test)
    assert not _group_ids(split.validation) & _group_ids(split.test)
    assert split.train[-1]["prediction_ts"] < split.validation[0]["prediction_ts"]
    assert split.validation[-1]["prediction_ts"] < split.test[0]["prediction_ts"]
    assert split.to_dict()["group_isolation"] is True


def test_chronological_split_with_two_groups_keeps_later_group_as_test():
    split = chronological_split([_row(0), _row(1, experiment_id="later")])

    assert len(split.train) == 1
    assert split.validation == []
    assert len(split.test) == 1
    assert split.test[0]["row_id"].endswith(":1")


def test_historical_median_and_degree_minute_fits_are_slice_specific():
    rows = [_row(index) for index in range(5)]
    rows.extend(_row(index, zone="floor_2", mode="cool") for index in range(5, 10))

    median_models = _fit_median_models(
        rows,
        "time_to_setpoint_s",
        minimum_rows=3,
        interval_level=0.8,
    )
    degree_models = _fit_degree_minute_models(
        rows,
        "time_to_setpoint_s",
        minimum_rows=3,
        interval_level=0.8,
    )

    assert set(median_models) == {"floor_1:heat", "floor_2:cool"}
    assert set(degree_models) == {"floor_1:heat", "floor_2:cool"}
    assert median_models["floor_1:heat"].median == 660.0
    assert degree_models["floor_1:heat"].predict(_row(20)) is not None
    assert degree_models["floor_1:heat"].artifact()["kind"] == ("degree_minute_thermal_response")


def test_degree_minute_does_not_predict_without_point_in_time_outdoor_input():
    rows = [_row(index) for index in range(4)]
    model = _fit_degree_minute_models(
        rows,
        "time_to_setpoint_s",
        minimum_rows=3,
        interval_level=0.8,
    )["floor_1:heat"]
    candidate = _row(10, outdoor_temp=None)
    candidate["features"]["outdoor_temp_f"] = None

    assert model.predict(candidate) is None


def test_ridge_fit_is_self_contained_and_serializes_encoder_metadata():
    rows = [
        _row(index, zone=zone, mode=mode)
        for index, (zone, mode) in enumerate(
            [
                ("floor_1", "heat"),
                ("floor_2", "heat"),
                ("floor_3", "heat"),
                ("floor_1", "cool"),
                ("floor_2", "cool"),
                ("floor_3", "cool"),
            ]
        )
    ]

    model = _fit_ridge_model(
        rows,
        "time_to_setpoint_s",
        minimum_rows=3,
        alpha=1.0,
        interval_level=0.8,
    )

    assert model is not None
    assert model.predict(_row(20, zone="floor_2", mode="cool")) is not None
    artifact = model.artifact()
    assert artifact["kind"] == "ridge_regression"
    assert len(artifact["coefficients"]) == len(artifact["encoder"]["feature_names"])
    assert artifact["encoder"]["missing_numeric_values_are_mean_imputed"] is True


def test_train_and_evaluate_emits_all_candidates_and_versioned_artifacts():
    rows = [_row(index) for index in range(15)]

    report, artifacts = train_and_evaluate(
        rows,
        dataset_sha256="abc123",
        code_version="commit-1",
        minimum_eligible_rows=1,
    )

    assert report["schema"] == EVALUATION_SCHEMA
    assert artifacts["schema"] == ARTIFACT_SCHEMA
    assert report["dataset_sha256"] == "abc123"
    assert report["code_version"] == "commit-1"
    assert set(report["candidates"]) == {
        "historical_median",
        "degree_minute_thermal_response",
        "ridge_regression",
    }
    result = report["by_zone_mode"]["floor_1:heat"]["time_to_setpoint_s"]
    assert result["historical_median"]["test"]["predicted_rows"] == 3
    assert result["degree_minute_thermal_response"]["test"]["status"] == "ok"
    assert result["ridge_regression"]["test"]["mae_s"] is not None


def test_evaluation_marks_unrepresented_slices_insufficient_data():
    report, _ = train_and_evaluate(
        [_row(index) for index in range(9)],
        minimum_eligible_rows=1,
    )

    result = report["by_zone_mode"]["floor_3:cool"]["zone_runtime_s"]
    assert result["historical_median"]["fit_status"] == "insufficient_data"
    assert result["historical_median"]["test"]["status"] == "insufficient_data"
    assert result["ridge_regression"]["test"]["status"] == "insufficient_data"
    assert report["coverage_status"] == "insufficient_data"


def test_ineligible_target_is_excluded_without_losing_other_target():
    rows = [_row(index, time_status="right_censored") for index in range(12)]
    report, _ = train_and_evaluate(rows, minimum_eligible_rows=1)

    result = report["by_zone_mode"]["floor_1:heat"]
    assert result["time_to_setpoint_s"]["historical_median"]["fit_status"] == ("insufficient_data")
    assert result["zone_runtime_s"]["historical_median"]["fit_status"] == "ok"


def test_load_dataset_hashes_input_and_rejects_future_feature_values(tmp_path: Path):
    path = tmp_path / "rows.jsonl"
    row = _row(0)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    loaded = load_dataset(path)

    assert len(loaded.rows) == 1
    assert len(loaded.sha256) == 64
    assert loaded.source_lines == 1

    leaked = deepcopy(row)
    leaked["features"]["zone_runtime_s"] = 1.0
    path.write_text(json.dumps(leaked) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="future value"):
        load_dataset(path)


def test_load_dataset_accepts_partial_rows_when_one_target_is_eligible(tmp_path: Path):
    path = tmp_path / "rows.jsonl"
    row = _row(0, time_status="right_censored")
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    loaded = load_dataset(path)

    assert len(loaded.rows) == 1
    assert loaded.rows[0]["label_status"]["time_to_setpoint"] == "right_censored"


def test_load_dataset_rejects_duplicate_rows_and_rows_without_labels(tmp_path: Path):
    path = tmp_path / "rows.jsonl"
    row = _row(0)
    path.write_text("\n".join(json.dumps(value) for value in (row, row)) + "\n")
    with pytest.raises(ValueError, match="duplicate row_id"):
        load_dataset(path)

    no_label = _row(1, time_status="right_censored", runtime_status="right_censored")
    path.write_text(json.dumps(no_label) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no eligible target"):
        load_dataset(path)


def test_cli_persists_report_and_model_artifacts(tmp_path: Path):
    input_path = tmp_path / "rows.jsonl"
    report_path = tmp_path / "out" / "report.json"
    artifacts_path = tmp_path / "out" / "artifacts.json"
    input_path.write_text(
        "".join(json.dumps(_row(index)) + "\n" for index in range(12)),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--input",
            str(input_path),
            "--report-out",
            str(report_path),
            "--artifacts-out",
            str(artifacts_path),
            "--code-version",
            "test-version",
            "--minimum-eligible-rows",
            "1",
        ]
    )

    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    artifacts = json.loads(artifacts_path.read_text(encoding="utf-8"))
    assert report["schema"] == EVALUATION_SCHEMA
    assert artifacts["schema"] == ARTIFACT_SCHEMA
    assert report["code_version"] == "test-version"


def test_cli_does_not_allow_two_json_outputs_on_stdout():
    assert main(["--input", "-", "--report-out", "-", "--artifacts-out", "-"]) == 2
