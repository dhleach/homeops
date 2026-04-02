"""Tests for scripts/validate_floor_aggregation.py."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from validate_floor_aggregation import (
    DAILY_SUMMARY_SCHEMA,
    FLOOR_CALL_SCHEMA,
    load_log,
    print_report,
    validate,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_floor_call(floor: str, date_str: str, duration_s: int) -> dict:
    entity = f"binary_sensor.{floor}_heating_call"
    return {
        "schema": FLOOR_CALL_SCHEMA,
        "source": "consumer.v1",
        "ts": f"{date_str}T10:00:00+00:00",
        "data": {
            "floor": floor,
            "entity_id": entity,
            "ended_at": f"{date_str}T10:00:00+00:00",
            "duration_s": duration_s,
        },
    }


def make_daily_summary(floor: str, date_str: str, total_runtime_s: int) -> dict:
    return {
        "schema": DAILY_SUMMARY_SCHEMA,
        "source": "consumer.v1",
        "ts": f"{date_str}T23:59:59+00:00",
        "data": {
            "floor": floor,
            "date": date_str,
            "total_calls": 2,
            "total_runtime_s": total_runtime_s,
            "avg_duration_s": total_runtime_s / 2,
            "max_duration_s": total_runtime_s,
            "outdoor_temp_avg_f": 35.0,
        },
    }


def write_log(tmp_path: Path, events: list[dict]) -> str:
    log = tmp_path / "events.jsonl"
    with open(log, "w") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")
    return str(log)


# ---------------------------------------------------------------------------
# load_log
# ---------------------------------------------------------------------------


class TestLoadLog:
    def test_empty_log(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text("")
        raw, summary = load_log(str(log))
        assert raw == {}
        assert summary == {}

    def test_loads_floor_calls(self, tmp_path):
        events = [
            make_floor_call("floor_2", "2026-01-15", 1800),
            make_floor_call("floor_2", "2026-01-15", 900),
        ]
        log = write_log(tmp_path, events)
        raw, _ = load_log(log)
        assert raw[("floor_2", "2026-01-15")] == 2700

    def test_loads_daily_summaries(self, tmp_path):
        events = [make_daily_summary("floor_2", "2026-01-15", 2700)]
        log = write_log(tmp_path, events)
        _, summary = load_log(log)
        assert summary[("floor_2", "2026-01-15")] == 2700

    def test_multiple_floors_multiple_days(self, tmp_path):
        events = [
            make_floor_call("floor_1", "2026-01-15", 1200),
            make_floor_call("floor_2", "2026-01-15", 3600),
            make_floor_call("floor_1", "2026-01-16", 600),
            make_daily_summary("floor_1", "2026-01-15", 1200),
            make_daily_summary("floor_2", "2026-01-15", 3600),
        ]
        log = write_log(tmp_path, events)
        raw, summary = load_log(log)
        assert raw[("floor_1", "2026-01-15")] == 1200
        assert raw[("floor_2", "2026-01-15")] == 3600
        assert raw[("floor_1", "2026-01-16")] == 600
        assert summary[("floor_1", "2026-01-15")] == 1200
        assert summary[("floor_2", "2026-01-15")] == 3600

    def test_skips_null_duration(self, tmp_path):
        evt = {
            "schema": FLOOR_CALL_SCHEMA,
            "ts": "2026-01-15T10:00:00+00:00",
            "data": {
                "floor": "floor_1",
                "ended_at": "2026-01-15T10:00:00+00:00",
                "duration_s": None,
            },
        }
        log = write_log(tmp_path, [evt])
        raw, _ = load_log(log)
        assert raw == {}

    def test_date_filter_start(self, tmp_path):
        events = [
            make_floor_call("floor_1", "2026-01-14", 3600),
            make_floor_call("floor_1", "2026-01-15", 1800),
        ]
        log = write_log(tmp_path, events)
        raw, _ = load_log(log, start=date(2026, 1, 15))
        assert ("floor_1", "2026-01-14") not in raw
        assert raw[("floor_1", "2026-01-15")] == 1800

    def test_date_filter_end(self, tmp_path):
        events = [
            make_floor_call("floor_1", "2026-01-15", 1800),
            make_floor_call("floor_1", "2026-01-16", 3600),
        ]
        log = write_log(tmp_path, events)
        raw, _ = load_log(log, end=date(2026, 1, 15))
        assert raw[("floor_1", "2026-01-15")] == 1800
        assert ("floor_1", "2026-01-16") not in raw

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            load_log(str(tmp_path / "missing.jsonl"))

    def test_skips_malformed_json(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        with open(log_path, "w") as f:
            f.write("not-json\n")
            f.write(json.dumps(make_floor_call("floor_1", "2026-01-15", 600)) + "\n")
        raw, _ = load_log(str(log_path))
        assert raw[("floor_1", "2026-01-15")] == 600


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_matching_totals_no_mismatches(self):
        raw = {("floor_2", "2026-01-15"): 2700}
        summary = {("floor_2", "2026-01-15"): 2700}
        assert validate(raw, summary) == []

    def test_mismatch_detected(self):
        raw = {("floor_2", "2026-01-15"): 2700}
        summary = {("floor_2", "2026-01-15"): 3000}
        mismatches = validate(raw, summary)
        assert len(mismatches) == 1
        m = mismatches[0]
        assert m["floor"] == "floor_2"
        assert m["date"] == "2026-01-15"
        assert m["raw_s"] == 2700
        assert m["summary_s"] == 3000
        assert m["delta_s"] == 300

    def test_missing_raw_counts_as_zero(self):
        raw = {}
        summary = {("floor_1", "2026-01-15"): 1800}
        mismatches = validate(raw, summary)
        assert len(mismatches) == 1
        assert mismatches[0]["raw_s"] == 0

    def test_summary_only_days_validated(self):
        """Days with raw events but no summary are not validated."""
        raw = {("floor_1", "2026-01-16"): 9999}
        summary = {("floor_1", "2026-01-15"): 1800}
        # raw has floor_1/2026-01-16 but summary does not → only 2026-01-15 checked
        mismatches = validate(raw, summary)
        assert len(mismatches) == 1  # 0 != 1800 for 2026-01-15
        assert mismatches[0]["date"] == "2026-01-15"

    def test_multiple_floors_one_mismatch(self):
        raw = {
            ("floor_1", "2026-01-15"): 1200,
            ("floor_2", "2026-01-15"): 2700,
        }
        summary = {
            ("floor_1", "2026-01-15"): 1200,
            ("floor_2", "2026-01-15"): 3000,  # mismatch
        }
        mismatches = validate(raw, summary)
        assert len(mismatches) == 1
        assert mismatches[0]["floor"] == "floor_2"

    def test_all_match_empty_mismatches(self):
        raw = {
            ("floor_1", "2026-01-15"): 1200,
            ("floor_2", "2026-01-15"): 2700,
            ("floor_3", "2026-01-15"): 900,
        }
        summary = dict(raw)
        assert validate(raw, summary) == []


# ---------------------------------------------------------------------------
# print_report smoke tests
# ---------------------------------------------------------------------------


class TestPrintReport:
    def test_pass_output(self, capsys):
        summary = {("floor_1", "2026-01-15"): 1800}
        raw = {("floor_1", "2026-01-15"): 1800}
        print_report([], summary, raw)
        out = capsys.readouterr().out
        assert "✅" in out
        assert "zero mismatches" in out

    def test_fail_output(self, capsys):
        summary = {("floor_2", "2026-01-15"): 3000}
        raw = {("floor_2", "2026-01-15"): 2700}
        mismatches = [
            {
                "floor": "floor_2",
                "date": "2026-01-15",
                "raw_s": 2700,
                "summary_s": 3000,
                "delta_s": 300,
            }
        ]
        print_report(mismatches, summary, raw)
        out = capsys.readouterr().out
        assert "❌" in out
        assert "floor_2" in out

    def test_raw_only_days_noted(self, capsys):
        summary = {("floor_1", "2026-01-15"): 1800}
        raw = {("floor_1", "2026-01-15"): 1800, ("floor_1", "2026-01-16"): 900}
        print_report([], summary, raw)
        out = capsys.readouterr().out
        assert "2026-01-16" in out
        assert "not validated" in out
