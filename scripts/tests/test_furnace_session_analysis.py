"""Tests for scripts/furnace_session_analysis.py."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from furnace_session_analysis import (
    SCHEMA,
    _fmt_duration,
    _interpret_r,
    _load_sessions,
    _parse_args,
    _pearson_r,
    _write_csv,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = UTC


def _make_event(
    ended_at: str,
    duration_s: int,
    outdoor_temp_f: float | None,
    entity_id: str = "binary_sensor.furnace_heating",
) -> str:
    return json.dumps(
        {
            "schema": SCHEMA,
            "source": "consumer.v1",
            "ts": ended_at,
            "data": {
                "ended_at": ended_at,
                "entity_id": entity_id,
                "duration_s": duration_s,
                "outdoor_temp_f": outdoor_temp_f,
            },
        }
    )


def _write_log(path: Path, events: list[str]) -> None:
    path.write_text("\n".join(events) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# _pearson_r
# ---------------------------------------------------------------------------


class TestPearsonR:
    def test_perfect_positive(self) -> None:
        r = _pearson_r([1.0, 2.0, 3.0], [2.0, 4.0, 6.0])
        assert r is not None
        assert abs(r - 1.0) < 1e-9

    def test_perfect_negative(self) -> None:
        r = _pearson_r([1.0, 2.0, 3.0], [6.0, 4.0, 2.0])
        assert r is not None
        assert abs(r + 1.0) < 1e-9

    def test_low_correlation(self) -> None:
        # Symmetric around the mean in both dimensions → r close to 0
        r = _pearson_r([1.0, 2.0, 3.0], [2.0, 2.0, 2.0])
        # constant y → zero variance → None
        assert r is None

    def test_too_few_points(self) -> None:
        assert _pearson_r([1.0], [1.0]) is None
        assert _pearson_r([], []) is None

    def test_zero_variance(self) -> None:
        assert _pearson_r([2.0, 2.0, 2.0], [1.0, 2.0, 3.0]) is None

    def test_mismatched_lengths(self) -> None:
        assert _pearson_r([1.0, 2.0], [1.0]) is None


# ---------------------------------------------------------------------------
# _interpret_r
# ---------------------------------------------------------------------------


class TestInterpretR:
    def test_strong_negative(self) -> None:
        assert _interpret_r(-0.8) == "strong negative"

    def test_moderate_positive(self) -> None:
        assert _interpret_r(0.5) == "moderate positive"

    def test_weak_negative(self) -> None:
        assert _interpret_r(-0.3) == "weak negative"

    def test_negligible(self) -> None:
        assert _interpret_r(0.05) == "negligible positive"


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_under_one_hour(self) -> None:
        assert _fmt_duration(360) == "6m 00s"

    def test_over_one_hour(self) -> None:
        assert _fmt_duration(3660) == "1h 01m"

    def test_none(self) -> None:
        assert _fmt_duration(None) == "—"

    def test_zero(self) -> None:
        assert _fmt_duration(0) == "0m 00s"


# ---------------------------------------------------------------------------
# _load_sessions
# ---------------------------------------------------------------------------


class TestLoadSessions:
    def test_loads_sessions(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_log(
            log,
            [
                _make_event("2026-03-01T10:00:00+00:00", 600, 45.0),
                _make_event("2026-03-02T12:00:00+00:00", 300, None),
                '{"schema": "other.event.v1", "data": {}}',  # skipped
            ],
        )
        sessions = _load_sessions(str(log))
        assert len(sessions) == 2
        assert sessions[0]["duration_s"] == 600
        assert sessions[0]["outdoor_temp_f"] == 45.0
        assert sessions[1]["outdoor_temp_f"] is None

    def test_computes_started_at(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_log(log, [_make_event("2026-03-01T10:10:00+00:00", 600, 50.0)])
        sessions = _load_sessions(str(log))
        assert sessions[0]["started_at"] == "2026-03-01T10:00:00+00:00"

    def test_cutoff_filter(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_log(
            log,
            [
                _make_event("2026-03-01T10:00:00+00:00", 300, 40.0),
                _make_event("2026-03-10T10:00:00+00:00", 300, 50.0),
            ],
        )
        cutoff = datetime(2026, 3, 5, tzinfo=UTC)
        sessions = _load_sessions(str(log), cutoff_dt=cutoff)
        assert len(sessions) == 1
        assert "2026-03-10" in sessions[0]["ended_at"]

    def test_missing_log(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _load_sessions(str(tmp_path / "nonexistent.jsonl"))

    def test_skips_malformed(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_log(
            log,
            [
                "not json",
                _make_event("2026-03-01T10:00:00+00:00", 300, 45.0),
            ],
        )
        sessions = _load_sessions(str(log))
        assert len(sessions) == 1


# ---------------------------------------------------------------------------
# _write_csv
# ---------------------------------------------------------------------------


class TestWriteCsv:
    def test_writes_expected_columns(self, tmp_path: Path) -> None:
        out = tmp_path / "out.csv"
        sessions = [
            {
                "started_at": "2026-03-01T09:55:00+00:00",
                "ended_at": "2026-03-01T10:00:00+00:00",
                "duration_s": 300,
                "outdoor_temp_f": 42.0,
                "entity_id": "binary_sensor.furnace_heating",
            }
        ]
        _write_csv(sessions, str(out))
        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 1
        assert rows[0]["duration_s"] == "300"
        assert rows[0]["outdoor_temp_f"] == "42.0"

    def test_null_temp_written_as_empty(self, tmp_path: Path) -> None:
        out = tmp_path / "out.csv"
        _write_csv(
            [
                {
                    "started_at": "2026-03-01T09:55:00+00:00",
                    "ended_at": "2026-03-01T10:00:00+00:00",
                    "duration_s": 300,
                    "outdoor_temp_f": None,
                    "entity_id": "binary_sensor.furnace_heating",
                }
            ],
            str(out),
        )
        rows = list(csv.DictReader(out.open()))
        assert rows[0]["outdoor_temp_f"] == ""


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_defaults(self) -> None:
        args = _parse_args([])
        assert args.days is None
        assert args.out is None
        assert args.log is None

    def test_all_flags(self) -> None:
        args = _parse_args(["--days", "14", "--out", "out.csv", "--log", "events.jsonl"])
        assert args.days == 14
        assert args.out == "out.csv"
        assert args.log == "events.jsonl"
