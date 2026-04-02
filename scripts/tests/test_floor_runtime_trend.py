"""Tests for scripts/floor_runtime_trend.py."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from floor_runtime_trend import (
    DEFAULT_DAYS,
    SCHEMA,
    _all_dates,
    _fmt_duration,
    _fmt_temp,
    _load_floor_summaries,
    _parse_args,
    _print_all_floors_table,
    _print_single_floor_table,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _make_event(floor: str, date_str: str, runtime_s: int = 3600, **kwargs) -> dict:
    return {
        "schema": SCHEMA,
        "source": "consumer.v1",
        "ts": f"{date_str}T00:00:05+00:00",
        "data": {
            "floor": floor,
            "date": date_str,
            "total_calls": kwargs.get("total_calls", 3),
            "total_runtime_s": runtime_s,
            "avg_duration_s": kwargs.get("avg_duration_s", runtime_s / 3),
            "max_duration_s": kwargs.get("max_duration_s", runtime_s),
            "outdoor_temp_avg_f": kwargs.get("outdoor_temp_avg_f", 40.0),
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
    def test_zero_returns_zero_m(self):
        assert _fmt_duration(0) == "0m"

    def test_none_returns_dash(self):
        assert _fmt_duration(None) == "—"

    def test_minutes_only(self):
        assert _fmt_duration(900) == "15m"

    def test_hours_and_minutes(self):
        assert _fmt_duration(3690) == "1h 01m"

    def test_exact_hour(self):
        assert _fmt_duration(3600) == "1h 00m"

    def test_large_value(self):
        assert _fmt_duration(7320) == "2h 02m"


# ---------------------------------------------------------------------------
# _fmt_temp
# ---------------------------------------------------------------------------


class TestFmtTemp:
    def test_none_returns_dash(self):
        assert _fmt_temp(None) == "—"

    def test_rounds_to_int(self):
        assert _fmt_temp(40.6) == "41°F"

    def test_exact_value(self):
        assert _fmt_temp(32.0) == "32°F"


# ---------------------------------------------------------------------------
# _all_dates
# ---------------------------------------------------------------------------


class TestAllDates:
    def test_length(self):
        dates = _all_dates(7)
        assert len(dates) == 7

    def test_newest_first(self):
        dates = _all_dates(5)
        assert dates[0] == _today()
        assert dates[1] == _days_ago(1)

    def test_oldest_last(self):
        dates = _all_dates(5)
        assert dates[-1] == _days_ago(4)


# ---------------------------------------------------------------------------
# _load_floor_summaries
# ---------------------------------------------------------------------------


class TestLoadFloorSummaries:
    def test_loads_events_within_window(self, tmp_path):
        events = [
            _make_event("floor_2", _days_ago(0), 3600),
            _make_event("floor_2", _days_ago(1), 4200),
        ]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        assert _today() in rows
        assert _days_ago(1) in rows

    def test_excludes_events_outside_window(self, tmp_path):
        old_date = _days_ago(60)
        events = [_make_event("floor_2", old_date, 3600)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        assert old_date not in rows

    def test_floor_filter_applies(self, tmp_path):
        events = [
            _make_event("floor_1", _days_ago(0), 1800),
            _make_event("floor_2", _days_ago(0), 3600),
        ]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_2")
        assert "floor_2" in rows.get(_today(), {})
        assert "floor_1" not in rows.get(_today(), {})

    def test_skips_non_matching_schema(self, tmp_path):
        events = [
            {"schema": "homeops.consumer.furnace_daily_summary.v1", "data": {"date": _today()}},
            _make_event("floor_2", _today(), 3600),
        ]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        assert "floor_2" in rows.get(_today(), {})

    def test_missing_log_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _load_floor_summaries(str(tmp_path / "nonexistent.jsonl"), days=30)

    def test_skips_bad_json_lines(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("not json\n" + json.dumps(_make_event("floor_2", _today(), 3600)) + "\n")
        rows = _load_floor_summaries(str(log), days=30)
        assert "floor_2" in rows.get(_today(), {})


# ---------------------------------------------------------------------------
# _print_all_floors_table
# ---------------------------------------------------------------------------


class TestPrintAllFloorsTable:
    def test_header_row_present(self, tmp_path):
        events = [_make_event("floor_2", _days_ago(0), 3600)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        buf = io.StringIO()
        _print_all_floors_table(rows, days=30, file=buf)
        output = buf.getvalue()
        assert "Floor 1" in output
        assert "Floor 2" in output
        assert "Floor 3" in output

    def test_today_row_present(self, tmp_path):
        events = [_make_event("floor_2", _days_ago(0), 3600)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        buf = io.StringIO()
        _print_all_floors_table(rows, days=30, file=buf)
        assert _today() in buf.getvalue()

    def test_runtime_formatted_in_output(self, tmp_path):
        events = [_make_event("floor_2", _days_ago(0), 7200)]  # 2h 00m
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        buf = io.StringIO()
        _print_all_floors_table(rows, days=30, file=buf)
        assert "2h 00m" in buf.getvalue()

    def test_outdoor_temp_shown(self, tmp_path):
        events = [_make_event("floor_1", _days_ago(0), 1800, outdoor_temp_avg_f=35.0)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        buf = io.StringIO()
        _print_all_floors_table(rows, days=30, file=buf)
        assert "35°F" in buf.getvalue()

    def test_no_data_shows_message(self, tmp_path):
        log = _write_log(tmp_path, [])
        rows = _load_floor_summaries(str(log), days=30)
        buf = io.StringIO()
        _print_all_floors_table(rows, days=30, file=buf)
        assert "No data" in buf.getvalue()


# ---------------------------------------------------------------------------
# _print_single_floor_table
# ---------------------------------------------------------------------------


class TestPrintSingleFloorTable:
    def test_floor_label_in_header(self, tmp_path):
        events = [_make_event("floor_2", _days_ago(0), 3600, total_calls=5)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_2")
        buf = io.StringIO()
        _print_single_floor_table(rows, "floor_2", days=30, file=buf)
        assert "Floor 2" in buf.getvalue()

    def test_call_count_shown(self, tmp_path):
        events = [_make_event("floor_2", _days_ago(0), 3600, total_calls=7)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_2")
        buf = io.StringIO()
        _print_single_floor_table(rows, "floor_2", days=30, file=buf)
        assert "7" in buf.getvalue()

    def test_no_data_for_floor(self, tmp_path):
        log = _write_log(tmp_path, [])
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_1")
        buf = io.StringIO()
        _print_single_floor_table(rows, "floor_1", days=30, file=buf)
        assert "No data" in buf.getvalue()


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_default_days(self):
        args = _parse_args([])
        assert args.days == DEFAULT_DAYS

    def test_custom_days(self):
        args = _parse_args(["--days", "14"])
        assert args.days == 14

    def test_floor_filter(self):
        args = _parse_args(["--floor", "floor_2"])
        assert args.floor == "floor_2"

    def test_no_floor_is_none(self):
        args = _parse_args([])
        assert args.floor is None
