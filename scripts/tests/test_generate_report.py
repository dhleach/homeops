"""Tests for scripts/generate_report.py.

Revision history:
  2026-08-22  Added coverage for composed chart data, explicit missing-data
              handling, deterministic HTML, and the report CLI contract.
"""

from __future__ import annotations

import json
from argparse import ArgumentTypeError
from datetime import date
from pathlib import Path

import pytest
from floor_runtime_trend import SCHEMA as FLOOR_SUMMARY_SCHEMA
from furnace_temp_scatter import (
    FURNACE_SESSION_SCHEMA,
    FURNACE_SUMMARY_SCHEMA,
    OUTDOOR_SCHEMA,
    DailyScatterPoint,
)
from generate_report import (
    DEFAULT_DAYS,
    DEFAULT_OUT,
    KNOWN_FLOORS,
    ReportData,
    _line_segments,
    _parse_args,
    _positive_days,
    build_report_data,
    main,
    render_floor_runtime_chart,
    render_report,
    render_scatter_chart,
    write_report,
)


def _floor(day: str, floor: str, runtime_s: object) -> dict:
    return {
        "schema": FLOOR_SUMMARY_SCHEMA,
        "data": {"date": day, "floor": floor, "total_runtime_s": runtime_s},
    }


def _outdoor(timestamp: str, temperature_f: float) -> dict:
    return {
        "schema": OUTDOOR_SCHEMA,
        "data": {"timestamp": timestamp, "temperature_f": temperature_f},
    }


def _furnace(ended_at: str, duration_s: int) -> dict:
    return {
        "schema": FURNACE_SESSION_SCHEMA,
        "data": {"ended_at": ended_at, "duration_s": duration_s},
    }


def _summary(day: str, runtime_s: int) -> dict:
    return {
        "schema": FURNACE_SUMMARY_SCHEMA,
        "data": {"date": day, "total_furnace_runtime_s": runtime_s},
    }


def _write_log(tmp_path: Path, events: list[object]) -> Path:
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            output.write(event if isinstance(event, str) else json.dumps(event))
            output.write("\n")
    return path


def _sample_data(tmp_path: Path) -> tuple[ReportData, date, date]:
    start = date(2026, 1, 10)
    end = date(2026, 1, 12)
    log = _write_log(
        tmp_path,
        [
            _floor("2026-01-10", "floor_1", 120),
            _floor("2026-01-10", "floor_2", 0),
            _outdoor("2026-01-10T12:00:00+00:00", 30.0),
            _summary("2026-01-10", 600),
            _outdoor("2026-01-11T12:00:00+00:00", 35.0),
            _furnace("2026-01-11T13:00:00+00:00", 120),
            _floor("2026-01-12", "floor_3", 180),
        ],
    )
    return build_report_data(log, start, end), start, end


def test_build_report_data_composes_floor_and_scatter_sources(tmp_path):
    data, _, _ = _sample_data(tmp_path)

    assert data.dates == ("2026-01-10", "2026-01-11", "2026-01-12")
    assert data.floor_runtime_min["floor_1"] == (2.0, None, None)
    assert data.floor_runtime_min["floor_2"] == (0.0, None, None)
    assert data.floor_runtime_min["floor_3"] == (None, None, 3.0)
    assert [(point.date, point.furnace_runtime_min) for point in data.scatter_points] == [
        ("2026-01-10", 10.0),
        ("2026-01-11", 2.0),
    ]


def test_build_report_data_uses_explicit_inclusive_range(tmp_path):
    log = _write_log(
        tmp_path,
        [
            _floor("2026-01-09", "floor_1", 60),
            _floor("2026-01-10", "floor_1", 120),
            _floor("2026-01-11", "floor_1", 180),
        ],
    )

    data = build_report_data(log, date(2026, 1, 10), date(2026, 1, 10))

    assert data.dates == ("2026-01-10",)
    assert data.floor_runtime_min["floor_1"] == (2.0,)


def test_build_report_data_skips_malformed_floor_records(tmp_path):
    log = _write_log(
        tmp_path,
        [
            {"schema": FLOOR_SUMMARY_SCHEMA, "data": []},
            {"schema": FLOOR_SUMMARY_SCHEMA, "data": {"date": None, "floor": "floor_1"}},
            _floor("2026-01-10", "floor_1", 60),
        ],
    )

    data = build_report_data(log, date(2026, 1, 10), date(2026, 1, 10))

    assert data.floor_runtime_min["floor_1"] == (1.0,)


