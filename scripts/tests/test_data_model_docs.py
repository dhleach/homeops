"""Regression test for the documented floor-call data model.

Revision history:
  2026-08-22  Added a lightweight contract check so required session and
              aggregate fields cannot disappear from the canonical document.
"""

from pathlib import Path

DATA_MODEL = Path(__file__).parents[2] / "docs" / "data-model.md"


def test_floor_call_data_model_contains_required_contract_fields():
    text = DATA_MODEL.read_text(encoding="utf-8")

    for model_name in ("FloorCallSession", "FloorStats"):
        assert model_name in text
    for field in (
        "floor_id",
        "session_start_ts",
        "session_end_ts",
        "duration_s",
        "window_start",
        "window_end",
        "call_count",
        "total_runtime_s",
        "avg_duration_s",
        "min_duration_s",
        "max_duration_s",
    ):
        assert f"`{field}`" in text
