"""Tests for scripts/temp_correlation.py."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from pathlib import Path

from temp_correlation import (
    DEFAULT_DAYS,
    SCHEMA,
    _interpret_correlation,
    _load_floor_summaries,
    _parse_args,
    _pearson_r,
    _print_all_floors_correlation,
    _print_single_floor_correlation,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _make_event(
    floor: str,
    date_str: str,
    runtime_s: int = 3600,
    temp_f: float | None = 40.0,
    **kwargs,
) -> dict:
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
            "outdoor_temp_avg_f": temp_f,
        },
    }


def _write_log(tmp_path: Path, events: list[dict]) -> Path:
    log = tmp_path / "events.jsonl"
    with open(log, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")
    return log


# ---------------------------------------------------------------------------
# _fmt_duration, _fmt_temp (already tested in test_floor_runtime_trend.py)
# ---------------------------------------------------------------------------


class TestPearsonR:
    def test_perfect_positive_correlation(self):
        # y = 2x: perfect linear relationship
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [2.0, 4.0, 6.0, 8.0, 10.0]
        r = _pearson_r(x, y)
        assert r is not None
        assert abs(r - 1.0) < 0.001  # r ≈ 1.0

    def test_perfect_negative_correlation(self):
        # y = -x: perfect negative relationship
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [5.0, 4.0, 3.0, 2.0, 1.0]
        r = _pearson_r(x, y)
        assert r is not None
        assert abs(r - (-1.0)) < 0.001  # r ≈ -1.0

    def test_no_correlation(self):
        # x and y are independent
        x = [1.0, 2.0, 3.0, 4.0, 5.0]
        y = [3.0, 1.0, 4.0, 1.0, 5.0]
        r = _pearson_r(x, y)
        assert r is not None
        # r should be close to 0
        assert abs(r) < 0.5

    def test_insufficient_data_one_point(self):
        r = _pearson_r([1.0], [2.0])
        assert r is None

    def test_insufficient_data_empty(self):
        r = _pearson_r([], [])
        assert r is None

    def test_mismatched_lengths(self):
        r = _pearson_r([1.0, 2.0], [3.0, 4.0, 5.0])
        assert r is None

    def test_zero_variance_x(self):
        # All x values the same
        x = [5.0, 5.0, 5.0, 5.0]
        y = [1.0, 2.0, 3.0, 4.0]
        r = _pearson_r(x, y)
        assert r is None

    def test_zero_variance_y(self):
        # All y values the same
        x = [1.0, 2.0, 3.0, 4.0]
        y = [5.0, 5.0, 5.0, 5.0]
        r = _pearson_r(x, y)
        assert r is None


class TestInterpretCorrelation:
    def test_strong_positive(self):
        result = _interpret_correlation(0.8)
        assert "strong" in result and "positive" in result

    def test_strong_negative(self):
        result = _interpret_correlation(-0.75)
        assert "strong" in result and "negative" in result

    def test_moderate_positive(self):
        result = _interpret_correlation(0.55)
        assert "moderate" in result and "positive" in result

    def test_weak_positive(self):
        result = _interpret_correlation(0.25)
        assert "weak" in result and "positive" in result

    def test_negligible(self):
        result = _interpret_correlation(0.1)
        assert "negligible" in result


class TestLoadFloorSummaries:
    def test_loads_within_window(self, tmp_path):
        events = [
            _make_event("floor_2", _days_ago(0), 3600, temp_f=35.0),
            _make_event("floor_2", _days_ago(5), 4200, temp_f=40.0),
        ]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        assert _today() in rows
        assert _days_ago(5) in rows

    def test_floor_filter_applies(self, tmp_path):
        events = [
            _make_event("floor_1", _days_ago(0), 1800, temp_f=35.0),
            _make_event("floor_2", _days_ago(0), 3600, temp_f=35.0),
        ]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_2")
        assert "floor_2" in rows[_today()]
        assert "floor_1" not in rows[_today()]


class TestPrintSingleFloorCorrelation:
    def test_output_contains_floor_label(self, tmp_path):
        events = [_make_event("floor_2", _today(), 3600, temp_f=40.0)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_2")
        buf = io.StringIO()
        _print_single_floor_correlation(rows, "floor_2", days=30, file=buf)
        assert "Floor 2" in buf.getvalue()

    def test_output_contains_correlation(self, tmp_path):
        # Create data with known correlation: warmer days = longer runtime
        events = [
            _make_event("floor_2", _days_ago(i), 3600 + i * 300, temp_f=40.0 - i * 2)
            for i in range(10)
        ]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_2")
        buf = io.StringIO()
        _print_single_floor_correlation(rows, "floor_2", days=30, file=buf)
        output = buf.getvalue()
        assert "Pearson r" in output
        assert "Sample size" in output

    def test_no_data_message(self, tmp_path):
        log = _write_log(tmp_path, [])
        rows = _load_floor_summaries(str(log), days=30, floor_filter="floor_1")
        buf = io.StringIO()
        _print_single_floor_correlation(rows, "floor_1", days=30, file=buf)
        assert "No data" in buf.getvalue()


class TestPrintAllFloorsCorrelation:
    def test_header_includes_all_floors(self, tmp_path):
        events = [_make_event("floor_2", _today(), 3600, temp_f=40.0)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        buf = io.StringIO()
        _print_all_floors_correlation(rows, days=30, file=buf)
        output = buf.getvalue()
        assert "Floor 1" in output
        assert "Floor 2" in output
        assert "Floor 3" in output

    def test_correlation_summary_present(self, tmp_path):
        events = [_make_event("floor_1", _days_ago(i), 1800, temp_f=40.0) for i in range(10)]
        log = _write_log(tmp_path, events)
        rows = _load_floor_summaries(str(log), days=30)
        buf = io.StringIO()
        _print_all_floors_correlation(rows, days=30, file=buf)
        output = buf.getvalue()
        assert "Correlation Summary" in output
        assert "Pearson r" in output


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
