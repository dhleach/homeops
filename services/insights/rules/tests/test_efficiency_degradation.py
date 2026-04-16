"""Tests for the EfficiencyDegradationRule."""

from __future__ import annotations

import pytest
from rules.efficiency_degradation import EfficiencyDegradationRule, _iso_week_key, _linear_slope

_SCHEMA = "homeops.consumer.heating_session_ended.v1"


def _session(floor: str, duration_s: float, date: str) -> dict:
    """Build a minimal heating_session_ended event."""
    return {
        "schema": _SCHEMA,
        "ts": f"{date}T12:00:00+00:00",
        "data": {"floor": floor, "duration_s": duration_s},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestLinearSlope:
    def test_positive_slope(self):
        xs = [0.0, 1.0, 2.0]
        ys = [100.0, 200.0, 300.0]
        assert abs(_linear_slope(xs, ys) - 100.0) < 1e-9

    def test_zero_slope_flat(self):
        xs = [0.0, 1.0, 2.0]
        ys = [200.0, 200.0, 200.0]
        assert _linear_slope(xs, ys) == pytest.approx(0.0)

    def test_negative_slope(self):
        xs = [0.0, 1.0, 2.0]
        ys = [300.0, 200.0, 100.0]
        assert abs(_linear_slope(xs, ys) - (-100.0)) < 1e-9

    def test_single_point_returns_zero(self):
        assert _linear_slope([0.0], [100.0]) == 0.0

    def test_zero_x_variance_returns_zero(self):
        assert _linear_slope([1.0, 1.0, 1.0], [100.0, 200.0, 300.0]) == 0.0


class TestIsoWeekKey:
    def test_known_dates(self):
        from datetime import UTC, datetime

        dt = datetime(2026, 3, 2, tzinfo=UTC)  # Monday of week 10
        assert _iso_week_key(dt) == "2026-W10"

    def test_week_boundary(self):
        from datetime import UTC, datetime

        dt = datetime(2026, 1, 1, tzinfo=UTC)  # 2026-01-01 is a Thursday — week 1
        key = _iso_week_key(dt)
        assert key.startswith("2026-W")


# ---------------------------------------------------------------------------
# EfficiencyDegradationRule.check()
# ---------------------------------------------------------------------------


class TestEfficiencyDegradationRule:
    def _weeks_of_sessions(
        self,
        floor: str,
        week_durations: list[tuple[str, list[float]]],
    ) -> list[dict]:
        """
        Build session events from [(week_start_date, [durations]), ...].
        ``week_start_date`` should be a Monday (YYYY-MM-DD).
        """
        events = []
        for date, durations in week_durations:
            for i, dur in enumerate(durations):
                events.append(
                    {
                        "schema": _SCHEMA,
                        "ts": f"{date}T{10 + i:02d}:00:00+00:00",
                        "data": {"floor": floor, "duration_s": dur},
                    }
                )
        return events

    def test_detects_clear_upward_trend(self):
        """Three weeks with clearly increasing means → finding."""
        # Week 1: mean ~300s, Week 2: ~400s, Week 3: ~500s
        events = self._weeks_of_sessions(
            "floor_2",
            [
                ("2026-03-02", [280.0, 300.0, 320.0]),  # mean 300
                ("2026-03-09", [380.0, 400.0, 420.0]),  # mean 400
                ("2026-03-16", [480.0, 500.0, 520.0]),  # mean 500
            ],
        )
        rule = EfficiencyDegradationRule(
            history=events,
            min_weeks=3,
            min_events_per_week=3,
            slope_threshold_s_per_week=60.0,
        )
        findings = rule.check()
        assert len(findings) == 1
        f = findings[0]
        assert f["data"]["floor"] == "floor_2"
        assert f["data"]["slope_s_per_week"] > 60.0
        assert f["schema"] == "homeops.insights.efficiency_degradation.v1"

    def test_no_finding_flat_trend(self):
        """Flat week-over-week means → no finding."""
        events = self._weeks_of_sessions(
            "floor_1",
            [
                ("2026-03-02", [300.0, 300.0, 300.0]),
                ("2026-03-09", [300.0, 300.0, 300.0]),
                ("2026-03-16", [300.0, 300.0, 300.0]),
            ],
        )
        rule = EfficiencyDegradationRule(
            history=events, min_weeks=3, min_events_per_week=3, slope_threshold_s_per_week=60.0
        )
        assert rule.check() == []

    def test_no_finding_decreasing_trend(self):
        """Decreasing means (getting more efficient) → no finding."""
        events = self._weeks_of_sessions(
            "floor_1",
            [
                ("2026-03-02", [500.0, 520.0, 510.0]),
                ("2026-03-09", [400.0, 410.0, 390.0]),
                ("2026-03-16", [300.0, 310.0, 290.0]),
            ],
        )
        rule = EfficiencyDegradationRule(
            history=events, min_weeks=3, min_events_per_week=3, slope_threshold_s_per_week=60.0
        )
        assert rule.check() == []

    def test_insufficient_weeks_skipped(self):
        """Only 2 qualifying weeks (min is 3) → no finding."""
        events = self._weeks_of_sessions(
            "floor_1",
            [
                ("2026-03-02", [300.0, 310.0, 320.0]),
                ("2026-03-09", [450.0, 460.0, 470.0]),
            ],
        )
        rule = EfficiencyDegradationRule(
            history=events, min_weeks=3, min_events_per_week=3, slope_threshold_s_per_week=60.0
        )
        assert rule.check() == []

    def test_insufficient_events_per_week_skipped(self):
        """Weeks with fewer than min_events_per_week are excluded."""
        # 3 weeks but each has only 2 events (min is 3)
        events = self._weeks_of_sessions(
            "floor_1",
            [
                ("2026-03-02", [300.0, 320.0]),
                ("2026-03-09", [400.0, 420.0]),
                ("2026-03-16", [500.0, 520.0]),
            ],
        )
        rule = EfficiencyDegradationRule(
            history=events, min_weeks=3, min_events_per_week=3, slope_threshold_s_per_week=60.0
        )
        assert rule.check() == []

    def test_non_monotonic_tail_suppressed(self):
        """Upward slope but last 3 means not monotonically non-decreasing → no finding."""
        events = self._weeks_of_sessions(
            "floor_1",
            [
                ("2026-03-02", [300.0, 300.0, 300.0]),
                ("2026-03-09", [400.0, 400.0, 400.0]),
                ("2026-03-16", [350.0, 350.0, 350.0]),  # dips back down
            ],
        )
        rule = EfficiencyDegradationRule(
            history=events, min_weeks=3, min_events_per_week=3, slope_threshold_s_per_week=60.0
        )
        assert rule.check() == []

    def test_empty_history(self):
        rule = EfficiencyDegradationRule(history=[], min_weeks=3, min_events_per_week=3)
        assert rule.check() == []

    def test_ignores_non_session_events(self):
        noise = [
            {
                "schema": "homeops.consumer.furnace_daily_summary.v1",
                "ts": "2026-03-01T12:00:00Z",
                "data": {},
            }
        ]
        rule = EfficiencyDegradationRule(history=noise, min_weeks=3, min_events_per_week=3)
        assert rule.check() == []

    def test_finding_fields_present(self):
        events = self._weeks_of_sessions(
            "floor_2",
            [
                ("2026-03-02", [280.0, 300.0, 320.0]),
                ("2026-03-09", [380.0, 400.0, 420.0]),
                ("2026-03-16", [480.0, 500.0, 520.0]),
            ],
        )
        rule = EfficiencyDegradationRule(
            history=events, min_weeks=3, min_events_per_week=3, slope_threshold_s_per_week=60.0
        )
        findings = rule.check()
        assert findings
        d = findings[0]["data"]
        for key in (
            "floor",
            "slope_s_per_week",
            "threshold_s_per_week",
            "weeks_analysed",
            "week_keys",
            "weekly_mean_s",
            "earliest_week",
            "latest_week",
        ):
            assert key in d, f"missing key {key!r}"
