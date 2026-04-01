"""Tests for scripts/query_floor_runtime.py."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from query_floor_runtime import (
    SCHEMA,
    _aggregate,
    _fmt_duration,
    _load_events,
    _print_table,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_event(floor: str, date_str: str, total_runtime_s: int = 3600) -> dict:
    return {
        "schema": SCHEMA,
        "source": "consumer.v1",
        "ts": f"{date_str}T00:00:05.000000+00:00",
        "data": {
            "floor": floor,
            "date": date_str,
            "total_calls": 3,
            "total_runtime_s": total_runtime_s,
            "avg_duration_s": total_runtime_s / 3,
            "max_duration_s": total_runtime_s,
            "outdoor_temp_avg_f": 35.0,
        },
    }


def write_log(tmp_path: Path, events: list[dict]) -> Path:
    log = tmp_path / "events.jsonl"
    with open(log, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")
    return log


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_zero_seconds(self):
        assert _fmt_duration(0) == "0m"

    def test_minutes_only(self):
        assert _fmt_duration(600) == "10m"

    def test_exactly_one_hour(self):
        assert _fmt_duration(3600) == "1h 00m"

    def test_hours_and_minutes(self):
        assert _fmt_duration(5400) == "1h 30m"

    def test_large_value(self):
        assert _fmt_duration(18000) == "5h 00m"

    def test_59_minutes(self):
        assert _fmt_duration(3540) == "59m"

    def test_one_minute(self):
        assert _fmt_duration(60) == "1m"


# ---------------------------------------------------------------------------
# _load_events
# ---------------------------------------------------------------------------


class TestLoadEvents:
    def test_loads_events_in_range(self, tmp_path):
        events = [
            make_event("floor_1", "2026-01-10"),
            make_event("floor_2", "2026-01-15"),
            make_event("floor_3", "2026-01-20"),
        ]
        log = write_log(tmp_path, events)
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), None)
        assert len(result) == 3

    def test_excludes_events_before_start(self, tmp_path):
        events = [
            make_event("floor_1", "2025-12-31"),
            make_event("floor_1", "2026-01-01"),
        ]
        log = write_log(tmp_path, events)
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), None)
        assert len(result) == 1
        assert result[0]["date"] == "2026-01-01"

    def test_excludes_events_after_end(self, tmp_path):
        events = [
            make_event("floor_1", "2026-01-31"),
            make_event("floor_1", "2026-02-01"),
        ]
        log = write_log(tmp_path, events)
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), None)
        assert len(result) == 1
        assert result[0]["date"] == "2026-01-31"

    def test_inclusive_start_and_end(self, tmp_path):
        events = [
            make_event("floor_1", "2026-01-01"),
            make_event("floor_1", "2026-01-31"),
        ]
        log = write_log(tmp_path, events)
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), None)
        assert len(result) == 2

    def test_floor_filter(self, tmp_path):
        events = [
            make_event("floor_1", "2026-01-10", 1800),
            make_event("floor_2", "2026-01-10", 2700),
            make_event("floor_3", "2026-01-10", 900),
        ]
        log = write_log(tmp_path, events)
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), "floor_2")
        assert len(result) == 1
        assert result[0]["floor"] == "floor_2"

    def test_skips_non_matching_schema(self, tmp_path):
        events = [
            {"schema": "homeops.consumer.furnace_daily_summary.v1", "data": {"date": "2026-01-10"}},
            make_event("floor_1", "2026-01-10"),
        ]
        log = write_log(tmp_path, events)
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), None)
        assert len(result) == 1

    def test_skips_malformed_json_lines(self, tmp_path):
        log = tmp_path / "events.jsonl"
        with open(log, "w") as f:
            f.write("not-json\n")
            f.write(json.dumps(make_event("floor_1", "2026-01-10")) + "\n")
            f.write("{broken\n")
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), None)
        assert len(result) == 1

    def test_empty_log(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("")
        result = _load_events(str(log), date(2026, 1, 1), date(2026, 1, 31), None)
        assert result == []

    def test_missing_log_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _load_events(
                str(tmp_path / "nonexistent.jsonl"), date(2026, 1, 1), date(2026, 1, 31), None
            )


# ---------------------------------------------------------------------------
# _aggregate
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_empty_returns_empty(self):
        assert _aggregate([]) == {}

    def test_single_floor_single_day(self):
        data = [{"floor": "floor_2", "date": "2026-01-10", "total_runtime_s": 3600}]
        result = _aggregate(data)
        assert "floor_2" in result
        assert result["floor_2"]["total_s"] == 3600
        assert result["floor_2"]["days"] == 1
        assert result["floor_2"]["avg_s"] == 3600
        assert result["floor_2"]["max_s"] == 3600

    def test_single_floor_multiple_days(self):
        data = [
            {"floor": "floor_1", "date": "2026-01-10", "total_runtime_s": 3600},
            {"floor": "floor_1", "date": "2026-01-11", "total_runtime_s": 1800},
            {"floor": "floor_1", "date": "2026-01-12", "total_runtime_s": 5400},
        ]
        result = _aggregate(data)
        f1 = result["floor_1"]
        assert f1["total_s"] == 10800
        assert f1["days"] == 3
        assert f1["avg_s"] == 3600
        assert f1["max_s"] == 5400

    def test_multiple_floors(self):
        data = [
            {"floor": "floor_1", "date": "2026-01-10", "total_runtime_s": 1800},
            {"floor": "floor_2", "date": "2026-01-10", "total_runtime_s": 3600},
            {"floor": "floor_3", "date": "2026-01-10", "total_runtime_s": 900},
        ]
        result = _aggregate(data)
        assert result["floor_1"]["total_s"] == 1800
        assert result["floor_2"]["total_s"] == 3600
        assert result["floor_3"]["total_s"] == 900

    def test_max_is_largest_single_day(self):
        data = [
            {"floor": "floor_2", "date": "2026-01-10", "total_runtime_s": 1000},
            {"floor": "floor_2", "date": "2026-01-11", "total_runtime_s": 9000},
            {"floor": "floor_2", "date": "2026-01-12", "total_runtime_s": 2000},
        ]
        result = _aggregate(data)
        assert result["floor_2"]["max_s"] == 9000

    def test_days_counts_unique_dates(self):
        data = [
            {"floor": "floor_1", "date": "2026-01-10", "total_runtime_s": 1800},
            {"floor": "floor_1", "date": "2026-01-10", "total_runtime_s": 900},  # duplicate date
        ]
        result = _aggregate(data)
        # days should still be 1 (unique date)
        assert result["floor_1"]["days"] == 1


# ---------------------------------------------------------------------------
# _print_table (smoke tests for output shape)
# ---------------------------------------------------------------------------


class TestPrintTable:
    def test_prints_header(self, capsys):
        aggregated = {
            "floor_2": {"total_s": 7200, "days": 2, "avg_s": 3600, "max_s": 4500},
        }
        _print_table(aggregated, date(2026, 1, 1), date(2026, 1, 31), None)
        out = capsys.readouterr().out
        assert "Floor" in out
        assert "Total Runtime" in out
        assert "Avg Daily" in out
        assert "Max Single Day" in out

    def test_shows_all_floors_when_no_filter(self, capsys):
        aggregated = {"floor_2": {"total_s": 7200, "days": 2, "avg_s": 3600, "max_s": 4500}}
        _print_table(aggregated, date(2026, 1, 1), date(2026, 1, 31), None)
        out = capsys.readouterr().out
        assert "floor_1" in out
        assert "floor_2" in out
        assert "floor_3" in out

    def test_shows_only_filtered_floor(self, capsys):
        aggregated = {"floor_2": {"total_s": 7200, "days": 2, "avg_s": 3600, "max_s": 4500}}
        _print_table(aggregated, date(2026, 1, 1), date(2026, 1, 31), "floor_2")
        out = capsys.readouterr().out
        assert "floor_2" in out
        assert "floor_1" not in out

    def test_no_data_message(self, capsys):
        _print_table({}, date(2026, 1, 1), date(2026, 1, 31), None)
        out = capsys.readouterr().out
        assert "No floor_daily_summary" in out

    def test_runtime_values_formatted(self, capsys):
        aggregated = {"floor_2": {"total_s": 3600, "days": 1, "avg_s": 3600, "max_s": 3600}}
        _print_table(aggregated, date(2026, 1, 1), date(2026, 1, 31), None)
        out = capsys.readouterr().out
        assert "1h 00m" in out
