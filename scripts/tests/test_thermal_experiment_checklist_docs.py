"""Regression checks for the operator-facing thermal experiment protocol."""

from __future__ import annotations

from pathlib import Path

CHECKLIST_DOC = Path(__file__).parents[2] / "docs" / "thermal-experiment-checklist.md"


def test_checklist_doc_defines_the_six_canonical_configurations():
    text = CHECKLIST_DOC.read_text(encoding="utf-8")

    for required in (
        "cool-s1-f1",
        "cool-s1-f2",
        "cool-s1-f3",
        "cool-p12",
        "cool-p13",
        "cool-p23",
        "30 minutes of ordinary operation",
        "30 minutes of intervention",
        "30 minutes of recovery",
    ):
        assert required in text


def test_checklist_doc_defines_repeats_and_data_only_boundary():
    text = CHECKLIST_DOC.read_text(encoding="utf-8")

    for required in (
        "Repeated runs",
        "new `experiment_id`",
        "overlap",
        "homeops.thermal.experiment_marker.v1",
        "The experiment ID is provenance and",
        "grouping metadata",
        "never calls Home Assistant",
        "checklist",
    ):
        assert required in text
