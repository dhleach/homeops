"""Regression checks for the canonical thermal-prediction feature schema.

Revision history:
  2026-08-27  Add a documentation contract test so feature sources, point-in-time
              availability, null behavior, heat/cool symmetry, and leakage rules
              remain explicit while the training pipeline is built.
  2026-08-29  Update the contract assertions for the normalized cooling export.
"""

from pathlib import Path

FEATURES_DOC = Path(__file__).parents[2] / "docs" / "thermal-prediction-features.md"


def test_feature_contract_names_sources_timing_and_missing_policies():
    text = FEATURES_DOC.read_text(encoding="utf-8")

    for required in (
        "homeops.thermal.features.v1",
        "prediction_ts",
        "start_temp_f",
        "start_setpoint_f",
        "setpoint_delta_f",
        "outdoor_temp_f",
        "outdoor_temp_age_s",
        "other_zones_calling",
        "concurrent_zone_count",
        "start_minute_of_day_local",
        "prior_zone_runtime_24h_s",
        "prior_zone_runtime_history_complete",
        "indoor_humidity_pct",
        "occupancy_state",
        "weather_humidity_pct",
        "weather_wind_speed_mph",
        "weather_cloud_cover_pct",
        "Source",
        "Timing rule",
        "Missing / null policy",
        "Leakage status",
    ):
        assert required in text


def test_feature_contract_is_point_in_time_and_excludes_targets():
    text = FEATURES_DOC.read_text(encoding="utf-8")

    for required in (
        "source event timestamp",
        "`<= prediction_ts`",
        "`active_start_ts`",
        "Latest valid sample at or before `prediction_ts`",
        "The current session is excluded",
        "active_end_ts",
        "target_crossing_ts",
        "time_to_setpoint_s",
        "zone_runtime_s",
        "post-start climate reading",
        "future outdoor/weather/occupancy readings",
        "final setpoint or final mode",
        "invented zero",
    ):
        assert required in text


def test_feature_contract_uses_one_schema_for_heat_and_cool():
    text = FEATURES_DOC.read_text(encoding="utf-8")

    for required in (
        "enum: `heat` or `cool`",
        "heat = setpoint − current, cool = current − setpoint",
        "same field names, types, null rules, and pipeline",
        "heating-call sensors as cooling calls",
        "explicitly instrumented cooling",
        "Heating history must never be relabeled as cooling history",
    ):
        assert required in text
