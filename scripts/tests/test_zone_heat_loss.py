"""Tests for scripts/zone_heat_loss.py."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from zone_heat_loss import (
    DEFAULT_DAYS,
    FLOOR_CALL_ENDED_SCHEMA,
    FLOOR_CALL_STARTED_SCHEMA,
    FURNACE_ENDED_SCHEMA,
    FURNACE_STARTED_SCHEMA,
    THERMOSTAT_TEMP_SCHEMA,
    _parse_args,
    _resolve_range,
    build_cooling_observations,
    build_report,
    load_events,
    render_markdown,
)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


def _event(schema: str, timestamp: datetime, **data) -> dict:
    return {
        "schema": schema,
        "source": "consumer.v1",
        "ts": timestamp.isoformat(),
        "data": {"ts": timestamp.isoformat(), **data},
    }


def _call_start(timestamp: datetime, zone: str = "floor_1") -> dict:
    return _event(
        FLOOR_CALL_STARTED_SCHEMA,
        timestamp,
        floor=zone,
        entity_id=f"binary_sensor.{zone}_heating_call",
        started_at=timestamp.isoformat(),
    )


def _call_end(timestamp: datetime, zone: str = "floor_1") -> dict:
    return _event(
        FLOOR_CALL_ENDED_SCHEMA,
        timestamp,
        floor=zone,
        entity_id=f"binary_sensor.{zone}_heating_call",
        ended_at=timestamp.isoformat(),
    )


def _temp(
    timestamp: datetime, temperature: float, action: str = "idle", zone: str = "floor_1"
) -> dict:
    return _event(
        THERMOSTAT_TEMP_SCHEMA,
        timestamp,
        zone=zone,
        entity_id=f"climate.{zone}_thermostat",
        current_temp=temperature,
        hvac_action=action,
    )


def _furnace_start(timestamp: datetime) -> dict:
    return _event(
        FURNACE_STARTED_SCHEMA,
        timestamp,
        entity_id="binary_sensor.furnace_heating",
        started_at=timestamp.isoformat(),
    )


def _furnace_end(timestamp: datetime) -> dict:
    return _event(
        FURNACE_ENDED_SCHEMA,
        timestamp,
        entity_id="binary_sensor.furnace_heating",
        ended_at=timestamp.isoformat(),
    )


def _write_log(tmp_path: Path, events: list[object]) -> Path:
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            output.write(event if isinstance(event, str) else json.dumps(event))
            output.write("\n")
    return path


def _curve_events() -> list[dict]:
    return [
        _furnace_start(_dt(0)),
        _furnace_end(_dt(0, 10)),
        _call_end(_dt(0, 20)),
        _temp(_dt(0, 20), 70),
        _temp(_dt(0, 40), 69),
        _temp(_dt(1, 0), 68),
        _call_start(_dt(2)),
    ]


class TestLoadEvents:
    def test_loads_relevant_events_sorted_and_deduplicated(self, tmp_path):
        event = _temp(_dt(1), 68)
        log = _write_log(
            tmp_path,
            [
                event,
                event,
                {"schema": "unrelated.v1", "data": {}},
                "not json",
                _call_end(_dt(0, 20)),
            ],
        )

        events = load_events(log)

        assert len(events) == 2
        assert [item.schema for item in events] == [FLOOR_CALL_ENDED_SCHEMA, THERMOSTAT_TEMP_SCHEMA]

    def test_missing_log_is_explicit(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="log file not found"):
            load_events(tmp_path / "missing.jsonl")


class TestCoolingObservations:
    def test_fits_cooling_rate_only_from_furnace_off_idle_samples(self, tmp_path):
        events = load_events(_write_log(tmp_path, _curve_events()))

        observations, stats = build_cooling_observations(
            events,
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert len(observations) == 1
        assert observations[0].sample_count == 3
        assert observations[0].heat_loss_rate_f_per_min == pytest.approx(1 / 20, abs=1e-6)
        assert observations[0].temperature_delta_f == -2
        assert stats["furnace_off_idle_samples"] == 3

    def test_excludes_furnace_on_and_active_thermostat_samples(self, tmp_path):
        raw = [
            _furnace_start(_dt(0)),
            _furnace_end(_dt(0, 10)),
            _call_end(_dt(0, 20)),
            _temp(_dt(0, 20), 70),
            _furnace_start(_dt(0, 25)),
            _temp(_dt(0, 30), 69, action="idle"),
            _furnace_end(_dt(0, 35)),
            _temp(_dt(0, 40), 68.5, action="cooling"),
            _temp(_dt(0, 50), 68, action="idle"),
            _call_start(_dt(1, 30)),
        ]
        observations, stats = build_cooling_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
            min_samples=2,
            min_duration_min=20,
        )

        assert len(observations) == 1
        assert observations[0].sample_count == 2
        assert stats["furnace_on_samples_excluded"] == 1
        assert stats["thermostat_active_samples_excluded"] == 1

    def test_large_gap_splits_segments(self, tmp_path):
        raw = _curve_events()[:-1]
        raw.extend(
            [
                _temp(_dt(4), 67),
                _temp(_dt(4, 20), 66),
                _temp(_dt(4, 40), 65),
                _call_start(_dt(5)),
            ]
        )
        observations, stats = build_cooling_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
            max_gap_min=60,
        )

        assert len(observations) == 2
        assert stats["qualifying_segments"] == 2

    def test_flat_segment_is_not_reported_as_cooling(self, tmp_path):
        raw = [
            _furnace_start(_dt(0)),
            _furnace_end(_dt(0, 10)),
            _call_end(_dt(0, 20)),
            _temp(_dt(0, 20), 68),
            _temp(_dt(0, 40), 68),
            _temp(_dt(1), 68),
            _call_start(_dt(2)),
        ]
        observations, stats = build_cooling_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert observations == []
        assert stats["non_cooling_segments"] == 1

    def test_rising_endpoint_is_not_reported_as_cooling(self, tmp_path):
        raw = [
            _furnace_start(_dt(0)),
            _furnace_end(_dt(0, 10)),
            _call_end(_dt(0, 20)),
            _temp(_dt(0, 20), 68),
            _temp(_dt(0, 40), 67),
            _temp(_dt(1), 69),
            _call_start(_dt(2)),
        ]

        observations, stats = build_cooling_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert observations == []
        assert stats["non_cooling_segments"] == 1

    def test_rejects_invalid_configuration(self):
        with pytest.raises(ValueError, match="min_samples"):
            build_cooling_observations([], date(2026, 1, 1), date(2026, 1, 1), min_samples=1)


class TestReport:
    def test_report_marks_zones_with_insufficient_history(self, tmp_path):
        events = load_events(_write_log(tmp_path, _curve_events()))

        report = build_report(
            events,
            date(2026, 1, 1),
            date(2026, 1, 1),
            min_observations=2,
        )

        assert report["schema"] == "homeops.zone-heat-loss-report.v1"
        assert report["coverage"]["cooling_observations"] == 1
        assert report["zones_detail"][0]["status"] == "insufficient_data"
        assert report["zones_detail"][0]["median_heat_loss_rate_f_per_min"] == pytest.approx(0.05)
        assert report["zones_detail"][1]["status"] == "insufficient_data"

    def test_empty_report_is_explicit(self):
        report = build_report([], date(2026, 1, 1), date(2026, 1, 1))

        assert report["coverage"]["cooling_observations"] == 0
        assert all(zone["status"] == "insufficient_data" for zone in report["zones_detail"])

    def test_markdown_contains_quality_and_interpretation_guard(self, tmp_path):
        events = load_events(_write_log(tmp_path, _curve_events()))
        report = build_report(events, date(2026, 1, 1), date(2026, 1, 1))
        output = io.StringIO()

        rendered = render_markdown(report, file=output)

        assert rendered + "\n" == output.getvalue()
        assert "Zone summaries" in rendered
        assert "Furnace-on" in rendered
        assert "not proof of insulation loss" in rendered


class TestCli:
    def test_resolve_range_defaults_to_requested_days(self):
        start, end = _resolve_range(DEFAULT_DAYS)

        assert (end - start).days == DEFAULT_DAYS - 1

    def test_resolve_range_requires_both_explicit_dates(self):
        with pytest.raises(ValueError, match="provided together"):
            _resolve_range(start=date(2026, 1, 1))

    def test_parse_args_accepts_explicit_range_and_options(self):
        args = _parse_args(
            [
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
                "--min-samples",
                "4",
                "--min-duration-min",
                "15",
                "--max-gap-min",
                "90",
                "--min-observations",
                "2",
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
        assert args.min_samples == 4
        assert args.min_duration_min == 15
        assert args.max_gap_min == 90
        assert args.min_observations == 2
        assert args.format == "json"

    def test_parse_args_rejects_one_sample(self):
        with pytest.raises(SystemExit):
            _parse_args(["--min-samples", "1"])
