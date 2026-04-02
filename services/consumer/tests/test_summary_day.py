"""Tests for summary.py --day flag (load_day_summary, compute_day_from_raw, cmd_day)."""

from __future__ import annotations

import json
from pathlib import Path

from summary import cmd_day, compute_day_from_raw, load_day_summary

TARGET = "2026-01-15"


def _make_furnace_summary(date_str: str, runtime_s: int = 7200, sessions: int = 4) -> dict:
    return {
        "schema": "homeops.consumer.furnace_daily_summary.v1",
        "source": "consumer.v1",
        "ts": f"{date_str}T00:00:05+00:00",
        "data": {
            "date": date_str,
            "total_furnace_runtime_s": runtime_s,
            "session_count": sessions,
            "per_floor_runtime_s": {"floor_1": 3600, "floor_2": 2700, "floor_3": 1800},
            "per_floor_session_count": {"floor_1": 2, "floor_2": 1, "floor_3": 1},
            "outdoor_temp_min_f": 25.0,
            "outdoor_temp_max_f": 38.0,
            "outdoor_temp_avg_f": 31.5,
            "per_floor_avg_setpoint_f": {"floor_1": None, "floor_2": None, "floor_3": None},
            "warnings_triggered": {
                "floor_2_long_call": 0,
                "floor_2_escalation": 0,
                "floor_no_response": 0,
                "zone_slow_to_heat": 0,
                "observer_silence": 0,
                "setpoint_miss": 0,
            },
        },
    }


def _make_raw_event(schema: str, ts: str, data: dict) -> dict:
    return {"schema": schema, "source": "consumer.v1", "ts": ts, "data": data}


def write_log(tmp_path: Path, events: list[dict]) -> str:
    log = tmp_path / "events.jsonl"
    with open(log, "w") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")
    return str(log)


# ---------------------------------------------------------------------------
# load_day_summary
# ---------------------------------------------------------------------------


