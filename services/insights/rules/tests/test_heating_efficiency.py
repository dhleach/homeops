"""Tests for the HeatingEfficiencyRule."""

from __future__ import annotations

import pytest
from rules.heating_efficiency import HeatingEfficiencyRule

_SCHEMA = "homeops.consumer.heating_session_ended.v1"


def _session(
    floor: str,
    duration_s: float,
    temp_delta_f: float,
    date: str = "2026-04-10",
) -> dict:
    """Build a minimal heating_session_ended event with temp delta."""
    return {
        "schema": _SCHEMA,
        "ts": f"{date}T12:00:00+00:00",
        "data": {
            "floor": floor,
            "duration_s": duration_s,
            "temp_delta_f": temp_delta_f,
        },
    }


def _make_sessions(floor: str, n: int, duration_s: float, temp_delta_f: float) -> list[dict]:
    return [
        _session(floor, duration_s, temp_delta_f, date=f"2026-04-{i + 1:02d}") for i in range(n)
    ]


# ---------------------------------------------------------------------------
# HeatingEfficiencyRule.check()
# ---------------------------------------------------------------------------


class TestHeatingEfficiencyRuleCheck:
    def test_basic_score_calculation(self):
        """Single floor with known values → verify score formula."""
        # 600s = 10 min, 2°F gained → score = 2/10 = 0.2 °F/min
        history = _make_sessions("floor_1", n=5, duration_s=600.0, temp_delta_f=2.0)
        rule = HeatingEfficiencyRule(
            history=history, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        results = rule.check()
        assert len(results) == 1
        assert results[0]["data"]["floor"] == "floor_1"
        assert results[0]["data"]["score_f_per_min"] == pytest.approx(0.2, abs=0.01)

    def test_multiple_floors_sorted_ascending(self):
        """Multiple floors → sorted least efficient first."""
        history = (
            _make_sessions("floor_1", 5, duration_s=600.0, temp_delta_f=4.0)  # 0.4 °F/min (better)
            + _make_sessions("floor_2", 5, duration_s=600.0, temp_delta_f=1.0)  # 0.1 °F/min (worse)
            + _make_sessions(
                "floor_3", 5, duration_s=600.0, temp_delta_f=2.0
            )  # 0.2 °F/min (middle)
        )
        rule = HeatingEfficiencyRule(
            history=history, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        results = rule.check()
        assert len(results) == 3
        scores = [r["data"]["score_f_per_min"] for r in results]
        assert scores == sorted(scores), "results should be sorted ascending"
        assert results[0]["data"]["floor"] == "floor_2"
        assert results[-1]["data"]["floor"] == "floor_1"

    def test_insufficient_sessions_skipped(self):
        """Fewer than min_sessions → floor excluded from results."""
        history = _make_sessions("floor_1", n=3, duration_s=600.0, temp_delta_f=2.0)  # only 3
        rule = HeatingEfficiencyRule(
            history=history, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        assert rule.check() == []

    def test_zero_or_negative_temp_delta_excluded(self):
        """Sessions with temp_delta_f <= 0 are excluded."""
        bad = [_session("floor_1", 600.0, 0.0), _session("floor_1", 600.0, -1.0)]
        good = _make_sessions("floor_1", 5, duration_s=600.0, temp_delta_f=2.0)
        rule = HeatingEfficiencyRule(
            history=bad + good, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        results = rule.check()
        # Only the 5 good sessions count
        assert len(results) == 1
        assert results[0]["data"]["session_count"] == 5

    def test_short_sessions_excluded(self):
        """Sessions shorter than min_duration_s are excluded."""
        short = [_session("floor_1", 30.0, 2.0) for _ in range(3)]
        good = _make_sessions("floor_1", 5, duration_s=600.0, temp_delta_f=2.0)
        rule = HeatingEfficiencyRule(
            history=short + good, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        results = rule.check()
        assert results[0]["data"]["session_count"] == 5

    def test_missing_temp_delta_excluded(self):
        """Sessions without temp_delta_f are skipped."""
        no_delta = [
            {
                "schema": _SCHEMA,
                "ts": "2026-04-01T12:00:00+00:00",
                "data": {"floor": "floor_1", "duration_s": 600.0},
            }
            for _ in range(3)
        ]
        good = _make_sessions("floor_1", 5, 600.0, 2.0)
        rule = HeatingEfficiencyRule(
            history=no_delta + good, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        assert rule.check()[0]["data"]["session_count"] == 5

    def test_empty_history(self):
        rule = HeatingEfficiencyRule(
            history=[], min_sessions=5, min_duration_s=60, lookback_days=None
        )
        assert rule.check() == []

    def test_ignores_non_session_schemas(self):
        noise = [
            {
                "schema": "homeops.consumer.furnace_daily_summary.v1",
                "ts": "2026-04-01T12:00:00+00:00",
                "data": {},
            }
        ]
        rule = HeatingEfficiencyRule(
            history=noise, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        assert rule.check() == []

    def test_schema_field(self):
        history = _make_sessions("floor_1", 5, 600.0, 2.0)
        rule = HeatingEfficiencyRule(
            history=history, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        results = rule.check()
        assert results[0]["schema"] == "homeops.insights.heating_efficiency.v1"

    def test_finding_fields_present(self):
        history = _make_sessions("floor_1", 5, 600.0, 2.0)
        rule = HeatingEfficiencyRule(
            history=history, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        results = rule.check()
        d = results[0]["data"]
        for key in (
            "floor",
            "label",
            "score_f_per_min",
            "session_count",
            "total_runtime_min",
            "total_temp_gain_f",
            "lookback_days",
        ):
            assert key in d, f"missing key {key!r}"


# ---------------------------------------------------------------------------
# HeatingEfficiencyRule.summary_text()
# ---------------------------------------------------------------------------


class TestHeatingEfficiencyRuleSummaryText:
    def test_returns_empty_when_no_data(self):
        rule = HeatingEfficiencyRule(
            history=[], min_sessions=5, min_duration_s=60, lookback_days=None
        )
        assert rule.summary_text() == ""

    def test_returns_string_with_data(self):
        history = _make_sessions("floor_1", 5, 600.0, 2.0)
        rule = HeatingEfficiencyRule(
            history=history, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        text = rule.summary_text()
        assert isinstance(text, str)
        assert "Floor 1" in text
        assert "°F/min" in text

    def test_multiple_floors_includes_best_worst(self):
        history = _make_sessions("floor_1", 5, 600.0, 4.0) + _make_sessions(
            "floor_2", 5, 600.0, 1.0
        )
        rule = HeatingEfficiencyRule(
            history=history, min_sessions=5, min_duration_s=60, lookback_days=None
        )
        text = rule.summary_text()
        assert "Least efficient" in text
        assert "Most efficient" in text
