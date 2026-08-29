"""Regression checks for the thermal-prediction evaluation contract.

Revision history:
  2026-08-29  Add a documentation contract test for the selected baseline
              ladder, chronological evaluation, uncertainty metrics, and the
              future cross-zone thermal-model boundary.
"""

from pathlib import Path

EVALUATION_DOC = Path(__file__).parents[2] / "docs" / "thermal-prediction-evaluation.md"


def test_evaluation_contract_selects_model_ladder():
    text = EVALUATION_DOC.read_text(encoding="utf-8")

    for required in (
        "historical-median reference baseline",
        "degree-minute/thermal-response baseline",
        "regularized linear regression (Ridge)",
        "time_to_setpoint_s",
        "zone_runtime_s",
        "neural network or an LLM",
        "scripts/time_to_temp.py",
    ):
        assert required in text


def test_evaluation_contract_defines_numeric_and_uncertainty_metrics():
    text = EVALUATION_DOC.read_text(encoding="utf-8")

    for required in (
        "MAE",
        "P95 absolute error",
        "Signed bias",
        "Interval coverage",
        "Interval width",
        "Eligible sample count",
        "80% prediction interval",
        "false-recommendation rate",
        "not applicable",
        "rather than",
        "zero",
    ):
        assert required in text


def test_evaluation_contract_preserves_point_in_time_and_time_aware_rules():
    text = EVALUATION_DOC.read_text(encoding="utf-8")

    for required in (
        "at or before prediction_ts",
        "current session's end",
        "Feature encoders, scalers, coefficients",
        "chronological",
        "walk-forward/expanding-window",
        "Do not randomly shuffle",
        "same session, overlapping interval",
        "insufficient_data",
    ):
        assert required in text


def test_evaluation_contract_reserves_cross_zone_what_if_scope_without_control():
    text = EVALUATION_DOC.read_text(encoding="utf-8")

    for required in (
        "point-in-time concurrent-zone state",
        "deliberate-intervention provenance",
        "held-out experiments",
        "what-if query",
        "read-only",
        "thermostat-control policy",
        "kilowatt-hour savings",
    ):
        assert required in text