def test_build_report_data_rejects_reverse_range(tmp_path):
    log = _write_log(tmp_path, [])

    with pytest.raises(ValueError, match="on or before"):
        build_report_data(log, date(2026, 1, 11), date(2026, 1, 10))


def test_line_segments_preserve_missing_value_gaps():
    assert _line_segments((1.0, None, 2.0, 3.0, None)) == [
        [(0, 1.0)],
        [(2, 2.0), (3, 3.0)],
    ]


def test_floor_runtime_chart_contains_series_and_zero_point(tmp_path):
    data, _, _ = _sample_data(tmp_path)

    chart = render_floor_runtime_chart(data)

    assert 'aria-label="Daily floor runtime"' in chart
    assert 'data-floor="floor_1"' in chart
    assert 'data-floor="floor_2"' in chart
    assert 'data-floor="floor_3"' in chart
    assert "2.0 minutes" in chart
    assert "0.0 minutes" in chart


def test_floor_runtime_chart_has_explicit_empty_state():
    data = ReportData(tuple(), {floor: tuple() for floor in KNOWN_FLOORS}, tuple())

    chart = render_floor_runtime_chart(data)

    assert "No floor runtime data in the selected period." in chart
    assert '<polyline class="series"' not in chart


def test_scatter_coverage_separates_complete_and_partial_rows():
    data = ReportData(
        ("2026-01-10", "2026-01-11"),
        {floor: (None, None) for floor in KNOWN_FLOORS},
        (
            DailyScatterPoint("2026-01-10", 30.0, 10.0),
            DailyScatterPoint("2026-01-11", 35.0, None),
        ),
    )

    chart = render_scatter_chart(data)

    assert len(data.complete_scatter_points) == 1
    assert data.partial_scatter_rows == 1
    assert chart.count('class="scatter-point"') == 1
    assert "2026-01-10" in chart
    assert "2026-01-11" not in chart


def test_scatter_chart_has_explicit_empty_state():
    data = ReportData(
        ("2026-01-10",),
        {floor: (None,) for floor in KNOWN_FLOORS},
        (DailyScatterPoint("2026-01-10", 30.0, None),),
    )

    chart = render_scatter_chart(data)

    assert "No complete scatter points in the selected period." in chart
    assert 'class="scatter-point"' not in chart


def test_render_report_is_self_contained_and_exposes_coverage(tmp_path):
    data, start, end = _sample_data(tmp_path)

    report = render_report(data, start, end)

    assert report.startswith("<!doctype html>")
    assert report.count('<svg class="chart"') == 2
    assert "Daily floor runtime" in report
    assert "Outdoor temperature vs furnace runtime" in report
    assert "Complete scatter points</span><strong>2</strong>" in report
    assert "Partial scatter rows</span><strong>0</strong>" in report
    assert "2026-01-12" in report
    assert "<script" not in report
    assert "https://" not in report


def test_render_report_is_deterministic(tmp_path):
    data, start, end = _sample_data(tmp_path)

    assert render_report(data, start, end) == render_report(data, start, end)


def test_write_report_creates_parent_and_matches_renderer(tmp_path):
    data, start, end = _sample_data(tmp_path)
    output = tmp_path / "nested" / "reports" / "hvac_trend.html"

    write_report(data, start, end, output)

    assert output.read_text(encoding="utf-8") == render_report(data, start, end)


def test_cli_defaults_and_explicit_paths():
    defaults = _parse_args([])
    explicit = _parse_args(
        [
            "--days",
            "14",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-31",
            "--log",
            "in.jsonl",
            "--out",
            "out.html",
        ]
    )

    assert defaults.days == DEFAULT_DAYS
    assert defaults.out == DEFAULT_OUT
    assert explicit.start == date(2026, 1, 1)
    assert explicit.end == date(2026, 1, 31)
    assert explicit.log == "in.jsonl"
    assert explicit.out == "out.html"


def test_cli_generates_report_and_rejects_incomplete_range(tmp_path):
    log = _write_log(tmp_path, [_floor("2026-01-10", "floor_1", 60)])
    output = tmp_path / "report.html"

    assert (
        main(
            [
                "--start",
                "2026-01-10",
                "--end",
                "2026-01-10",
                "--log",
                str(log),
                "--out",
                str(output),
            ]
        )
        == 0
    )
    assert output.exists()
    assert main(["--start", "2026-01-10", "--log", str(log), "--out", str(output)]) == 2


def test_positive_days_rejects_zero():
    with pytest.raises(ArgumentTypeError):
        _positive_days("0")