class TestLoadDaySummary:
    def test_finds_matching_date(self, tmp_path):
        log = write_log(tmp_path, [_make_furnace_summary(TARGET)])
        result = load_day_summary(log, TARGET)
        assert result is not None
        assert result["date"] == TARGET
        assert result["total_furnace_runtime_s"] == 7200

    def test_returns_none_when_no_match(self, tmp_path):
        log = write_log(tmp_path, [_make_furnace_summary("2026-01-14")])
        result = load_day_summary(log, TARGET)
        assert result is None

    def test_returns_none_on_empty_log(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("")
        result = load_day_summary(str(log), TARGET)
        assert result is None

    def test_returns_none_on_missing_file(self, tmp_path):
        result = load_day_summary(str(tmp_path / "missing.jsonl"), TARGET)
        assert result is None

    def test_skips_non_summary_events(self, tmp_path):
        events = [
            _make_raw_event(
                "homeops.consumer.heating_session_ended.v1",
                f"{TARGET}T10:00:00+00:00",
                {"duration_s": 600, "ended_at": f"{TARGET}T10:00:00+00:00"},
            ),
            _make_furnace_summary(TARGET),
        ]
        log = write_log(tmp_path, events)
        result = load_day_summary(log, TARGET)
        assert result is not None
        assert result["total_furnace_runtime_s"] == 7200

    def test_multiple_dates_returns_correct_one(self, tmp_path):
        events = [
            _make_furnace_summary("2026-01-13"),
            _make_furnace_summary(TARGET, runtime_s=9000),
            _make_furnace_summary("2026-01-16"),
        ]
        log = write_log(tmp_path, events)
        result = load_day_summary(log, TARGET)
        assert result is not None
        assert result["total_furnace_runtime_s"] == 9000

    def test_skips_malformed_json(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        with open(log_path, "w") as f:
            f.write("not-json\n")
            f.write(json.dumps(_make_furnace_summary(TARGET)) + "\n")
        result = load_day_summary(str(log_path), TARGET)
        assert result is not None


# ---------------------------------------------------------------------------
# compute_day_from_raw
# ---------------------------------------------------------------------------


class TestComputeDayFromRaw:
    def test_empty_log_returns_zeros(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        log_path.write_text("")
        result = compute_day_from_raw(str(log_path), TARGET)
        assert result["date"] == TARGET
        assert result["total_furnace_runtime_s"] == 0
        assert result["session_count"] == 0

    def test_counts_heating_sessions(self, tmp_path):
        events = [
            _make_raw_event(
                "homeops.consumer.heating_session_ended.v1",
                f"{TARGET}T08:00:00+00:00",
                {"duration_s": 1800, "ended_at": f"{TARGET}T08:00:00+00:00"},
            ),
            _make_raw_event(
                "homeops.consumer.heating_session_ended.v1",
                f"{TARGET}T14:00:00+00:00",
                {"duration_s": 2700, "ended_at": f"{TARGET}T14:00:00+00:00"},
            ),
        ]
        log = write_log(tmp_path, events)
        result = compute_day_from_raw(log, TARGET)
        assert result["total_furnace_runtime_s"] == 4500
        assert result["session_count"] == 2

    def test_outdoor_temp_aggregated(self, tmp_path):
        events = [
            _make_raw_event(
                "homeops.consumer.outdoor_temp_updated.v1",
                f"{TARGET}T06:00:00+00:00",
                {"temperature_f": 28.0},
            ),
            _make_raw_event(
                "homeops.consumer.outdoor_temp_updated.v1",
                f"{TARGET}T12:00:00+00:00",
                {"temperature_f": 40.0},
            ),
        ]
        log = write_log(tmp_path, events)
        result = compute_day_from_raw(log, TARGET)
        assert result["outdoor_temp_min_f"] == 28.0
        assert result["outdoor_temp_max_f"] == 40.0

    def test_excludes_other_dates(self, tmp_path):
        events = [
            _make_raw_event(
                "homeops.consumer.heating_session_ended.v1",
                "2026-01-14T23:00:00+00:00",
                {"duration_s": 3600, "ended_at": "2026-01-14T23:00:00+00:00"},
            ),
            _make_raw_event(
                "homeops.consumer.heating_session_ended.v1",
                f"{TARGET}T10:00:00+00:00",
                {"duration_s": 900, "ended_at": f"{TARGET}T10:00:00+00:00"},
            ),
        ]
        log = write_log(tmp_path, events)
        result = compute_day_from_raw(log, TARGET)
        assert result["total_furnace_runtime_s"] == 900
        assert result["session_count"] == 1

    def test_warnings_counted(self, tmp_path):
        events = [
            _make_raw_event(
                "homeops.consumer.floor_2_long_call_warning.v1",
                f"{TARGET}T11:00:00+00:00",
                {"floor": "floor_2", "elapsed_s": 2800},
            ),
            _make_raw_event(
                "homeops.consumer.observer_silence_warning.v1",
                f"{TARGET}T15:00:00+00:00",
                {"silence_s": 700},
            ),
        ]
        log = write_log(tmp_path, events)
        result = compute_day_from_raw(log, TARGET)
        assert result["warnings_triggered"]["floor_2_long_call"] == 1
        assert result["warnings_triggered"]["observer_silence"] == 1


# ---------------------------------------------------------------------------
# cmd_day (integration via --events-file)
# ---------------------------------------------------------------------------


class TestCmdDay:
    def _make_args(self, day: str, events_file: str):
        import argparse

        return argparse.Namespace(
            day=day,
            events_file=events_file,
            ssh_key="/home/node/.openclaw/home-config/.ssh/id_ed25519",
            ssh_host="bob@100.115.21.72",
            remote_events="/home/leachd/repos/homeops/state/consumer/events.jsonl",
        )

    def test_day_from_daily_summary(self, tmp_path, capsys):
        log = write_log(tmp_path, [_make_furnace_summary(TARGET)])
        args = self._make_args(TARGET, str(log))
        rc = cmd_day(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "2026-01-15" in out
        assert "daily summary" in out

    def test_day_falls_back_to_raw(self, tmp_path, capsys):
        events = [
            _make_raw_event(
                "homeops.consumer.heating_session_ended.v1",
                f"{TARGET}T10:00:00+00:00",
                {"duration_s": 1800, "ended_at": f"{TARGET}T10:00:00+00:00"},
            )
        ]
        log = write_log(tmp_path, events)
        args = self._make_args(TARGET, str(log))
        rc = cmd_day(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "raw events" in out

    def test_invalid_date_returns_error(self, tmp_path, capsys):
        log = write_log(tmp_path, [])
        args = self._make_args("not-a-date", str(log))
        rc = cmd_day(args)
        assert rc == 1

    def test_missing_events_file_returns_error(self, tmp_path, capsys):
        import argparse

        args = argparse.Namespace(
            day=TARGET,
            events_file=str(tmp_path / "missing.jsonl"),
            ssh_key="",
            ssh_host="",
            remote_events="",
        )
        rc = cmd_day(args)
        assert rc == 1
