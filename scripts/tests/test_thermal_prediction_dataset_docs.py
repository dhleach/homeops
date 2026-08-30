"""Regression checks for the normalized thermal training-row export contract.

Revision history:
  2026-08-29  Add documentation coverage for the JSONL row schema, provenance,
              leakage boundary, and incomplete-session behavior.
"""

from pathlib import Path

DATASET_DOC = Path(__file__).parents[2] / "docs" / "thermal-prediction-dataset.md"


def test_dataset_contract_defines_row_shape_and_labels():
    text = DATASET_DOC.read_text(encoding="utf-8")

    for required in (
        "homeops.thermal.training_row.v1",
        "features",
        "labels",
        "label_status",
        "provenance",
        "time_to_setpoint_s",
        "zone_runtime_s",
        "right-censored",
        "missing_start_boundary",
        "source_events",
        "prior_zone_runtime_history_complete",
    ):
        assert required in text


def test_dataset_contract_preserves_modes_and_prevents_leakage():
    text = DATASET_DOC.read_text(encoding="utf-8")

    for required in (
        "Heating rows",
        "Cooling rows",
        "never relabeled as cooling",
        "No cooling row is synthesized",
        "features",
        "Session end",
        "target crossing",
        "total duration",
        "operation_type",
        "--observer-log",
        "--derived-log",
        "byte-for-byte",
        "identical JSONL",
    ):
        assert required in text
