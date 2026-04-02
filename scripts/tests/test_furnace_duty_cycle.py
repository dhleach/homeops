"""Tests for scripts/furnace_duty_cycle.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from furnace_duty_cycle import (
    SCHEMA,
    _compute,
    _fmt_duration,
    _load_and_clip,
    _parse_window_dt,
    _print_result,
)

WIN_START = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
WIN_END = datetime(2026, 1, 15, 23, 59, 59, tzinfo=UTC)


def make_session_event(ended_at: datetime, duration_s: int | None) -> dict:
    return {
        "schema": SCHEMA,
        "source": "consumer.v1",
        "ts": ended_at.isoformat(),
        "data": {
            "ended_at": ended_at.isoformat(),
            "entity_id": "binary_sensor.furnace_heating",
            "duration_s": duration_s,
        },
    }


def write_log(tmp_path: Path, events: list[dict]) -> Path:
    log = tmp_path / "events.jsonl"
    with open(log, "w") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")
    return log


# ---------------------------------------------------------------------------
# _parse_window_dt
# ---------------------------------------------------------------------------


class TestParseWindowDt:
    def test_date_only_start(self):
        dt = _parse_window_dt("2026-01-15")
        assert dt == datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)

    def test_date_only_end_of_day(self):
        dt = _parse_window_dt("2026-01-15", end_of_day=True)
        assert dt == datetime(2026, 1, 15, 23, 59, 59, tzinfo=UTC)

    def test_datetime_hhmm(self):
        dt = _parse_window_dt("2026-01-15T06:30")
        assert dt == datetime(2026, 1, 15, 6, 30, 0, tzinfo=UTC)

    def test_datetime_hhmmss(self):
        dt = _parse_window_dt("2026-01-15T06:30:45")
        assert dt == datetime(2026, 1, 15, 6, 30, 45, tzinfo=UTC)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_window_dt("not-a-date")


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_seconds_only(self):
        assert _fmt_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _fmt_duration(125) == "2m 05s"

    def test_hours_minutes_seconds(self):
        assert _fmt_duration(3661) == "1h 01m 01s"

    def test_zero(self):
        assert _fmt_duration(0) == "0s"


# ---------------------------------------------------------------------------
# _load_and_clip
# ---------------------------------------------------------------------------


class TestLoadAndClip:
    def test_session_fully_within_window(self, tmp_path):
        ended = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
        log = write_log(tmp_path, [make_session_event(ended, 1800)])
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert len(result) == 1
        assert result[0]["clipped_s"] == 1800
        assert result[0]["duration_s"] == 1800

    def test_session_outside_window_before(self, tmp_path):
        ended = datetime(2026, 1, 14, 23, 0, 0, tzinfo=UTC)
        log = write_log(tmp_path, [make_session_event(ended, 1800)])
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert result == []

    def test_session_outside_window_after(self, tmp_path):
        ended = datetime(2026, 1, 16, 10, 0, 0, tzinfo=UTC)
        log = write_log(tmp_path, [make_session_event(ended, 600)])
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert result == []

    def test_session_clips_at_start(self, tmp_path):
        # Session started 30 min before window start, ended 30 min after
        ended = datetime(2026, 1, 15, 0, 30, 0, tzinfo=UTC)
        duration_s = 3600  # started at 2026-01-14T23:30 — 30 min before window
        log = write_log(tmp_path, [make_session_event(ended, duration_s)])
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert len(result) == 1
        assert result[0]["clipped_s"] == pytest.approx(1800, abs=1)  # only 30 min in window

    def test_session_clips_at_end(self, tmp_path):
        # Session started 30 min before window end, ended 30 min after
        ended = datetime(2026, 1, 16, 0, 30, 0, tzinfo=UTC)
        duration_s = 3600  # started at 2026-01-15T23:30
        log = write_log(tmp_path, [make_session_event(ended, duration_s)])
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert len(result) == 1
        assert result[0]["clipped_s"] == pytest.approx(1799, abs=2)  # 23:30→23:59:59 = ~30min

    def test_skips_null_duration(self, tmp_path):
        ended = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
        log = write_log(tmp_path, [make_session_event(ended, None)])
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert result == []

    def test_skips_non_matching_schema(self, tmp_path):
        evt = {"schema": "homeops.consumer.heating_session_started.v1", "data": {}}
        log = write_log(tmp_path, [evt])
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert result == []

    def test_skips_malformed_json(self, tmp_path):
        log = tmp_path / "events.jsonl"
        with open(log, "w") as f:
            f.write("not-json\n")
            ended = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
            f.write(json.dumps(make_session_event(ended, 600)) + "\n")
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert len(result) == 1

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            _load_and_clip(str(tmp_path / "missing.jsonl"), WIN_START, WIN_END)

    def test_empty_log(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("")
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert result == []

    def test_multiple_sessions_all_clipped(self, tmp_path):
        events = [
            make_session_event(datetime(2026, 1, 15, 8, 0, 0, tzinfo=UTC), 1800),
            make_session_event(datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC), 3600),
            make_session_event(datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC), 900),
        ]
        log = write_log(tmp_path, events)
        result = _load_and_clip(str(log), WIN_START, WIN_END)
        assert len(result) == 3
        total = sum(s["clipped_s"] for s in result)
        assert total == pytest.approx(6300, abs=1)


# ---------------------------------------------------------------------------
# _compute
# ---------------------------------------------------------------------------


class TestCompute:
    def test_empty_sessions(self):
        stats = _compute([], WIN_START, WIN_END)
        assert stats["session_count"] == 0
        assert stats["total_on_s"] == 0.0
        assert stats["duty_cycle_pct"] == pytest.approx(0.0)

    def test_50_percent_duty_cycle(self):
        ws = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
        we = datetime(2026, 1, 15, 1, 0, 0, tzinfo=UTC)
        sessions = [{"clipped_s": 1800, "duration_s": 1800}]
        stats = _compute(sessions, ws, we)
        assert stats["duty_cycle_pct"] == pytest.approx(50.0, abs=0.1)
        assert stats["total_on_s"] == pytest.approx(1800, abs=1)
        assert stats["window_duration_s"] == pytest.approx(3600, abs=1)

    def test_100_percent(self):
        ws = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
        we = datetime(2026, 1, 15, 1, 0, 0, tzinfo=UTC)
        sessions = [{"clipped_s": 3600, "duration_s": 3600}]
        stats = _compute(sessions, ws, we)
        assert stats["duty_cycle_pct"] == pytest.approx(100.0, abs=0.1)

    def test_clipped_count(self):
        sessions = [
            {"clipped_s": 900, "duration_s": 1800},  # clipped
            {"clipped_s": 600, "duration_s": 600},  # not clipped
        ]
        stats = _compute(sessions, WIN_START, WIN_END)
        assert stats["clipped_count"] == 1

    def test_duty_cycle_full_day(self):
        # 6 hours on in a 24-hour window = 25%
        ws = datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
        we = datetime(2026, 1, 16, 0, 0, 0, tzinfo=UTC)
        sessions = [{"clipped_s": 21600, "duration_s": 21600}]  # 6h
        stats = _compute(sessions, ws, we)
        assert stats["duty_cycle_pct"] == pytest.approx(25.0, abs=0.1)


# ---------------------------------------------------------------------------
# _print_result (smoke)
# ---------------------------------------------------------------------------


class TestPrintResult:
    def test_prints_duty_cycle(self, capsys):
        stats = {
            "session_count": 3,
            "total_on_s": 5400,
            "window_duration_s": 86400,
            "duty_cycle_pct": 6.25,
            "clipped_count": 0,
        }
        _print_result(stats, WIN_START, WIN_END, [{"clipped_s": 5400, "duration_s": 5400}])
        out = capsys.readouterr().out
        assert "6.2%" in out
        assert "Duty cycle" in out

    def test_no_sessions_message(self, capsys):
        stats = {
            "session_count": 0,
            "total_on_s": 0,
            "window_duration_s": 86400,
            "duty_cycle_pct": 0.0,
            "clipped_count": 0,
        }
        _print_result(stats, WIN_START, WIN_END, [])
        out = capsys.readouterr().out
        assert "No heating_session_ended" in out
