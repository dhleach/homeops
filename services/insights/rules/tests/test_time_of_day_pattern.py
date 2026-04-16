"""Tests for the TimeOfDayPatternRule."""

from __future__ import annotations

from rules.time_of_day_pattern import TimeOfDayPatternRule, _period_for_hour

_SCHEMA = "homeops.consumer.heating_session_ended.v1"


def _session(floor: str, hour: int, date: str = "2026-03-01") -> dict:
    """Build a minimal heating_session_ended event for a given floor and UTC hour."""
    return {
        "schema": _SCHEMA,
        "ts": f"{date}T{hour:02d}:00:00+00:00",
        "data": {"floor": floor, "duration_s": 600},
    }


# ---------------------------------------------------------------------------
# _period_for_hour
# ---------------------------------------------------------------------------


class TestPeriodForHour:
    def test_night_boundaries(self):
        assert _period_for_hour(0) == "night"
        assert _period_for_hour(5) == "night"

    def test_morning_boundaries(self):
        assert _period_for_hour(6) == "morning"
        assert _period_for_hour(11) == "morning"

    def test_afternoon_boundaries(self):
        assert _period_for_hour(12) == "afternoon"
        assert _period_for_hour(17) == "afternoon"

    def test_evening_boundaries(self):
        assert _period_for_hour(18) == "evening"
        assert _period_for_hour(23) == "evening"


# ---------------------------------------------------------------------------
# TimeOfDayPatternRule.check()
# ---------------------------------------------------------------------------


class TestTimeOfDayPatternRule:
    def _make_history(self, floor: str, hour_counts: dict[int, int]) -> list[dict]:
        """Build history events: {hour: count}."""
        events = []
        for hour, count in hour_counts.items():
            for i in range(count):
                events.append(_session(floor, hour, date=f"2026-02-{(i % 28) + 1:02d}"))
        return events

    def test_no_anomaly_uniform_distribution(self):
        """Uniform distribution across all periods → no findings."""
        history = []
        for hour in [2, 8, 14, 20]:  # one per period
            for i in range(4):
                history.append(_session("floor_1", hour, date=f"2026-02-{i + 1:02d}"))
        window = []
        for hour in [2, 8, 14, 20]:
            for i in range(2):
                window.append(_session("floor_1", hour, date=f"2026-03-{i + 1:02d}"))
        rule = TimeOfDayPatternRule(
            history=history, threshold_ratio=1.8, min_events=8, min_window_events=2
        )
        assert rule.check(window) == []

    def test_detects_night_spike(self):
        """Window concentrated in night when baseline has even spread → finding."""
        # History: 4 calls per period × 4 periods = 16 total
        history = self._make_history("floor_2", {2: 4, 8: 4, 14: 4, 20: 4})
        # Window: all 6 calls in night period
        window = [_session("floor_2", 3, date=f"2026-03-{i + 1:02d}") for i in range(6)]
        rule = TimeOfDayPatternRule(
            history=history, threshold_ratio=1.8, min_events=8, min_window_events=3
        )
        findings = rule.check(window)
        assert len(findings) == 1
        assert findings[0]["data"]["floor"] == "floor_2"
        assert findings[0]["data"]["period"] == "night"
        assert findings[0]["schema"] == "homeops.insights.time_of_day_anomaly.v1"

    def test_insufficient_history_skipped(self):
        """Fewer than min_events in history → no findings even with obvious spike."""
        history = [_session("floor_1", 2, date=f"2026-02-{i + 1:02d}") for i in range(3)]  # only 3
        window = [_session("floor_1", 2, date=f"2026-03-{i + 1:02d}") for i in range(6)]
        rule = TimeOfDayPatternRule(
            history=history, threshold_ratio=1.8, min_events=8, min_window_events=3
        )
        assert rule.check(window) == []

    def test_insufficient_window_events_skipped(self):
        """Window has fewer than min_window_events in the anomalous period → no findings."""
        history = self._make_history("floor_1", {2: 4, 8: 4, 14: 4, 20: 4})
        # Only 2 night calls in window (min is 3)
        window = [_session("floor_1", 2, date=f"2026-03-{i + 1:02d}") for i in range(2)]
        rule = TimeOfDayPatternRule(
            history=history, threshold_ratio=1.8, min_events=8, min_window_events=3
        )
        assert rule.check(window) == []

    def test_empty_window_no_findings(self):
        history = self._make_history("floor_1", {2: 4, 8: 4, 14: 4, 20: 4})
        rule = TimeOfDayPatternRule(history=history)
        assert rule.check([]) == []

    def test_empty_history_no_findings(self):
        window = [_session("floor_1", 2, date=f"2026-03-{i + 1:02d}") for i in range(5)]
        rule = TimeOfDayPatternRule(history=[])
        assert rule.check(window) == []

    def test_ignores_non_session_events(self):
        """Non-session schemas in history/window are silently ignored."""
        history = self._make_history("floor_1", {2: 4, 8: 4, 14: 4, 20: 4})
        noise = [
            {
                "schema": "homeops.consumer.furnace_daily_summary.v1",
                "ts": "2026-03-01T02:00:00Z",
                "data": {},
            }
        ]
        rule = TimeOfDayPatternRule(history=history)
        # Should not raise, noise events are skipped
        result = rule.check(noise)
        assert isinstance(result, list)

    def test_finding_fields_present(self):
        """Validate all expected fields in a finding."""
        history = self._make_history("floor_3", {2: 4, 8: 4, 14: 4, 20: 4})
        window = [_session("floor_3", 3, date=f"2026-03-{i + 1:02d}") for i in range(6)]
        rule = TimeOfDayPatternRule(
            history=history, threshold_ratio=1.8, min_events=8, min_window_events=3
        )
        findings = rule.check(window)
        assert findings, "expected at least one finding"
        f = findings[0]["data"]
        for key in (
            "floor",
            "period",
            "observed_share",
            "historical_share",
            "ratio",
            "observed_count",
            "observed_total",
            "historical_count",
            "historical_total",
            "threshold_ratio",
        ):
            assert key in f, f"missing key {key!r}"
