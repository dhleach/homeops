"""Regression checks for the canonical thermal-prediction target contract.

Revision history:
  2026-08-27  Add a documentation contract test so target boundaries, units,
              mode availability, and incomplete-session handling cannot drift
              while the later ML pipeline is implemented.
"""

from pathlib import Path

TARGETS_DOC = Path(__file__).parents[2] / "docs" / "thermal-prediction-targets.md"


def test_target_contract_defines_boundaries_units_and_directional_labels():
    text = TARGETS_DOC.read_text(encoding="utf-8")

    for required in (
        "active_start_ts",
        "active_end_ts",
        "prediction_ts",
        "start_temp_f",
        "start_setpoint_f",
        "target_crossing_ts",
        "time_to_setpoint_s",
        "zone_runtime_s",
        "ISO 8601 UTC",
        "elapsed wall-clock time in seconds",
        "floor_1",
        "floor_2",
        "floor_3",
        "`heat`",
        "`cool`",
        "current_temp_f >= start_setpoint_f",
        "current_temp_f <= start_setpoint_f",
    ):
        assert required in text


def test_target_contract_defines_censoring_and_cooling_boundary():
    text = TARGETS_DOC.read_text(encoding="utf-8")

    for required in (
        "Right-censored",
        "missing_start_boundary",
        "missing_end_boundary",
        "invalid_measurement",
        "already_at_target",
        "consumer restart",
        "Setpoint changes before crossing",
        "zero eligible rows",
        "must not be presented",
        "ordinary regression",
        "homeops.consumer.zone_time_to_temp.v1",
    ):
        assert required in text
