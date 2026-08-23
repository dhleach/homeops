"""Tests for scripts/runtime_per_degree.py.

Revision history:
  2026-08-23  Added focused coverage for completed-call pairing, furnace overlap,
              thermostat and outdoor-data guards, bucket aggregation, and CLI
              validation for the read-only efficiency report.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from runtime_per_degree import (
    DEFAULT_DAYS,
    FLOOR_CALL_ENDED_SCHEMA,
    FLOOR_CALL_STARTED_SCHEMA,
    FURNACE_ENDED_SCHEMA,
    FURNACE_STARTED_SCHEMA,
    OUTDOOR_SCHEMA,
    THERMOSTAT_SCHEMAS,
    _parse_args,
    _resolve_range,
    build_observations,
    build_report,
    load_events,
    render_markdown,
)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


def _event(schema: str, event_time: datetime, **data) -> dict:
    return {
        "schema": schema,
        "source": "consumer.v1",
        "ts": event_time.isoformat(),
        "data": {"ts": event_time.isoformat(), **data},
    }


def _call_start(timestamp: datetime, zone: str = "floor_1") -> dict:
    return _event(
        FLOOR_CALL_STARTED_SCHEMA,
        timestamp,
        floor=zone,
        entity_id=f"binary_sensor.{zone}_heating_call",
        started_at=timestamp.isoformat(),
    )


def _call_end(
    timestamp: datetime,
    duration_s: float | None,
    zone: str = "floor_1",
) -> dict:
    return _event(
        FLOOR_CALL_ENDED_SCHEMA,
        timestamp,
        floor=zone,
        entity_id=f"binary_sensor.{zone}_heating_call",
        ended_at=timestamp.isoformat(),
        duration_s=duration_s,
    )


def _furnace_start(timestamp: datetime) -> dict:
    return _event(
        FURNACE_STARTED_SCHEMA,
        timestamp,
        entity_id="binary_sensor.furnace_heating",
        started_at=timestamp.isoformat(),
    )


def _furnace_end(
    timestamp: datetime,
    duration_s: float | None,
    outdoor_temp_f: float | None = None,
) -> dict:
    return _event(
        FURNACE_ENDED_SCHEMA,
        timestamp,
        entity_id="binary_sensor.furnace_heating",
        ended_at=timestamp.isoformat(),
        duration_s=duration_s,
        outdoor_temp_f=outdoor_temp_f,
    )


def _temp(
    timestamp: datetime,
    temperature_f: float,
    zone: str = "floor_1",
    schema: str = "homeops.consumer.thermostat_current_temp_updated.v1",
) -> dict:
    return _event(
        schema,
        timestamp,
        zone=zone,
        entity_id=f"climate.{zone}_thermostat",
        current_temp=temperature_f,
        hvac_action="heating",
    )


def _outdoor(timestamp: datetime, temperature_f: float) -> dict:
    return _event(
        OUTDOOR_SCHEMA,
        timestamp,
        entity_id="sensor.outdoor_temperature",
        temperature_f=temperature_f,
        timestamp=timestamp.isoformat(),
    )


def _write_log(tmp_path: Path, events: list[object]) -> Path:
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            output.write(event if isinstance(event, str) else json.dumps(event))
            output.write("\n")
    return path


def _valid_events(
    *,
    zone: str = "floor_1",
    outdoor_temp_f: float | None = 35.0,
    end_minute: int = 15,
    start_temp_f: float = 68.0,
    end_temp_f: float = 70.0,
    furnace_end_minute: int = 20,
) -> list[dict]:
    start = _dt(0, 5)
    end = _dt(0, end_minute)
    furnace_end = _dt(0, furnace_end_minute)
    return [
        _furnace_start(_dt(0)),
        _call_start(start, zone),
        _temp(_dt(0, 4), start_temp_f, zone),
        _outdoor(_dt(0, 6), outdoor_temp_f)
        if outdoor_temp_f is not None
        else _event(OUTDOOR_SCHEMA, _dt(0, 6), temperature_f="unknown"),
        _furnace_end(furnace_end, 20 * 60, outdoor_temp_f),
        _call_end(end, (end - start).total_seconds(), zone),
        _temp(_dt(0, end_minute + 1), end_temp_f, zone),
    ]


class TestLoadEvents:
    def test_loads_relevant_events_sorted_and_deduplicated(self, tmp_path):
        event = _temp(_dt(1), 68.0)
        log = _write_log(
            tmp_path,
            [
                event,
                event,
                {"schema": "unrelated.v1", "data": {}},
                "not json",
                _outdoor(_dt(0), 35.0),
            ],
        )

        events = load_events(log)

        assert len(events) == 2
        assert [item.schema for item in events][0] == OUTDOOR_SCHEMA
        assert events[1].schema in THERMOSTAT_SCHEMAS


class TestRuntimeObservations:
    def test_computes_furnace_overlap_ratio_and_bucket(self, tmp_path):
        events = load_events(_write_log(tmp_path, _valid_events()))

        observations, stats = build_observations(
            events,
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert len(observations) == 1
        observation = observations[0]
        assert observation.furnace_on_time_s == pytest.approx(600.0)
        assert observation.temperature_delta_f == pytest.approx(2.0)
        assert observation.runtime_per_degree_s == pytest.approx(300.0)
        assert observation.outdoor_temp_source == "outdoor_event"
        assert observation.outdoor_bucket_lower_f == 30.0
        assert observation.outdoor_bucket_upper_f == 40.0
        assert stats["eligible_observations"] == 1

    def test_uses_duration_to_reconstruct_missing_call_start(self, tmp_path):
        raw = _valid_events()
        raw.remove(next(event for event in raw if event["schema"] == FLOOR_CALL_STARTED_SCHEMA))
        observations, stats = build_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert len(observations) == 1
        assert observations[0].call_started_at == _dt(0, 5)
        assert stats["calls_using_derived_start"] == 1

    def test_skips_incomplete_and_invalid_duration_calls(self, tmp_path):
        raw = [
            _call_start(_dt(0, 5), "floor_1"),
            _call_end(_dt(0, 10), None, "floor_1"),
            _call_start(_dt(1, 5), "floor_1"),
            _call_end(_dt(1, 10), 0, "floor_1"),
            _call_start(_dt(2, 5), "floor_1"),
            _call_end(_dt(2, 10), -5, "floor_1"),
        ]

        observations, stats = build_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert observations == []
        assert stats["call_ends_in_range"] == 3
        assert stats["incomplete_calls"] == 1
        assert stats["invalid_duration_calls"] == 2

    def test_missing_temperature_boundary_is_explicit(self, tmp_path):
        raw = _valid_events()
        raw = [
            event
            for event in raw
            if event["schema"] != "homeops.consumer.thermostat_current_temp_updated.v1"
        ]
        observations, stats = build_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert observations == []
        assert stats["calls_missing_temperature_boundary"] == 1

    def test_stale_temperature_boundary_is_explicit(self, tmp_path):
        raw = _valid_events()
        for event in raw:
            if event["schema"] in THERMOSTAT_SCHEMAS:
                timestamp = _dt(0) if event["data"]["current_temp"] == 68.0 else _dt(2)
                event["ts"] = timestamp.isoformat()
                event["data"]["ts"] = timestamp.isoformat()
        observations, stats = build_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
            max_temp_gap_min=10,
        )

        assert observations == []
        assert stats["calls_with_stale_temperature_boundary"] == 1

    @pytest.mark.parametrize("end_temp_f", [68.0, 67.0])
    def test_non_positive_temperature_delta_is_explicit(self, tmp_path, end_temp_f):
        observations, stats = build_observations(
            load_events(_write_log(tmp_path, _valid_events(end_temp_f=end_temp_f))),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert observations == []
        assert stats["calls_with_non_positive_temperature_delta"] == 1

    def test_no_furnace_runtime_is_explicit(self, tmp_path):
        raw = _valid_events()
        raw = [
            event
            for event in raw
            if event["schema"] not in {FURNACE_STARTED_SCHEMA, FURNACE_ENDED_SCHEMA}
        ]
        observations, stats = build_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert observations == []
        assert stats["calls_without_furnace_runtime"] == 1

    def test_missing_outdoor_temperature_is_explicit(self, tmp_path):
        raw = _valid_events(outdoor_temp_f=None)
        observations, stats = build_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert observations == []
        assert stats["calls_missing_outdoor_temperature"] == 1

    def test_furnace_session_temperature_fallback_is_labeled(self, tmp_path):
        raw = _valid_events(outdoor_temp_f=35.0)
        raw = [event for event in raw if event["schema"] != OUTDOOR_SCHEMA]
        raw = [
            event
            for event in raw
            if event["schema"] != FURNACE_ENDED_SCHEMA
            or event["data"].get("ended_at") != _dt(0, 20).isoformat()
        ] + [_furnace_end(_dt(0, 14), 14 * 60, 35.0)]
        observations, stats = build_observations(
            load_events(_write_log(tmp_path, raw)),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )

        assert len(observations) == 1
        assert observations[0].outdoor_temp_source == "furnace_session_end"
        assert stats["furnace_session_temperatures_used"] == 1


class TestReport:
    def test_groups_by_zone_and_outdoor_bucket(self, tmp_path):
        first = _valid_events(outdoor_temp_f=35.0)
        second = [
            event
            for event in _valid_events(
                outdoor_temp_f=45.0,
                end_temp_f=69.0,
            )
            if event["schema"] != OUTDOOR_SCHEMA
        ]
        shifted = []
        for event in second:
            event = json.loads(json.dumps(event))
            for key in ("ts",):
                event[key] = (datetime.fromisoformat(event[key]) + timedelta(hours=1)).isoformat()
            for key in ("ts", "started_at", "ended_at", "timestamp"):
                if key in event["data"]:
                    event["data"][key] = (
                        datetime.fromisoformat(event["data"][key]) + timedelta(hours=1)
                    ).isoformat()
            shifted.append(event)
        second = shifted + [_outdoor(_dt(1, 6), 45.0)]
        zone_two = [event for event in _valid_events(zone="floor_2", outdoor_temp_f=35.0)]
        report = build_report(
            load_events(_write_log(tmp_path, first + second + zone_two)),
            date(2026, 1, 1),
            date(2026, 1, 1),
            min_observations=2,
        )

        floor_one = next(zone for zone in report["zones"] if zone["zone"] == "floor_1")
        floor_two = next(zone for zone in report["zones"] if zone["zone"] == "floor_2")
        assert floor_one["observation_count"] == 2
        assert {bucket["outdoor_temp_bucket"] for bucket in floor_one["buckets"]} == {
            "[30, 40)°F",
            "[40, 50)°F",
        }
        assert floor_one["status"] == "ok"
        assert all(bucket["status"] == "insufficient_data" for bucket in floor_one["buckets"])
        assert floor_two["observation_count"] == 1
        assert floor_two["status"] == "insufficient_data"
        assert report["coverage"]["eligible_observations"] == 3

    def test_empty_report_marks_known_zones_insufficient(self):
        report = build_report([], date(2026, 1, 1), date(2026, 1, 1))

        assert report["coverage"]["eligible_observations"] == 0
        assert all(zone["status"] == "insufficient_data" for zone in report["zones"])
        assert report["data_quality"]["call_ends_in_range"] == 0

    def test_report_is_json_serializable_and_markdown_has_guard(self, tmp_path):
        report = build_report(
            load_events(_write_log(tmp_path, _valid_events())),
            date(2026, 1, 1),
            date(2026, 1, 1),
        )
        output = io.StringIO()

        rendered = render_markdown(report, file=output)

        json.dumps(report)
        assert rendered + "\n" == output.getvalue()
        assert "Zone and outdoor-temperature buckets" in rendered
        assert "not a furnace combustion efficiency rating" in rendered
        assert "Missing outdoor temperature" in rendered


class TestCli:
    def test_range_and_cli_options_validate(self):
        args = _parse_args(
            [
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
                "--min-observations",
                "2",
                "--max-temp-gap-min",
                "15",
                "--max-outdoor-gap-min",
                "120",
                "--bucket-width-f",
                "5",
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
        assert args.min_observations == 2
        assert args.max_temp_gap_min == 15
        assert args.max_outdoor_gap_min == 120
        assert args.bucket_width_f == 5
        assert args.format == "json"
        start, end = _resolve_range(DEFAULT_DAYS, args.start, args.end)
        assert (start, end) == (args.start, args.end)

        with pytest.raises(ValueError, match="provided together"):
            _resolve_range(start=date(2026, 1, 1))
        with pytest.raises(ValueError, match="greater than 0"):
            build_report([], date(2026, 1, 1), date(2026, 1, 1), bucket_width_f=0)
