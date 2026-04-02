"""Tests for monthly.py and summary.py --month command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from monthly import (
    MonthlyStats,
    _fmt_duration,
    compute_monthly_summary,
    format_monthly_summary,
)
from summary import build_parser, cmd_month, main

_SCHEMA = "homeops.consumer.furnace_daily_summary.v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_summary(
    date: str,
    runtime_s: int = 3600,
    sessions: int = 4,
    outdoor_avg: float | None = 35.0,
    outdoor_min: float | None = 28.0,
    outdoor_max: float | None = 42.0,
    floor_1: int = 1200,
    floor_2: int = 1800,
    floor_3: int = 600,
    warnings: dict | None = None,
) -> dict:
    return {
        "schema": _SCHEMA,
        "source": "consumer.v1",
        "ts": f"{date}T00:00:05+00:00",
        "data": {
            "date": date,
            "total_furnace_runtime_s": runtime_s,
            "session_count": sessions,
            "per_floor_runtime_s": {"floor_1": floor_1, "floor_2": floor_2, "floor_3": floor_3},
            "per_floor_session_count": {"floor_1": 2, "floor_2": 1, "floor_3": 1},
            "outdoor_temp_avg_f": outdoor_avg,
            "outdoor_temp_min_f": outdoor_min,
            "outdoor_temp_max_f": outdoor_max,
            "warnings_triggered": warnings
            or {
                "floor_2_long_call": 0,
                "floor_no_response": 0,
                "zone_slow_to_heat": 0,
                "observer_silence": 0,
                "setpoint_miss": 0,
            },
        },
    }


def _write_log(tmp_path: Path, events: list[dict]) -> Path:
    log = tmp_path / "events.jsonl"
    with open(log, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")
    return log


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_zero(self):
        assert _fmt_duration(0) == "0m"

    def test_minutes_only(self):
        assert _fmt_duration(900) == "15m"

    def test_hours_and_minutes(self):
        assert _fmt_duration(3660) == "1h 01m"


# ---------------------------------------------------------------------------
# compute_monthly_summary
# ---------------------------------------------------------------------------


class TestComputeMonthlySummary:
    def test_empty_events_returns_zero_day_count(self):
        stats = compute_monthly_summary([], "2026-01")
        assert stats.day_count == 0
        assert stats.total_furnace_s == 0

    def test_sums_runtime(self):
        events = [
            _make_summary("2026-01-01", runtime_s=3600),
            _make_summary("2026-01-02", runtime_s=7200),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.total_furnace_s == 10800

    def test_sums_session_count(self):
        events = [
            _make_summary("2026-01-01", sessions=4),
            _make_summary("2026-01-02", sessions=6),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.session_count == 10

    def test_filters_to_correct_month(self):
        events = [
            _make_summary("2026-01-15", runtime_s=3600),
            _make_summary("2026-02-01", runtime_s=9999),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.day_count == 1
        assert stats.total_furnace_s == 3600

    def test_outdoor_avg_f_computed(self):
        events = [
            _make_summary("2026-01-01", outdoor_avg=30.0),
            _make_summary("2026-01-02", outdoor_avg=40.0),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.outdoor_avg_f == 35.0

    def test_outdoor_min_max_across_days(self):
        events = [
            _make_summary("2026-01-01", outdoor_min=20.0, outdoor_max=35.0),
            _make_summary("2026-01-02", outdoor_min=25.0, outdoor_max=42.0),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.outdoor_min_f == 20.0
        assert stats.outdoor_max_f == 42.0

    def test_per_floor_runtime_summed(self):
        events = [
            _make_summary("2026-01-01", floor_2=1800),
            _make_summary("2026-01-02", floor_2=3600),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.per_floor_s["floor_2"] == 5400

    def test_warnings_summed(self):
        events = [
            _make_summary(
                "2026-01-01",
                warnings={
                    "floor_2_long_call": 1,
                    "floor_no_response": 0,
                    "zone_slow_to_heat": 0,
                    "observer_silence": 0,
                    "setpoint_miss": 0,
                },
            ),
            _make_summary(
                "2026-01-02",
                warnings={
                    "floor_2_long_call": 2,
                    "floor_no_response": 0,
                    "zone_slow_to_heat": 0,
                    "observer_silence": 0,
                    "setpoint_miss": 0,
                },
            ),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.warnings["floor_2_long_call"] == 3

    def test_none_outdoor_temps_excluded(self):
        events = [
            _make_summary("2026-01-01", outdoor_avg=None, outdoor_min=None, outdoor_max=None),
        ]
        stats = compute_monthly_summary(events, "2026-01")
        assert stats.outdoor_avg_f is None
        assert stats.outdoor_min_f is None

    def test_days_in_month_january(self):
        stats = compute_monthly_summary([], "2026-01")
        assert stats.days_in_month == 31

    def test_days_in_month_february_non_leap(self):
        stats = compute_monthly_summary([], "2026-02")
        assert stats.days_in_month == 28

    def test_invalid_month_format_raises(self):
        with pytest.raises(ValueError):
            compute_monthly_summary([], "2026-1")


# ---------------------------------------------------------------------------
# format_monthly_summary
# ---------------------------------------------------------------------------


class TestFormatMonthlySummary:
    def _build_stats(self) -> MonthlyStats:
        events = [
            _make_summary(
                "2026-01-01",
                runtime_s=7200,
                sessions=5,
                outdoor_avg=32.0,
                outdoor_min=22.0,
                outdoor_max=40.0,
                floor_2=3600,
            ),
            _make_summary(
                "2026-01-02",
                runtime_s=5400,
                sessions=3,
                outdoor_avg=28.0,
                outdoor_min=20.0,
                outdoor_max=35.0,
                floor_2=2700,
            ),
        ]
        return compute_monthly_summary(events, "2026-01")

    def test_contains_month(self):
        stats = self._build_stats()
        output = format_monthly_summary(stats)
        assert "2026-01" in output

    def test_contains_total_runtime(self):
        stats = self._build_stats()
        output = format_monthly_summary(stats)
        assert "3h" in output  # 12600s = 3h 30m

    def test_contains_session_count(self):
        stats = self._build_stats()
        output = format_monthly_summary(stats)
        assert "8" in output  # 5 + 3

    def test_contains_floor_labels(self):
        stats = self._build_stats()
        output = format_monthly_summary(stats)
        assert "Floor 2" in output

    def test_no_warnings_section_when_all_zero(self):
        stats = self._build_stats()
        output = format_monthly_summary(stats)
        assert "Warnings" not in output

    def test_warnings_shown_when_nonzero(self):
        warn = {
            "floor_2_long_call": 2,
            "floor_no_response": 0,
            "zone_slow_to_heat": 0,
            "observer_silence": 0,
            "setpoint_miss": 0,
        }
        events = [_make_summary("2026-01-01", warnings=warn)]
        stats = compute_monthly_summary(events, "2026-01")
        output = format_monthly_summary(stats)
        assert "floor_2_long_call" in output
        assert "2" in output


# ---------------------------------------------------------------------------
# summary.py --month CLI integration
# ---------------------------------------------------------------------------


class TestSummaryMonthCLI:
    def test_invalid_month_returns_error(self, tmp_path):
        log = _write_log(tmp_path, [])
        args = build_parser().parse_args(["--month", "not-a-month", "--events-file", str(log)])
        rc = cmd_month(args)
        assert rc == 1

    def test_missing_events_file_returns_error(self, tmp_path):
        args = build_parser().parse_args(
            ["--month", "2026-01", "--events-file", str(tmp_path / "nope.jsonl")]
        )
        rc = cmd_month(args)
        assert rc == 1

    def test_no_data_returns_zero(self, tmp_path):
        log = _write_log(tmp_path, [])
        args = build_parser().parse_args(["--month", "2026-01", "--events-file", str(log)])
        rc = cmd_month(args)
        assert rc == 0

    def test_successful_run_returns_zero(self, tmp_path):
        events = [_make_summary("2026-01-15", runtime_s=3600)]
        log = _write_log(tmp_path, events)
        args = build_parser().parse_args(["--month", "2026-01", "--events-file", str(log)])
        rc = cmd_month(args)
        assert rc == 0

    def test_main_month_flag(self, tmp_path):
        events = [_make_summary("2026-03-15", runtime_s=7200)]
        log = _write_log(tmp_path, events)
        rc = main(["--month", "2026-03", "--events-file", str(log)])
        assert rc == 0
