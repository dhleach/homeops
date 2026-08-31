"""Tests for normalized thermal-row validation and quarantine."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from validate_thermal_dataset import (
    QUARANTINE_SCHEMA,
    TRAINING_ROW_SCHEMA,
    main,
    validate_jsonl,
    validate_row,
    validate_rows,
)


def _timestamp(minutes: int) -> str:
    return (datetime(2024, 1, 15, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()


def _row(mode: str = "heat", *, start_minute: int = 0, end_minute: int = 7) -> dict:
    if mode == "heat":
        start_temp, setpoint, delta = 69.0, 70.0, 1.0
        source_schema = "homeops.consumer.zone_time_to_temp.v1"
        end_temp = 70.2
    else:
        start_temp, setpoint, delta = 75.0, 73.0, 2.0
        source_schema = "homeops.consumer.thermostat_cooling_session_started.v1"
        end_temp = 72.8
    start = _timestamp(start_minute)
    target = _timestamp(start_minute + 5)
    end = _timestamp(end_minute)
    return {
        "schema": TRAINING_ROW_SCHEMA,
        "row_id": f"floor_1:{mode}:{start}",
        "zone": "floor_1",
        "mode": mode,
        "prediction_ts": start,
        "active_start_ts": start,
        "active_end_ts": end,
        "target_crossing_ts": target,
        "features": {
            "start_temp_f": start_temp,
            "start_setpoint_f": setpoint,
            "setpoint_delta_f": delta,
            "outdoor_temp_f": 30.0,
            "outdoor_temp_age_s": 60.0,
            "other_zones_calling": [],
            "concurrent_zone_count": 0,
            "start_minute_of_day_local": 600 + start_minute,
            "prior_zone_runtime_24h_s": None,
            "prior_zone_runtime_history_complete": False,
        },
        "labels": {
            "time_to_setpoint_s": 300.0,
            "zone_runtime_s": float((end_minute - start_minute) * 60),
        },
        "label_status": {
            "time_to_setpoint": "eligible",
            "zone_runtime": "eligible",
        },
        "observations": {
            "end_temp_f": end_temp,
            "observed_duration_s": float((end_minute - start_minute) * 60),
            "outcome_types": ["target_reached"],
        },
        "quality_flags": [],
        "provenance": {
            "start_boundary": "observed",
            "source_events": [
                {
                    "source": "observer",
                    "line": 1,
                    "schema": "homeops.observer.state_changed.v1",
                    "event_id": "observer-1",
                    "timestamp": start,
                },
                {
                    "source": "derived",
                    "line": 2,
                    "schema": source_schema,
                    "event_id": "derived-1",
                    "timestamp": target,
                },
            ],
        },
    }


def _codes(row: dict) -> set[str]:
    return set(validate_row(row))


def test_valid_heat_and_cooling_rows_pass_unchanged():
    rows = [_row("heat"), _row("cool", start_minute=20, end_minute=27)]
    original = deepcopy(rows)

    result = validate_rows(rows)

    assert result.valid_rows == original
    assert result.quarantined_rows == []
    assert result.report["status"] == "ok"


def test_directional_temperature_mismatch_is_quarantined():
    row = _row("cool")
    row["features"]["start_temp_f"] = 72.0
    row["features"]["start_setpoint_f"] = 73.0
    row["features"]["setpoint_delta_f"] = -1.0

    result = validate_rows([row])

    assert not result.valid_rows
    assert "invalid_direction" in result.quarantined_rows[0]["reason_codes"]


def test_missing_start_boundary_is_quarantined_without_reconstruction():
    row = _row()
    row["prediction_ts"] = None
    row["active_start_ts"] = None
    row["target_crossing_ts"] = None
    row["label_status"] = {
        "time_to_setpoint": "missing_start_boundary",
        "zone_runtime": "missing_start_boundary",
    }
    row["labels"] = {"time_to_setpoint_s": None, "zone_runtime_s": None}
    row["provenance"]["start_boundary"] = "missing"

    result = validate_rows([row])

    quarantine = result.quarantined_rows[0]
    assert "missing_start_boundary" in quarantine["reason_codes"]
    assert quarantine["row"]["prediction_ts"] is None


def test_impossible_timestamp_and_duration_are_quarantined():
    row = _row(end_minute=-1)
    row["labels"]["zone_runtime_s"] = -60.0

    codes = _codes(row)

    assert "impossible_duration" in codes
    assert "impossible_timestamp_order" in codes or "target_after_session_end" in codes


def test_stale_outdoor_input_is_quarantined():
    row = _row()
    row["features"]["outdoor_temp_age_s"] = 10_801.0

    result = validate_rows([row])

    assert "stale_outdoor_input" in result.quarantined_rows[0]["reason_codes"]


def test_missing_action_evidence_is_reported_when_explicit_action_is_empty():
    row = _row()
    row["active_action"] = None

    result = validate_rows([row])

    assert "missing_active_action" in result.quarantined_rows[0]["reason_codes"]


def test_heat_row_with_cooling_source_is_quarantined():
    row = _row("heat")
    row["provenance"]["source_events"][1]["schema"] = (
        "homeops.consumer.thermostat_cooling_session_started.v1"
    )

    result = validate_rows([row])

    assert "heating_cooling_source_mismatch" in result.quarantined_rows[0]["reason_codes"]


def test_duplicate_row_ids_quarantine_all_ambiguous_records():
    first = _row()
    second = deepcopy(first)

    result = validate_rows([first, second])

    assert len(result.quarantined_rows) == 2
    assert all("duplicate_row_id" in row["reason_codes"] for row in result.quarantined_rows)
    assert result.report["reason_counts"]["duplicate_row_id"] == 2


def test_overlapping_sessions_quarantine_both_records():
    first = _row(end_minute=10)
    second = _row(start_minute=5, end_minute=12)
    second["row_id"] = "floor_1:heat:overlapping"

    result = validate_rows([first, second])

    assert len(result.quarantined_rows) == 2
    assert all("overlapping_session" in row["reason_codes"] for row in result.quarantined_rows)


def test_overlapping_sessions_across_modes_quarantine_both_records():
    heat = _row("heat")
    cool = _row("cool")

    result = validate_rows([heat, cool])

    assert len(result.quarantined_rows) == 2
    assert all("overlapping_session" in row["reason_codes"] for row in result.quarantined_rows)


def test_censored_row_with_time_target_remains_valid_for_that_target():
    row = _row()
    row["active_end_ts"] = None
    row["labels"]["zone_runtime_s"] = None
    row["label_status"]["zone_runtime"] = "right_censored"
    row["observations"]["observed_duration_s"] = None
    row["quality_flags"] = ["missing_end_boundary"]

    result = validate_rows([row])

    assert result.valid_rows == [row]
    assert result.quarantined_rows == []
    assert result.report["valid_row_warning_counts"] == {"missing_end_boundary": 1}


def test_feature_target_leakage_is_quarantined():
    row = _row()
    row["features"]["zone_runtime_s"] = 420.0

    result = validate_rows([row])

    assert "feature_target_leakage" in result.quarantined_rows[0]["reason_codes"]


def test_invalid_experiment_metadata_is_quarantined():
    row = _row()
    row["provenance"]["experiment"] = "not-an-object"

    result = validate_rows([row])

    assert "invalid_experiment_metadata" in result.quarantined_rows[0]["reason_codes"]


def test_malformed_json_is_quarantined_with_original_line():
    valid_line = json.dumps(_row())
    result = validate_jsonl(StringIO(f"{valid_line}\n{{not-json}}\n"))

    assert len(result.valid_rows) == 1
    assert len(result.quarantined_rows) == 1
    quarantine = result.quarantined_rows[0]
    assert quarantine["schema"] == QUARANTINE_SCHEMA
    assert quarantine["source_line"] == 2
    assert quarantine["reason_codes"] == ["malformed_json"]
    assert quarantine["raw_line"] == "{not-json}"


def test_coverage_report_marks_sparse_floor_mode_slices_insufficient():
    result = validate_rows([_row()], minimum_eligible_rows=2)

    assert result.report["coverage_status"] == "insufficient_data"
    assert result.report["by_zone_mode"]["floor_1:heat"]["time_to_setpoint_status"] == (
        "insufficient_data"
    )
    assert result.report["by_zone_mode"]["floor_1:heat"]["eligible_time_to_setpoint"] == 1


def test_cli_writes_separate_valid_quarantine_and_report_outputs(tmp_path: Path):
    input_path = tmp_path / "input.jsonl"
    valid_path = tmp_path / "valid.jsonl"
    quarantine_path = tmp_path / "quarantine.jsonl"
    report_path = tmp_path / "report.json"
    input_path.write_text(
        json.dumps(_row()) + "\n" + json.dumps({"schema": "wrong"}) + "\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--input",
            str(input_path),
            "--valid-out",
            str(valid_path),
            "--quarantine-out",
            str(quarantine_path),
            "--report-out",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert len(valid_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(quarantine_path.read_text(encoding="utf-8").splitlines()) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["valid_rows"] == 1
    assert report["quarantined_rows"] == 1


def test_non_object_rows_are_quarantined():
    result = validate_jsonl(StringIO("[]\n"))

    assert result.quarantined_rows[0]["reason_codes"] == ["non_object_row"]
