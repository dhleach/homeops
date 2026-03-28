"""Tests for FurnaceSessionAnomalyRule (session duration anomaly detection)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "insights"))

from rules.furnace_session_anomaly import (
    SHORT_SESSION_THRESHOLD_S,
    FurnaceSessionAnomalyRule,
)

TS = "2024-01-15T10:00:00+00:00"

# A baseline with realistic p95 values.
_BASELINE = {
    "floor_1": {"count": 100, "min": 120, "max": 2400, "median": 600, "p75": 900, "p95": 1200.0},
    "floor_2": {"count": 100, "min": 180, "max": 3600, "median": 900, "p75": 1200, "p95": 1800.0},
    "floor_3": {"count": 100, "min": 60, "max": 1800, "median": 400, "p75": 700, "p95": 800.0},
}


class TestShortSessionWarning:
    def test_short_session_fires_warning(self):
        """duration_s < 90 → short session warning emitted."""
        rule = FurnaceSessionAnomalyRule(baseline=_BASELINE)
        results = rule.check_session("floor_1", 45, TS)

        assert len(results) == 1
        evt = results[0]
        assert evt["schema"] == "homeops.consumer.heating_short_session_warning.v1"
        assert evt["data"]["floor"] == "floor_1"
        assert evt["data"]["duration_s"] == 45
        assert evt["data"]["threshold_s"] == SHORT_SESSION_THRESHOLD_S
        assert evt["data"]["likely_cause"] == "short_cycle"

    def test_exactly_at_threshold_is_not_short(self):
        """duration_s == SHORT_SESSION_THRESHOLD_S → no short warning (boundary)."""
        rule = FurnaceSessionAnomalyRule()
        results = rule.check_session("floor_1", SHORT_SESSION_THRESHOLD_S, TS)
        schemas = [r["schema"] for r in results]
        assert "homeops.consumer.heating_short_session_warning.v1" not in schemas

    def test_short_session_no_baseline(self):
        """Short session fires even with no baseline."""
        rule = FurnaceSessionAnomalyRule(baseline={})
        results = rule.check_session("floor_2", 30, TS)
        assert len(results) == 1
        assert results[0]["schema"] == "homeops.consumer.heating_short_session_warning.v1"


class TestNormalSession:
    def test_normal_session_fires_nothing(self):
        """A session of normal duration → no events emitted."""
        rule = FurnaceSessionAnomalyRule(baseline=_BASELINE)
        # floor_1 p95=1200s → long threshold = max(1800, 1800) = 1800s
        # 800s is well within normal range
        results = rule.check_session("floor_1", 800, TS)
        assert results == []

    def test_normal_session_no_baseline(self):
        """Normal duration with no baseline → no events (uses abs fallback)."""
        rule = FurnaceSessionAnomalyRule()
        # floor_1 fallback = 1800s; 900s is normal
        results = rule.check_session("floor_1", 900, TS)
        assert results == []


class TestLongSessionWarning:
    def test_long_session_fires_warning_with_baseline(self):
        """duration_s > p95 × 1.5 → long session warning with baseline p95."""
        rule = FurnaceSessionAnomalyRule(baseline=_BASELINE)
        # floor_1 p95=1200 → threshold = max(1800, 1800) = 1800; use 1900
        results = rule.check_session("floor_1", 1900, TS)

        assert len(results) == 1
        evt = results[0]
        assert evt["schema"] == "homeops.consumer.heating_long_session_warning.v1"
        assert evt["data"]["floor"] == "floor_1"
        assert evt["data"]["duration_s"] == 1900
        assert evt["data"]["baseline_p95_s"] == 1200.0
        assert evt["data"]["likely_cause"] == "overheating_risk"

    def test_long_session_threshold_is_max_of_p95x15_and_fallback(self):
        """Threshold = max(p95 × 1.5, abs fallback); floor_2 p95=1800 → threshold=2700."""
        rule = FurnaceSessionAnomalyRule(baseline=_BASELINE)
        # floor_2: p95=1800 → p95×1.5=2700; abs fallback=2700 → threshold=2700
        # 2700 is NOT > 2700, so should not fire
        results = rule.check_session("floor_2", 2700, TS)
        assert results == []

        # 2701 is > 2700 → fires
        results = rule.check_session("floor_2", 2701, TS)
        assert len(results) == 1
        assert results[0]["schema"] == "homeops.consumer.heating_long_session_warning.v1"

    def test_long_session_fires_with_no_baseline_fallback(self):
        """When no baseline, absolute fallback threshold is used."""
        rule = FurnaceSessionAnomalyRule(baseline={})
        # floor_1 abs fallback = 1800s
        results = rule.check_session("floor_1", 1801, TS)
        assert len(results) == 1
        evt = results[0]
        assert evt["schema"] == "homeops.consumer.heating_long_session_warning.v1"
        assert evt["data"]["baseline_p95_s"] is None
        assert evt["data"]["threshold_s"] == 1800.0

    def test_long_session_fallback_floor2(self):
        """floor_2 no-baseline fallback = 2700s."""
        rule = FurnaceSessionAnomalyRule()
        results = rule.check_session("floor_2", 2701, TS)
        assert len(results) == 1
        assert results[0]["data"]["threshold_s"] == 2700.0

    def test_long_session_fallback_floor3(self):
        """floor_3 no-baseline fallback = 1200s."""
        rule = FurnaceSessionAnomalyRule()
        results = rule.check_session("floor_3", 1201, TS)
        assert len(results) == 1
        assert results[0]["data"]["threshold_s"] == 1200.0

    def test_high_p95_raises_threshold_above_fallback(self):
        """When p95 is large, threshold = p95 × 1.5 (above abs fallback)."""
        baseline = {
            "floor_1": {
                "count": 10,
                "min": 100,
                "max": 5000,
                "median": 1000,
                "p75": 2000,
                "p95": 2000.0,
            }
        }
        rule = FurnaceSessionAnomalyRule(baseline=baseline)
        # p95×1.5 = 3000 > abs fallback 1800 → threshold = 3000
        assert rule.check_session("floor_1", 2999, TS) == []
        results = rule.check_session("floor_1", 3001, TS)
        assert len(results) == 1
        assert results[0]["data"]["threshold_s"] == 3000.0


class TestNoneDurationSkipped:
    def test_none_duration_is_skipped(self):
        """duration_s=None (across_restart) → no events emitted."""
        rule = FurnaceSessionAnomalyRule(baseline=_BASELINE)
        results = rule.check_session("floor_1", None, TS)
        assert results == []

    def test_none_duration_no_baseline(self):
        """None duration with no baseline → also no events."""
        rule = FurnaceSessionAnomalyRule()
        results = rule.check_session("floor_2", None, TS)
        assert results == []


class TestFloorNoneHandled:
    def test_floor_none_short_session(self):
        """floor=None short session → warning with floor=None in data."""
        rule = FurnaceSessionAnomalyRule()
        results = rule.check_session(None, 30, TS)
        assert len(results) == 1
        assert results[0]["data"]["floor"] is None

    def test_floor_none_normal_session(self):
        """floor=None normal session → no warning (uses default fallback)."""
        rule = FurnaceSessionAnomalyRule()
        results = rule.check_session(None, 900, TS)
        assert results == []

    def test_floor_none_long_session_uses_default_fallback(self):
        """floor=None long session → uses _DEFAULT_LONG_SESSION_FALLBACK_S (2700s)."""
        rule = FurnaceSessionAnomalyRule()
        results = rule.check_session(None, 2701, TS)
        assert len(results) == 1
        assert results[0]["schema"] == "homeops.consumer.heating_long_session_warning.v1"
        assert results[0]["data"]["floor"] is None


class TestShortTakesPriorityOverLong:
    def test_pathological_short_session_does_not_fire_long_warning(self):
        """30s session: short fires, long does NOT also fire (short takes priority)."""
        # Make baseline so 30s would theoretically be "long" if short check weren't first
        # (This can't really happen physically, but we guard the logic.)
        baseline = {
            "floor_1": {
                "count": 10,
                "min": 1,
                "max": 100,
                "median": 10,
                "p75": 15,
                "p95": 1.0,  # p95×1.5 = 1.5s → 30s > 1.5s
            }
        }
        rule = FurnaceSessionAnomalyRule(baseline=baseline)
        results = rule.check_session("floor_1", 30, TS)

        assert len(results) == 1
        assert results[0]["schema"] == "homeops.consumer.heating_short_session_warning.v1"
        schemas = [r["schema"] for r in results]
        assert "homeops.consumer.heating_long_session_warning.v1" not in schemas
