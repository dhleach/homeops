"""Tests for the shared confidence scoring helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "insights"))

from rules.confidence import compute_confidence, severity_label  # noqa: E402


class TestComputeConfidence:
    def test_below_2sigma_returns_zero(self):
        assert compute_confidence(0.0) == 0.0
        assert compute_confidence(1.0) == 0.0
        assert compute_confidence(1.99) == 0.0

    def test_exactly_2sigma_returns_zero(self):
        assert compute_confidence(2.0) == 0.0

    def test_at_5sigma_saturates_to_one(self):
        assert compute_confidence(5.0) == 1.0

    def test_above_5sigma_capped_at_one(self):
        assert compute_confidence(10.0) == 1.0
        assert compute_confidence(100.0) == 1.0

    def test_midpoint_3_5sigma(self):
        # z=3.5 → (3.5 - 2) / 3 = 0.5
        assert abs(compute_confidence(3.5) - 0.5) < 1e-9

    def test_3sigma(self):
        # z=3 → (3 - 2) / 3 ≈ 0.333...
        assert abs(compute_confidence(3.0) - (1 / 3)) < 1e-9

    def test_negative_z_clamped_to_zero(self):
        # Should not happen in practice but guard against it.
        assert compute_confidence(-5.0) == 0.0


class TestSeverityLabel:
    def test_low_boundary(self):
        assert severity_label(0.0) == "low"
        assert severity_label(0.33) == "low"

    def test_medium_boundary(self):
        assert severity_label(0.34) == "medium"
        assert severity_label(0.66) == "medium"

    def test_high_boundary(self):
        assert severity_label(0.67) == "high"
        assert severity_label(1.0) == "high"


class TestFloorRuntimeAnomalyConfidence:
    """Integration: floor_runtime_anomaly events carry confidence + severity."""

    def setup_method(self):
        from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule

        self.Rule = FloorRuntimeAnomalyRule

    def _build_history(self, n: int, runtime: int = 3600) -> list[dict]:
        return [
            {
                "schema": "homeops.consumer.furnace_daily_summary.v1",
                "ts": "2026-01-01T00:00:00+00:00",
                "data": {
                    "date": f"2026-03-{i + 1:02d}",
                    "per_floor_runtime_s": {
                        "floor_2": runtime,
                    },
                },
            }
            for i in range(n)
        ]

    def test_confidence_and_severity_present(self):
        history = self._build_history(10, runtime=3600)
        rule = self.Rule(history)
        events = rule.check_daily_runtime("floor_2", 9000, "2026-03-28")
        assert len(events) == 1
        d = events[0]["data"]
        assert "confidence" in d
        assert "severity" in d
        assert "baseline_stddev_s" in d
        assert 0.0 <= d["confidence"] <= 1.0
        assert d["severity"] in {"low", "medium", "high"}

    def test_stddev_zero_defaults_to_medium(self):
        """All-identical history → stddev=0 → confidence=0.5, severity='medium'."""
        history = self._build_history(10, runtime=3600)
        rule = self.Rule(history)
        events = rule.check_daily_runtime("floor_2", 9000, "2026-03-28")
        assert len(events) == 1
        d = events[0]["data"]
        assert d["baseline_stddev_s"] == 0.0
        assert d["confidence"] == 0.5
        assert d["severity"] == "medium"

    def test_high_deviation_yields_high_confidence(self):
        """Extreme outlier relative to varied history → high confidence."""
        import random

        random.seed(42)
        # History with meaningful spread: values 1000–5000
        runtimes = [1000, 2000, 3000, 4000, 5000, 2500, 3500, 1500, 4500, 2000]
        history = [
            {
                "schema": "homeops.consumer.furnace_daily_summary.v1",
                "ts": "2026-01-01T00:00:00+00:00",
                "data": {
                    "date": f"2026-03-{i + 1:02d}",
                    "per_floor_runtime_s": {"floor_2": runtimes[i]},
                },
            }
            for i in range(10)
        ]
        rule = self.Rule(history)
        # runtime = 20000 — far above the mean (~2900) and well past 5σ
        events = rule.check_daily_runtime("floor_2", 20000, "2026-03-28")
        assert len(events) == 1
        assert events[0]["data"]["confidence"] == 1.0
        assert events[0]["data"]["severity"] == "high"


class TestFurnaceSessionAnomalyConfidence:
    """Integration: furnace_session_anomaly events carry confidence + severity."""

    def setup_method(self):
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        self.Rule = FurnaceSessionAnomalyRule

    _BASELINE = {
        "floor_1": {
            "count": 100,
            "min": 120,
            "max": 2400,
            "median": 600,
            "p75": 900,
            "p95": 1200.0,
        },
    }

    TS = "2024-01-15T10:00:00+00:00"

    def test_short_session_always_high_confidence(self):
        rule = self.Rule(baseline=self._BASELINE)
        events = rule.check_session("floor_1", 45, self.TS)
        assert len(events) == 1
        d = events[0]["data"]
        assert d["confidence"] == 1.0
        assert d["severity"] == "high"

    def test_long_session_confidence_present(self):
        rule = self.Rule(baseline=self._BASELINE)
        # floor_1 threshold = 1800; use 1900 → fires
        events = rule.check_session("floor_1", 1900, self.TS)
        assert len(events) == 1
        d = events[0]["data"]
        assert "confidence" in d
        assert "severity" in d
        assert 0.0 <= d["confidence"] <= 1.0
        assert d["severity"] in {"low", "medium", "high"}

    def test_long_session_extreme_yields_high_confidence(self):
        """Session far above threshold → confidence saturates to 1.0."""
        rule = self.Rule(baseline=self._BASELINE)
        # threshold=1800; 18000 is 10× threshold
        events = rule.check_session("floor_1", 18000, self.TS)
        assert len(events) == 1
        assert events[0]["data"]["confidence"] == 1.0
        assert events[0]["data"]["severity"] == "high"

    def test_long_session_just_above_threshold_low_confidence(self):
        """Session just barely above threshold → low confidence."""
        rule = self.Rule()
        # floor_1 fallback = 1800; 1810 is barely above
        # z_proxy = (1810 - 1800) / 1800 ≈ 0.0056 → confidence ≈ 0 → low
        events = rule.check_session("floor_1", 1810, self.TS)
        assert len(events) == 1
        d = events[0]["data"]
        assert d["confidence"] == 0.0
        assert d["severity"] == "low"
