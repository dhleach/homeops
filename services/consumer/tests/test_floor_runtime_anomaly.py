"""Tests for FloorRuntimeAnomalyRule."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure insights/rules is importable from within the consumer tests.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "insights"))

from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule  # noqa: E402

_ANOMALY_SCHEMA = "homeops.consumer.floor_runtime_anomaly.v1"
_TODAY = "2026-03-28"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_summary(date: str, floor_1: int, floor_2: int, floor_3: int) -> dict:
    return {
        "schema": "homeops.consumer.furnace_daily_summary.v1",
        "ts": "2026-01-01T00:00:00+00:00",
        "data": {
            "date": date,
            "per_floor_runtime_s": {
                "floor_1": floor_1,
                "floor_2": floor_2,
                "floor_3": floor_3,
            },
        },
    }


def _build_history(n: int, floor_2_runtime: int = 3600) -> list[dict]:
    """Build n daily summary events with stable runtimes."""
    return [make_summary(f"2026-03-{i + 1:02d}", 1800, floor_2_runtime, 900) for i in range(n)]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestFloorRuntimeAnomalyRule:
    # 1. No anomaly when runtime is at or below mean * threshold
    def test_no_anomaly_below_threshold(self):
        history = _build_history(10, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history)
        # mean = 3600, threshold = 5400; runtime exactly at threshold → no anomaly
        events = rule.check_daily_runtime("floor_2", 5400, _TODAY)
        assert events == []

    def test_no_anomaly_well_below_threshold(self):
        history = _build_history(10, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history)
        events = rule.check_daily_runtime("floor_2", 3000, _TODAY)
        assert events == []

    # 2. Anomaly fires when runtime > mean * 1.5
    def test_anomaly_fires_above_threshold(self):
        history = _build_history(10, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history)
        # mean = 3600, threshold = 5400; 5401 > 5400 → anomaly
        events = rule.check_daily_runtime("floor_2", 5401, _TODAY)
        assert len(events) == 1
        assert events[0]["schema"] == _ANOMALY_SCHEMA

    # 3. No anomaly with fewer than 3 history points
    def test_insufficient_history_two_points(self):
        history = _build_history(2)
        rule = FloorRuntimeAnomalyRule(history)
        events = rule.check_daily_runtime("floor_2", 9999, _TODAY)
        assert events == []

    def test_insufficient_history_one_point(self):
        history = _build_history(1)
        rule = FloorRuntimeAnomalyRule(history)
        events = rule.check_daily_runtime("floor_2", 9999, _TODAY)
        assert events == []

    def test_exactly_three_points_fires(self):
        history = _build_history(3, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history)
        events = rule.check_daily_runtime("floor_2", 9000, _TODAY)
        assert len(events) == 1

    # 4. No anomaly when mean_s < 300 (low-use guard)
    def test_low_use_guard_no_anomaly(self):
        # Build history where floor_3 always runs only 100 s → mean = 100 < 300 → guard fires
        history = [make_summary(f"2026-03-{i + 1:02d}", 1800, 3600, 100) for i in range(10)]
        rule = FloorRuntimeAnomalyRule(history)
        events = rule.check_daily_runtime("floor_3", 9999, _TODAY)
        assert events == []

    def test_low_use_guard_boundary_exactly_300(self):
        # mean exactly 300 → guard requires mean < 300, so should NOT guard
        history = [make_summary(f"2026-03-{i + 1:02d}", 300, 300, 300) for i in range(5)]
        rule = FloorRuntimeAnomalyRule(history)
        # runtime just above threshold (300 * 1.5 = 450) → should fire
        events = rule.check_daily_runtime("floor_1", 451, _TODAY)
        assert len(events) == 1

    # 5. Custom threshold_multiplier (e.g. 2.0)
    def test_custom_threshold_multiplier(self):
        history = _build_history(10, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history, threshold_multiplier=2.0)
        # mean = 3600, threshold = 7200; 6000 < 7200 → no anomaly
        events = rule.check_daily_runtime("floor_2", 6000, _TODAY)
        assert events == []

    def test_custom_threshold_multiplier_fires(self):
        history = _build_history(10, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history, threshold_multiplier=2.0)
        # 7201 > 7200 → anomaly
        events = rule.check_daily_runtime("floor_2", 7201, _TODAY)
        assert len(events) == 1
        assert events[0]["data"]["threshold_multiplier"] == 2.0

    # 6. Multiple floors: one anomalous, one normal → correct events returned
    def test_multiple_floors_one_anomalous(self):
        history = _build_history(10, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history)

        # floor_1 mean = 1800, floor_2 mean = 3600
        results = []
        for floor, runtime in [("floor_1", 1900), ("floor_2", 7300)]:
            results.extend(rule.check_daily_runtime(floor, runtime, _TODAY))

        assert len(results) == 1
        assert results[0]["data"]["floor"] == "floor_2"

    # 7. Correct schema and payload fields in returned event
    def test_event_payload_fields(self):
        history = _build_history(10, floor_2_runtime=3600)
        rule = FloorRuntimeAnomalyRule(history)
        events = rule.check_daily_runtime("floor_2", 7200, _TODAY)
        assert len(events) == 1
        evt = events[0]
        assert evt["schema"] == _ANOMALY_SCHEMA
        assert evt["source"] == "consumer.v1"
        assert "ts" in evt
        d = evt["data"]
        assert d["floor"] == "floor_2"
        assert d["runtime_s"] == 7200
        assert d["baseline_mean_s"] == 3600.0
        assert d["threshold_multiplier"] == 1.5
        assert d["threshold_s"] == 3600.0 * 1.5
        assert d["lookback_days"] == 14
        assert d["history_count"] == 10
        assert d["date"] == _TODAY

    # 8. Empty history → no anomaly
    def test_empty_history(self):
        rule = FloorRuntimeAnomalyRule([])
        events = rule.check_daily_runtime("floor_2", 9999, _TODAY)
        assert events == []

    # 9. lookback_days slices history correctly (only last N days used)
    def test_lookback_days_slicing(self):
        # 20 days of history: first 10 have floor_2=500, last 10 have floor_2=3600
        old = [make_summary(f"2026-02-{i + 1:02d}", 1800, 500, 900) for i in range(10)]
        recent = [make_summary(f"2026-03-{i + 1:02d}", 1800, 3600, 900) for i in range(10)]
        history = old + recent

        # lookback_days=10 → only the recent 10 days → mean=3600
        rule = FloorRuntimeAnomalyRule(history, lookback_days=10)
        # 5401 > 3600*1.5=5400 → should fire
        events = rule.check_daily_runtime("floor_2", 5401, _TODAY)
        assert len(events) == 1
        assert events[0]["data"]["history_count"] == 10

        # lookback_days=20 → all 20 days → mean=(500*10+3600*10)/20=2050
        # threshold=2050*1.5=3075; 5401 > 3075 → still fires but history_count=20
        rule20 = FloorRuntimeAnomalyRule(history, lookback_days=20)
        events20 = rule20.check_daily_runtime("floor_2", 5401, _TODAY)
        assert len(events20) == 1
        assert events20[0]["data"]["history_count"] == 20

    def test_lookback_excludes_today(self):
        # Put today's date in history to confirm it's excluded from baseline.
        history = _build_history(10, floor_2_runtime=3600)
        # Inject an entry with today's date and an extreme runtime that would inflate mean.
        history.append(make_summary(_TODAY, 1800, 99999, 900))
        rule = FloorRuntimeAnomalyRule(history)
        # If today was included: mean ≈ (3600*10 + 99999) / 11 ≈ 12363 → threshold ≈ 18545
        # If today excluded: mean = 3600 → threshold = 5400; 5401 fires
        events = rule.check_daily_runtime("floor_2", 5401, _TODAY)
        assert len(events) == 1
        # history_count must not include today
        assert events[0]["data"]["history_count"] == 10
