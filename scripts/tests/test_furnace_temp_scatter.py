"""Tests for scripts/furnace_temp_scatter.py."""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from pathlib import Path

import pytest
from furnace_temp_scatter import (
    FURNACE_SESSION_SCHEMA,
    FURNACE_SUMMARY_SCHEMA,
    OUTDOOR_SCHEMA,
    DailyScatterPoint,
    _parse_args,
    _print_summary,
    build_scatter_points,
    write_csv,
)


def _outdoor(timestamp: str, temperature_f: float) -> dict:
    return {
        "schema": OUTDOOR_SCHEMA,
        "ts": timestamp,
        "data": {
            "entity_id": "sensor.outdoor_temperature",
            "temperature_f": temperature_f,
            "timestamp": timestamp,
        },
    }


def _furnace(ended_at: str, duration_s: int | float | None) -> dict:
    return {
        "schema": FURNACE_SESSION_SCHEMA,
        "ts": ended_at,
        "data": {
            "ended_at": ended_at,
            "duration_s": duration_s,
        },
    }


def _summary(day: str, runtime_s: int, temp_f: float | None = None) -> dict:
    data = {"date": day, "total_furnace_runtime_s": runtime_s}
    if temp_f is not None:
        data["outdoor_temp_avg_f"] = temp_f
    return {"schema": FURNACE_SUMMARY_SCHEMA, "data": data}


def _write_log(tmp_path: Path, events: list[object]) -> Path:
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            if isinstance(event, str):
                output.write(event + "\n")
            else:
                output.write(json.dumps(event) + "\n")
    return path


class TestBuildScatterPoints:
    def test_aggregates_outdoor_readings_and_runtime_in_sorted_utc_days(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                _outdoor("2026-03-21T00:30:00-05:00", 30.0),
                _outdoor("2026-03-21T01:30:00-05:00", 40.0),
                _outdoor("2026-03-20T18:45:00-05:00", 40.0),
                _furnace("2026-03-21T01:00:00+00:00", 120),
                _furnace("2026-03-20T22:00:00+00:00", 60),
            ],
        )

        points = build_scatter_points(log)

        assert [point.date for point in points] == ["2026-03-20", "2026-03-21"]
        assert points[0].avg_temp_f == 40.0
        assert points[0].furnace_runtime_min == 1.0
        assert points[1].avg_temp_f == 35.0
        assert points[1].furnace_runtime_min == 2.0

    def test_summary_runtime_preserves_zero_and_wins_over_raw_fallback(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                _outdoor("2026-08-20T12:00:00+00:00", 74.0),
                _furnace("2026-08-20T13:00:00+00:00", 900),
                _summary("2026-08-20", 0),
                _outdoor("2026-08-21T12:00:00+00:00", 72.0),
                _summary("2026-08-21", 120),
            ],
        )

        points = build_scatter_points(log)

        assert [(point.date, point.furnace_runtime_min) for point in points] == [
            ("2026-08-20", 0.0),
            ("2026-08-21", 2.0),
        ]

    def test_zero_and_negative_temperatures_are_valid(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                _outdoor("2026-01-10T01:00:00+00:00", 0.0),
                _outdoor("2026-01-10T02:00:00+00:00", -4.0),
                _summary("2026-01-10", 60),
            ],
        )

        point = build_scatter_points(log)[0]

        assert point.avg_temp_f == -2.0
        assert point.furnace_runtime_min == 1.0

    def test_summary_temperature_is_fallback_when_raw_readings_are_absent(self, tmp_path):
        log = _write_log(tmp_path, [_summary("2026-01-10", 60, temp_f=28.36)])

        point = build_scatter_points(log)[0]

        assert point.avg_temp_f == 28.4

    def test_missing_measurement_is_a_partial_row(self, tmp_path):
        log = _write_log(tmp_path, [_outdoor("2026-01-10T01:00:00+00:00", 32.0)])

        point = build_scatter_points(log)[0]

        assert point.avg_temp_f == 32.0
        assert point.furnace_runtime_min is None

    def test_duplicate_and_malformed_records_do_not_change_totals(self, tmp_path):
        outdoor = _outdoor("2026-01-10T01:00:00+00:00", 32.0)
        furnace = _furnace("2026-01-10T02:00:00+00:00", 60)
        log = _write_log(
            tmp_path,
            [
                outdoor,
                outdoor,
                furnace,
                furnace,
                _furnace("not-a-timestamp", 90),
                _furnace("2026-01-10T03:00:00+00:00", None),
                "not json",
                {"schema": OUTDOOR_SCHEMA, "data": {"temperature_f": "32"}},
            ],
        )

        point = build_scatter_points(log)[0]

        assert point.avg_temp_f == 32.0
        assert point.furnace_runtime_min == 1.0

    def test_inclusive_date_filter_excludes_outside_days(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                _summary("2026-01-09", 60, 30.0),
                _summary("2026-01-10", 120, 32.0),
                _summary("2026-01-11", 180, 34.0),
            ],
        )

        points = build_scatter_points(log, date(2026, 1, 10), date(2026, 1, 10))

        assert [point.date for point in points] == ["2026-01-10"]

    def test_invalid_date_range_is_rejected(self, tmp_path):
        log = _write_log(tmp_path, [])

        with pytest.raises(ValueError, match="on or before"):
            build_scatter_points(log, date(2026, 1, 11), date(2026, 1, 10))


class TestCsvOutput:
    def test_writes_stable_header_numeric_values_and_blanks(self, tmp_path):
        output = tmp_path / "nested" / "scatter.csv"
        points = [
            DailyScatterPoint("2026-01-10", -2.0, 1.5),
            DailyScatterPoint("2026-01-11", 34.0, None),
        ]

        write_csv(points, output)

        with output.open(newline="", encoding="utf-8") as csv_file:
            rows = list(csv.DictReader(csv_file))
        assert rows == [
            {"date": "2026-01-10", "avg_temp_f": "-2.0", "furnace_runtime_min": "1.5"},
            {"date": "2026-01-11", "avg_temp_f": "34.0", "furnace_runtime_min": ""},
        ]

    def test_summary_reports_complete_and_partial_coverage(self):
        points = [
            DailyScatterPoint("2026-01-10", 32.0, 1.0),
            DailyScatterPoint("2026-01-11", 33.0, None),
        ]
        output = io.StringIO()

        _print_summary(points, file=output)

        assert "Days represented: 2" in output.getvalue()
        assert "Complete scatter points: 1" in output.getvalue()
        assert "Partial rows: 1" in output.getvalue()


class TestCli:
    def test_parse_args_accepts_explicit_range_and_paths(self):
        args = _parse_args(
            [
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
                "--log",
                "in.jsonl",
                "--out",
                "out.csv",
            ]
        )

        assert args.start == date(2026, 1, 1)
        assert args.end == date(2026, 1, 31)
        assert args.log == "in.jsonl"
        assert args.out == "out.csv"

    def test_parse_args_rejects_bad_date(self):
        with pytest.raises(SystemExit):
            _parse_args(["--start", "not-a-date"])
