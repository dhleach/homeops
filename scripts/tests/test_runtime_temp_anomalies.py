"""Tests for scripts/runtime_temp_anomalies.py."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from runtime_temp_anomalies import (
    DEFAULT_DAYS,
    SCHEMA,
    FloorDay,
    _parse_args,
    _resolve_range,
    build_report,
    fit_linear_model,
    load_floor_days,
    render_markdown,
)


def _summary(
    day: str,
    floor: str,
    runtime_s: int | float,
    outdoor_temp_f: float | None = 40.0,
) -> dict:
    data = {
        "date": day,
        "floor": floor,
        "total_runtime_s": runtime_s,
        "outdoor_temp_avg_f": outdoor_temp_f,
    }
    return {"schema": SCHEMA, "source": "consumer.v1", "data": data}


def _write_log(tmp_path: Path, events: list[object]) -> Path:
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            output.write(event if isinstance(event, str) else json.dumps(event))
            output.write("\n")
    return path


def _days(start: date, count: int) -> list[str]:
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


class TestLoadFloorDays:
    def test_filters_range_schema_and_invalid_measurements(self, tmp_path):
        start = date(2026, 1, 1)
        log = _write_log(
            tmp_path,
            [
                _summary("2025-12-31", "floor_1", 100, 30),
                _summary("2026-01-01", "floor_1", 100, 30),
                _summary("2026-01-02", "floor_1", 200, None),
                _summary("2026-01-03", "floor_1", -1, 30),
                {"schema": "other.event.v1", "data": {}},
                "not json",
            ],
        )

        rows = load_floor_days(log, start, date(2026, 1, 3))

        assert rows == [FloorDay(date(2026, 1, 1), "floor_1", 30.0, 100.0)]

    def test_last_valid_duplicate_wins(self, tmp_path):
        log = _write_log(
            tmp_path,
            [
                _summary("2026-01-01", "floor_1", 100, 30),
                _summary("2026-01-01", "floor_1", 250, 30),
            ],
        )

        rows = load_floor_days(log, date(2026, 1, 1), date(2026, 1, 1))

        assert rows[0].runtime_s == 250.0

    def test_missing_log_is_explicit(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="log file not found"):
            load_floor_days(tmp_path / "missing.jsonl", date(2026, 1, 1), date(2026, 1, 1))


class TestModel:
    def test_fits_known_line_and_r_squared(self):
        rows = [
            FloorDay(date(2026, 1, 1), "floor_1", 20, 5000),
            FloorDay(date(2026, 1, 2), "floor_1", 30, 4000),
            FloorDay(date(2026, 1, 3), "floor_1", 40, 3000),
        ]

        model = fit_linear_model(rows)

        assert model is not None
        assert model.slope_s_per_f == -100
        assert model.intercept_s == 7000
        assert model.r_squared == 1
        assert model.predict(35) == 3500

    def test_returns_none_for_insufficient_or_constant_temperature(self):
        one = [FloorDay(date(2026, 1, 1), "floor_1", 20, 5000)]
        constant = [
            FloorDay(date(2026, 1, 1), "floor_1", 20, 5000),
            FloorDay(date(2026, 1, 2), "floor_1", 20, 4000),
        ]

        assert fit_linear_model(one) is None
        assert fit_linear_model(constant) is None


class TestBuildReport:
    def test_flags_high_runtime_residual_and_preserves_direction(self):
        start = date(2026, 1, 1)
        rows = [
            FloorDay(start + timedelta(days=index), "floor_1", 20 + index, 5000 - index * 100)
            for index in range(14)
        ]
        rows.append(FloorDay(start + timedelta(days=14), "floor_1", 34, 7000))

        report = build_report(rows, start, start + timedelta(days=14), min_points=14)

        assert report["candidate_anomaly_count"] == 1
        anomaly = report["anomalies"][0]
        assert anomaly["day"] == "2026-01-15"
        assert anomaly["direction"] == "higher_than_expected"
        assert anomaly["score"] >= 2.5

    def test_linear_history_has_no_candidates(self):
        start = date(2026, 1, 1)
        rows = [
            FloorDay(start + timedelta(days=index), "floor_1", 20 + index, 5000 - index * 100)
            for index in range(14)
        ]

        report = build_report(rows, start, start + timedelta(days=13), min_points=14)

        assert report["candidate_anomaly_count"] == 0
        assert report["floors"][0]["status"] == "no_residual_variation"

    def test_insufficient_floor_is_visible(self):
        start = date(2026, 1, 1)
        rows = [FloorDay(start, "floor_2", 30, 1000)]

        report = build_report(rows, start, start, min_points=2)

        floor = report["floors"][0]
        assert floor["status"] == "insufficient_data"
        assert floor["sample_count"] == 1
        assert report["candidate_anomaly_count"] == 0

    def test_low_runtime_residual_is_not_operational_candidate(self):
        start = date(2026, 1, 1)
        rows = [
            FloorDay(start + timedelta(days=index), "floor_1", 20 + index, 5000 - index * 100)
            for index in range(14)
        ]
        rows.append(FloorDay(start + timedelta(days=14), "floor_1", 34, 0))

        report = build_report(rows, start, start + timedelta(days=14), min_points=14)

        assert report["candidate_anomaly_count"] == 0
        assert report["floors"][0]["anomalies"] == []

    def test_multiple_floors_are_sorted_and_anomalies_are_date_sorted(self):
        start = date(2026, 1, 1)
        rows = []
        for floor, offset in (("floor_2", 0), ("floor_1", 100)):
            rows.extend(
                FloorDay(
                    start + timedelta(days=index), floor, 20 + index, 5000 - index * 100 + offset
                )
                for index in range(14)
            )

        report = build_report(rows, start, start + timedelta(days=13), min_points=14)

        assert [floor["floor"] for floor in report["floors"]] == ["floor_1", "floor_2"]
        assert report["anomalies"] == []


class TestOutputAndCli:
    def test_markdown_contains_model_and_interpretation_guard(self):
        start = date(2026, 1, 1)
        report = build_report(
            [FloorDay(start, "floor_1", 30, 1000)],
            start,
            start,
            min_points=2,
        )
        output = io.StringIO()

        rendered = render_markdown(report, file=output)

        assert rendered + "\n" == output.getvalue()
        assert "Floor models" in rendered
        assert "insufficient_data" in rendered
        assert "not proof of an equipment fault" in rendered

    def test_resolve_range_defaults_to_requested_days(self):
        start, end = _resolve_range(days=DEFAULT_DAYS)

        assert (end - start).days == DEFAULT_DAYS - 1

    def test_resolve_range_requires_both_explicit_dates(self):
        with pytest.raises(ValueError, match="provided together"):
            _resolve_range(start=date(2026, 1, 1))

    def test_parse_args_accepts_model_options(self):
        args = _parse_args(
            [
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
                "--min-points",
                "10",
                "--threshold",
                "3",
                "--format",
                "json",
                "--log",
                "in.jsonl",
                "--out",
                "out.json",
            ]
        )

        assert args.start == date(2026, 1, 1)
        assert args.end == date(2026, 1, 31)
        assert args.min_points == 10
        assert args.threshold == 3.0
        assert args.format == "json"

    def test_parse_args_rejects_invalid_positive_values(self):
        with pytest.raises(SystemExit):
            _parse_args(["--min-points", "1"])
        with pytest.raises(SystemExit):
            _parse_args(["--threshold", "0"])
