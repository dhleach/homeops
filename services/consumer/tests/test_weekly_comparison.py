"""Tests for weekly comparison computation (TDD — written before implementation)."""

from datetime import date, timedelta

import pytest

# The module under test — will be created after tests pass
from weekly import (
    compute_weekly_comparison,
    format_weekly_comparison,
    load_daily_summaries,
    pct_change,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_summary(
    date_str: str,
    furnace_s: int,
    sessions: int,
    per_floor_s: dict,
    outdoor_temp_avg_f: float | None = None,
    per_floor_avg_setpoint_f: dict | None = None,
) -> dict:
    """Build a minimal furnace_daily_summary.v1 event dict."""
    return {
        "schema": "homeops.consumer.furnace_daily_summary.v1",
        "source": "consumer.v1",
        "ts": f"{date_str}T12:00:00+00:00",
        "data": {
            "date": date_str,
            "total_furnace_runtime_s": furnace_s,
            "session_count": sessions,
            "per_floor_runtime_s": per_floor_s,
            "per_floor_session_count": {},
            "outdoor_temp_min_f": None,
            "outdoor_temp_max_f": None,
            "outdoor_temp_avg_f": outdoor_temp_avg_f,
            "per_floor_avg_setpoint_f": per_floor_avg_setpoint_f or {},
            "warnings_triggered": {},
        },
    }


def _make_events(
    entries: list[tuple],
    outdoor_temps: dict[str, float] | None = None,
    setpoints: dict[str, dict[str, float]] | None = None,
) -> list[dict]:
    """
    entries: list of (date_str, furnace_s, sessions, per_floor_s)
    outdoor_temps: optional dict {date_str: outdoor_temp_avg_f}
    setpoints: optional dict {date_str: {floor: setpoint_f}}
    Returns a list of event dicts sorted by date.
    """
    return [
        _make_summary(
            d,
            f,
            s,
            p,
            outdoor_temp_avg_f=outdoor_temps.get(d) if outdoor_temps else None,
            per_floor_avg_setpoint_f=setpoints.get(d) if setpoints else None,
        )
        for d, f, s, p in entries
    ]


def _floor3() -> dict[str, int]:
    return {"floor_1": 1800, "floor_2": 900, "floor_3": 900}


def _floor3x2() -> dict[str, int]:
    return {"floor_1": 3600, "floor_2": 1800, "floor_3": 1800}


# ---------------------------------------------------------------------------
# pct_change
# ---------------------------------------------------------------------------


class TestPctChange:
    def test_increase(self):
        assert pct_change(100, 120) == pytest.approx(20.0)

    def test_decrease(self):
        assert pct_change(120, 100) == pytest.approx(-16.67, abs=0.01)

    def test_zero_last(self):
        # last week was 0 → can't compute pct; returns None
        assert pct_change(0, 50) is None

    def test_zero_both(self):
        assert pct_change(0, 0) is None

    def test_this_week_zero(self):
        # went from 100 to 0 → -100%
        assert pct_change(100, 0) == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# load_daily_summaries
# ---------------------------------------------------------------------------


class TestLoadDailySummaries:
    def test_filters_non_summary_events(self, tmp_path):
        import json

        log = tmp_path / "events.jsonl"
        s1 = _make_summary(
            "2026-03-25", 3600, 10, {"floor_1": 1800, "floor_2": 900, "floor_3": 900}
        )  # noqa: E501
        s2 = _make_summary(
            "2026-03-26", 7200, 20, {"floor_1": 3600, "floor_2": 1800, "floor_3": 1800}
        )  # noqa: E501
        events = [
            {"schema": "homeops.observer.state_changed.v1", "data": {}},
            s1,
            {"schema": "homeops.consumer.heating_session_ended.v1", "data": {}},
            s2,
        ]
        with log.open("w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        summaries = load_daily_summaries(str(log))
        assert len(summaries) == 2
        assert summaries[0]["data"]["date"] == "2026-03-25"
        assert summaries[1]["data"]["date"] == "2026-03-26"

    def test_empty_file(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("")
        assert load_daily_summaries(str(log)) == []

    def test_malformed_lines_skipped(self, tmp_path):
        import json

        log = tmp_path / "events.jsonl"
        log.write_text(
            "not json\n" + json.dumps(_make_summary("2026-03-25", 1000, 5, {})) + "\n" + "{broken\n"
        )
        summaries = load_daily_summaries(str(log))
        assert len(summaries) == 1


# ---------------------------------------------------------------------------
# compute_weekly_comparison
# ---------------------------------------------------------------------------


class TestComputeWeeklyComparison:
    def _make_14_days(self, anchor: date = None) -> list[dict]:
        """Return 14 days of summaries: last week + this week relative to anchor."""
        if anchor is None:
            anchor = date(2026, 3, 31)
        events = []
        for i in range(14):
            d = anchor - timedelta(days=13 - i)  # oldest → newest
            events.append(
                _make_summary(
                    str(d),
                    furnace_s=3600 * (i + 1),
                    sessions=i + 1,
                    per_floor_s={
                        "floor_1": 1800 * (i + 1),
                        "floor_2": 900 * (i + 1),
                        "floor_3": 450 * (i + 1),
                    },
                )
            )
        return events

    def test_returns_weeklycomparison(self):
        events = self._make_14_days()
        result = compute_weekly_comparison(events)
        assert result is not None

    def test_this_week_last_week_span(self):
        anchor = date(2026, 3, 31)
        events = self._make_14_days(anchor)
        result = compute_weekly_comparison(events)
        # this_week = last 7 days of data = days 7–13 (0-indexed from oldest)
        assert result.this_week.day_count == 7
        assert result.last_week.day_count == 7

    def test_total_furnace_runtime(self):
        # Build controlled data: last week = 7×3600s, this week = 7×7200s
        anchor = date(2026, 3, 31)
        last_week_days = [
            (str(anchor - timedelta(days=13 - i)), 3600, 5, _floor3()) for i in range(7)
        ]
        this_week_days = [
            (str(anchor - timedelta(days=6 - i)), 7200, 10, _floor3x2()) for i in range(7)
        ]
        events = _make_events(last_week_days + this_week_days)
        result = compute_weekly_comparison(events)
        assert result.last_week.total_furnace_s == 7 * 3600
        assert result.this_week.total_furnace_s == 7 * 7200

    def test_session_count(self):
        anchor = date(2026, 3, 31)
        last_week_days = [(str(anchor - timedelta(days=13 - i)), 3600, 5, {}) for i in range(7)]
        this_week_days = [(str(anchor - timedelta(days=6 - i)), 3600, 10, {}) for i in range(7)]
        events = _make_events(last_week_days + this_week_days)
        result = compute_weekly_comparison(events)
        assert result.last_week.session_count == 35
        assert result.this_week.session_count == 70

    def test_floor_avg_daily(self):
        anchor = date(2026, 3, 31)
        lw_floor = {"floor_1": 1800, "floor_2": 900, "floor_3": 300}
        tw_floor = {"floor_1": 3600, "floor_2": 1800, "floor_3": 600}
        last_week = [(str(anchor - timedelta(days=13 - i)), 3600, 5, lw_floor) for i in range(7)]
        this_week = [(str(anchor - timedelta(days=6 - i)), 3600, 5, tw_floor) for i in range(7)]
        events = _make_events(last_week + this_week)
        result = compute_weekly_comparison(events)
        # avg daily = total / day_count
        assert result.last_week.floor_avg_daily_s["floor_1"] == pytest.approx(1800.0)
        assert result.last_week.floor_avg_daily_s["floor_2"] == pytest.approx(900.0)
        assert result.last_week.floor_avg_daily_s["floor_3"] == pytest.approx(300.0)
        assert result.this_week.floor_avg_daily_s["floor_1"] == pytest.approx(3600.0)
        assert result.this_week.floor_avg_daily_s["floor_2"] == pytest.approx(1800.0)
        assert result.this_week.floor_avg_daily_s["floor_3"] == pytest.approx(600.0)

    def test_fewer_than_14_days_uses_available_data(self):
        # Only 10 days available — should still work
        anchor = date(2026, 3, 31)
        floor = {"floor_1": 1800, "floor_2": 900, "floor_3": 300}
        events = [
            _make_summary(str(anchor - timedelta(days=9 - i)), 3600, 5, floor) for i in range(10)
        ]
        result = compute_weekly_comparison(events)
        # this_week = last 7 days, last_week = 3 days
        assert result.this_week.day_count == 7
        assert result.last_week.day_count == 3

    def test_fewer_than_7_days_returns_none(self):
        # Less than 7 days — not enough for a this_week window
        anchor = date(2026, 3, 31)
        events = [_make_summary(str(anchor - timedelta(days=i)), 3600, 5, {}) for i in range(5)]
        result = compute_weekly_comparison(events)
        assert result is None

    def test_empty_events_returns_none(self):
        result = compute_weekly_comparison([])
        assert result is None

    def test_outdoor_temp_aggregation(self):
        # Build data with outdoor temperatures
        anchor = date(2026, 3, 31)
        outdoor_temps = {str(anchor - timedelta(days=13 - i)): 30.0 + i for i in range(14)}
        last_week_days = [
            (str(anchor - timedelta(days=13 - i)), 3600, 5, _floor3()) for i in range(7)
        ]
        this_week_days = [
            (str(anchor - timedelta(days=6 - i)), 3600, 5, _floor3x2()) for i in range(7)
        ]
        events = _make_events(last_week_days + this_week_days, outdoor_temps=outdoor_temps)
        result = compute_weekly_comparison(events)
        assert result is not None
        # Last week: temps 30-36, avg should be 33.0
        assert result.last_week.outdoor_temp_avg_f == pytest.approx(33.0)
        # This week: temps 37-43, avg should be 40.0
        assert result.this_week.outdoor_temp_avg_f == pytest.approx(40.0)

    def test_outdoor_temp_missing_gracefully(self):
        # Data without outdoor temps — should not crash
        anchor = date(2026, 3, 31)
        last_week_days = [
            (str(anchor - timedelta(days=13 - i)), 3600, 5, _floor3()) for i in range(7)
        ]
        this_week_days = [
            (str(anchor - timedelta(days=6 - i)), 3600, 5, _floor3x2()) for i in range(7)
        ]
        events = _make_events(last_week_days + this_week_days)
        result = compute_weekly_comparison(events)
        assert result is not None
        assert result.last_week.outdoor_temp_avg_f is None
        assert result.this_week.outdoor_temp_avg_f is None

    def test_per_floor_setpoint_aggregation(self):
        # Build data with per-floor setpoints
        anchor = date(2026, 3, 31)
        setpoints = {
            str(anchor - timedelta(days=13 - i)): {
                "floor_1": 68.0,
                "floor_2": 70.0 + i * 0.1,
                "floor_3": 68.0,
            }
            for i in range(14)
        }
        last_week_days = [
            (str(anchor - timedelta(days=13 - i)), 3600, 5, _floor3()) for i in range(7)
        ]
        this_week_days = [
            (str(anchor - timedelta(days=6 - i)), 3600, 5, _floor3x2()) for i in range(7)
        ]
        events = _make_events(
            last_week_days + this_week_days,
            setpoints=setpoints,
        )
        result = compute_weekly_comparison(events)
        assert result is not None
        lw_sp = result.last_week.per_floor_avg_setpoint_f
        tw_sp = result.this_week.per_floor_avg_setpoint_f
        # Floor 1 and 3 should be ~68 for both weeks
        assert lw_sp.get("floor_1") == pytest.approx(68.0)
        assert lw_sp.get("floor_3") == pytest.approx(68.0)
        # Floor 2: last week = avg(70.0–70.6) ≈ 70.3, this week = avg(70.7–71.3) ≈ 71.0
        assert lw_sp.get("floor_2") == pytest.approx(70.3, abs=0.1)
        assert tw_sp.get("floor_2") == pytest.approx(71.0, abs=0.1)

    def test_per_floor_setpoint_missing_gracefully(self):
        # Data without setpoints — should not crash
        anchor = date(2026, 3, 31)
        last_week_days = [
            (str(anchor - timedelta(days=13 - i)), 3600, 5, _floor3()) for i in range(7)
        ]
        this_week_days = [
            (str(anchor - timedelta(days=6 - i)), 3600, 5, _floor3x2()) for i in range(7)
        ]
        events = _make_events(last_week_days + this_week_days)
        result = compute_weekly_comparison(events)
        assert result is not None
        assert result.last_week.per_floor_avg_setpoint_f == {}
        assert result.this_week.per_floor_avg_setpoint_f == {}

    def test_mixed_events_with_and_without_data(self):
        # Some days have outdoor temp/setpoints, some don't (backward compat)
        anchor = date(2026, 3, 31)
        # Last week: first 3 days no data, last 4 days with data
        last_week_days = [
            (str(anchor - timedelta(days=13 - i)), 3600, 5, _floor3()) for i in range(7)
        ]
        # This week: all days with data
        this_week_days = [
            (str(anchor - timedelta(days=6 - i)), 3600, 5, _floor3x2()) for i in range(7)
        ]
        outdoor_temps = {str(anchor - timedelta(days=13 - i)): 30.0 + i for i in range(3, 7)}
        outdoor_temps.update({str(anchor - timedelta(days=6 - i)): 40.0 + i for i in range(7)})
        setpoints_data = {}
        for i in range(3, 7):
            setpoints_data[str(anchor - timedelta(days=13 - i))] = {
                "floor_1": 68.0,
                "floor_2": 70.0,
                "floor_3": 68.0,
            }
        for i in range(7):
            setpoints_data[str(anchor - timedelta(days=6 - i))] = {
                "floor_1": 68.0,
                "floor_2": 71.0,
                "floor_3": 68.0,
            }
        events = _make_events(
            last_week_days + this_week_days,
            outdoor_temps=outdoor_temps,
            setpoints=setpoints_data,
        )
        result = compute_weekly_comparison(events)
        assert result is not None
        # Last week should have only 4 days of temp data
        assert len(result.last_week.outdoor_temp_avg_f_samples) == 4
        # This week should have 7 days of temp data
        assert len(result.this_week.outdoor_temp_avg_f_samples) == 7


# ---------------------------------------------------------------------------
# format_weekly_comparison
# ---------------------------------------------------------------------------


class TestFormatWeeklyComparison:
    def _make_result(self):
        anchor = date(2026, 3, 31)
        lw_floor = {"floor_1": 2700, "floor_2": 1800, "floor_3": 900}
        tw_floor = {"floor_1": 3060, "floor_2": 2160, "floor_3": 1080}
        last_week = [(str(anchor - timedelta(days=13 - i)), 3600, 5, lw_floor) for i in range(7)]
        this_week = [(str(anchor - timedelta(days=6 - i)), 4320, 8, tw_floor) for i in range(7)]
        events = _make_events(last_week + this_week)
        return compute_weekly_comparison(events)

    def test_output_contains_header(self):
        result = self._make_result()
        output = format_weekly_comparison(result)
        assert "Weekly Comparison" in output

    def test_output_contains_total_furnace_runtime(self):
        result = self._make_result()
        output = format_weekly_comparison(result)
        assert "Total furnace runtime" in output

    def test_output_contains_session_count(self):
        result = self._make_result()
        output = format_weekly_comparison(result)
        assert "Session count" in output

    def test_output_contains_floor_lines(self):
        result = self._make_result()
        output = format_weekly_comparison(result)
        assert "Floor 1" in output
        assert "Floor 2" in output
        assert "Floor 3" in output

    def test_floor2_increase_flagged(self):
        # Floor 2 increase should show a warning marker
        result = self._make_result()
        output = format_weekly_comparison(result)
        lines = output.splitlines()
        floor2_line = next((ln for ln in lines if "Floor 2" in ln), None)
        assert floor2_line is not None
        # Floor 2 went UP so it should be flagged
        assert "watch" in floor2_line.lower() or "⚠" in floor2_line or "←" in floor2_line

    def test_floor2_decrease_not_flagged(self):
        # If floor 2 decreases, no flag
        anchor = date(2026, 3, 31)
        lw_floor = {"floor_1": 1800, "floor_2": 3600, "floor_3": 900}
        tw_floor = {"floor_1": 1800, "floor_2": 1800, "floor_3": 900}
        last_week = [(str(anchor - timedelta(days=13 - i)), 3600, 5, lw_floor) for i in range(7)]
        this_week = [(str(anchor - timedelta(days=6 - i)), 3600, 5, tw_floor) for i in range(7)]
        events = _make_events(last_week + this_week)
        result = compute_weekly_comparison(events)
        output = format_weekly_comparison(result)
        lines = output.splitlines()
        floor2_line = next((ln for ln in lines if "Floor 2" in ln), None)
        assert floor2_line is not None
        assert "watch" not in floor2_line.lower()
        assert "←" not in floor2_line

    def test_format_uses_hm_for_runtimes(self):
        result = self._make_result()
        output = format_weekly_comparison(result)
        # Should have hour/minute notation
        assert "h" in output and "m" in output

    def test_format_shows_pct_change(self):
        result = self._make_result()
        output = format_weekly_comparison(result)
        assert "%" in output

    def test_format_with_none_result_raises(self):
        with pytest.raises((TypeError, AttributeError)):
            format_weekly_comparison(None)

    def test_outdoor_temp_row_present(self):
        # Build data with outdoor temps
        anchor = date(2026, 3, 31)
        outdoor_temps = {str(anchor - timedelta(days=13 - i)): 28.0 + i for i in range(14)}
        lw_floor = {"floor_1": 1800, "floor_2": 900, "floor_3": 300}
        tw_floor = {"floor_1": 3600, "floor_2": 1800, "floor_3": 600}
        last_week = [(str(anchor - timedelta(days=13 - i)), 3600, 5, lw_floor) for i in range(7)]
        this_week = [(str(anchor - timedelta(days=6 - i)), 3600, 5, tw_floor) for i in range(7)]
        events = _make_events(last_week + this_week, outdoor_temps=outdoor_temps)
        result = compute_weekly_comparison(events)
        output = format_weekly_comparison(result)
        assert "Avg outdoor temp" in output

    def test_outdoor_temp_row_absent_when_no_data(self):
        # When no outdoor temp data, row should be absent
        result = self._make_result()
        output = format_weekly_comparison(result)
        assert "Avg outdoor temp" not in output

    def test_setpoint_annotation_on_floor_rows(self):
        # Build data with setpoints
        anchor = date(2026, 3, 31)
        setpoints = {
            str(anchor - timedelta(days=13 - i)): {
                "floor_1": 68.0,
                "floor_2": 70.0,
                "floor_3": 68.0,
            }
            for i in range(14)
        }
        lw_floor = {"floor_1": 1800, "floor_2": 900, "floor_3": 300}
        tw_floor = {"floor_1": 3600, "floor_2": 1800, "floor_3": 600}
        last_week = [(str(anchor - timedelta(days=13 - i)), 3600, 5, lw_floor) for i in range(7)]
        this_week = [(str(anchor - timedelta(days=6 - i)), 3600, 5, tw_floor) for i in range(7)]
        events = _make_events(last_week + this_week, setpoints=setpoints)
        result = compute_weekly_comparison(events)
        output = format_weekly_comparison(result)
        # Each floor row should have setpoint annotation
        lines = output.splitlines()
        floor1_line = next((ln for ln in lines if "Floor 1" in ln), None)
        floor2_line = next((ln for ln in lines if "Floor 2" in ln), None)
        floor3_line = next((ln for ln in lines if "Floor 3" in ln), None)
        assert floor1_line is not None and "setpoint" in floor1_line
        assert floor2_line is not None and "setpoint" in floor2_line
        assert floor3_line is not None and "setpoint" in floor3_line

    def test_setpoint_annotation_absent_when_no_data(self):
        # When no setpoint data, annotations should be absent
        result = self._make_result()
        output = format_weekly_comparison(result)
        # No floor row should have a setpoint annotation
        lines = output.splitlines()
        for line in lines:
            if "Floor" in line and "avg daily" in line:
                assert "setpoint" not in line

    def test_format_backward_compat_no_crash_missing_temps(self):
        # Old events without outdoor_temp_avg_f should not crash
        result = self._make_result()
        output = format_weekly_comparison(result)
        assert output is not None
        assert len(output) > 0
