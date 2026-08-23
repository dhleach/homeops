"""Tests for scripts/time_to_temp.py.

Revision history:
  2026-08-23  Added coverage for completed-event loading, model fitting,
              sparse and extrapolated predictions, deterministic bucket output,
              Markdown rendering, and CLI validation.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from time_to_temp import (
    DEFAULT_DAYS,
    SCHEMA,
    TimeToTempObservation,
    _parse_args,
    _resolve_range,
    build_report,
    fit_linear_model,
    load_observations,
    render_markdown,
)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


def _event(event_time: datetime, **data) -> dict:
    return {
        "schema": SCHEMA,
        "source": "consumer.v1",
        "ts": event_time.isoformat(),
        "data": data,
    }


def _time_to_temp(
    event_time: datetime,
    *,
    zone: str = "floor_1",
    outdoor_temp_f: float = 30.0,
    setpoint_delta: float = 2.0,
    duration_s: float = 400.0,
) -> dict:
    return _event(
        event_time,
        entity_id=f"climate.{zone}_thermostat",
        zone=zone,
        outdoor_temp_f=outdoor_temp_f,
        setpoint_delta=setpoint_delta,
        duration_s=duration_s,
    )


def _observation(
    index: int,
    *,
    zone: str = "floor_1",
    outdoor_temp_f: float,
    setpoint_delta_f: float = 2.0,
    seconds_per_degree_s: float,
) -> TimeToTempObservation:
    timestamp = _dt(index)
    duration_s = setpoint_delta_f * seconds_per_degree_s
    return TimeToTempObservation(
        timestamp=timestamp,
        zone=zone,
        outdoor_temp_f=outdoor_temp_f,
        setpoint_delta_f=setpoint_delta_f,
        duration_s=duration_s,
        seconds_per_degree_s=seconds_per_degree_s,
        outdoor_bucket_lower_f=float(outdoor_temp_f // 10 * 10),
        outdoor_bucket_upper_f=float(outdoor_temp_f // 10 * 10 + 10),
    )


def _write_log(tmp_path: Path, events: list[object]) -> Path:
    path = tmp_path / "events.jsonl"
    with path.open("w", encoding="utf-8") as output:
        for event in events:
            output.write(event if isinstance(event, str) else json.dumps(event))
            output.write("\n")
    return path


class TestLoadObservations:
    def test_filters_schema_range_duplicates_and_invalid_rows(self, tmp_path):
        valid = _time_to_temp(
            _dt(2),
            outdoor_temp_f=30,
            setpoint_delta=2,
            duration_s=600,
        )
        invalid = _time_to_temp(_dt(3), setpoint_delta=0)
        missing = _time_to_temp(_dt(4))
        del missing["data"]["outdoor_temp_f"]
        log = _write_log(
            tmp_path,
            [
                _time_to_temp(_dt(0)),
                valid,
                valid,
                {"schema": "unrelated.v1", "data": {}},
                invalid,
                missing,
                "not json",
            ],
        )

        observations, quality = load_observations(log, date(2026, 1, 1), date(2026, 1, 1))

        assert len(observations) == 2
        assert [row.timestamp for row in observations] == [_dt(0), _dt(2)]
        assert observations[1].seconds_per_degree_s == pytest.approx(300)
        assert quality["duplicate_events"] == 1
        assert quality["malformed_json"] == 1
        assert quality["events_with_invalid_measurement"] == 1
        assert quality["events_missing_measurement"] == 1
        assert quality["eligible_observations"] == 2

    def test_rejects_missing_log_explicitly(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="log file not found"):
            load_observations(tmp_path / "missing.jsonl", date(2026, 1, 1), date(2026, 1, 1))


class TestModel:
    def test_fits_seconds_per_degree_line_and_predicts_duration(self):
        rows = [
            _observation(0, outdoor_temp_f=20, seconds_per_degree_s=300),
            _observation(1, outdoor_temp_f=30, seconds_per_degree_s=200),
            _observation(2, outdoor_temp_f=40, seconds_per_degree_s=100),
        ]

        model = fit_linear_model(rows)

        assert model is not None
        assert model.slope_s_per_degree_per_f == pytest.approx(-10)
        assert model.intercept_s_per_degree == pytest.approx(500)
        assert model.r_squared == pytest.approx(1)
        assert model.predict_seconds_per_degree(35) == pytest.approx(150)
        assert model.predict_duration(35, 3) == pytest.approx(450)

    def test_returns_none_for_insufficient_or_constant_outdoor_temperature(self):
        one = [_observation(0, outdoor_temp_f=20, seconds_per_degree_s=300)]
        constant = [
            _observation(0, outdoor_temp_f=20, seconds_per_degree_s=300),
            _observation(1, outdoor_temp_f=20, seconds_per_degree_s=200),
        ]

        assert fit_linear_model(one) is None
        assert fit_linear_model(constant) is None


class TestReport:
    def test_builds_zone_model_buckets_and_in_range_prediction(self):
        rows = [
            _observation(
                index,
                outdoor_temp_f=20 + index * 10,
                setpoint_delta_f=3,
                seconds_per_degree_s=300 - index * 50,
            )
            for index in range(5)
        ]

        report = build_report(
            rows,
            date(2026, 1, 1),
            date(2026, 1, 1),
            min_observations=3,
            query_zone="floor_1",
            query_outdoor_temp_f=30,
            query_setpoint_delta_f=3,
        )

        floor_one = next(zone for zone in report["zones"] if zone["zone"] == "floor_1")
        assert floor_one["status"] == "ok"
        assert floor_one["model"]["r_squared"] == pytest.approx(1)
        assert floor_one["bucket_count"] == 5
        assert report["prediction"]["status"] == "ok"
        assert report["prediction"]["predicted_duration_s"] == pytest.approx(750)
        assert report["prediction"]["outdoor_temp_bucket"] == "[30, 40)°F"

    def test_marks_sparse_known_zones_insufficient(self):
        report = build_report(
            [_observation(0, outdoor_temp_f=30, seconds_per_degree_s=200)],
            date(2026, 1, 1),
            date(2026, 1, 1),
            min_observations=2,
            query_zone="floor_2",
            query_outdoor_temp_f=30,
            query_setpoint_delta_f=3,
        )

        floor_one = next(zone for zone in report["zones"] if zone["zone"] == "floor_1")
        floor_two = next(zone for zone in report["zones"] if zone["zone"] == "floor_2")
        assert floor_one["status"] == "insufficient_data"
        assert floor_two["status"] == "insufficient_data"
        assert report["prediction"]["status"] == "insufficient_data"
        assert report["prediction"]["predicted_duration_s"] is None

    def test_marks_out_of_training_range_prediction_as_extrapolated(self):
        rows = [
            _observation(
                index,
                outdoor_temp_f=20 + index * 10,
                seconds_per_degree_s=300 - index * 50,
            )
            for index in range(3)
        ]

        report = build_report(
            rows,
            date(2026, 1, 1),
            date(2026, 1, 1),
            min_observations=2,
            query_zone="floor_1",
            query_outdoor_temp_f=10,
            query_setpoint_delta_f=2,
        )

        prediction = report["prediction"]
        assert prediction["status"] == "extrapolated"
        assert prediction["extrapolated_dimensions"] == ["outdoor_temp_f"]
        assert prediction["predicted_duration_s"] == pytest.approx(700)

    def test_filters_rows_to_requested_date_range(self):
        rows = [
            _observation(0, outdoor_temp_f=20, seconds_per_degree_s=300),
            TimeToTempObservation(
                timestamp=_dt(0) + timedelta(days=1),
                zone="floor_1",
                outdoor_temp_f=30,
                setpoint_delta_f=2,
                duration_s=400,
                seconds_per_degree_s=200,
                outdoor_bucket_lower_f=30,
                outdoor_bucket_upper_f=40,
            ),
        ]

        report = build_report(rows, date(2026, 1, 2), date(2026, 1, 2), min_observations=2)

        assert report["coverage"]["eligible_observations"] == 1
        assert report["zones"][0]["status"] == "insufficient_data"


class TestOutputAndCli:
    def test_markdown_contains_models_buckets_and_guard(self):
        report = build_report(
            [_observation(0, outdoor_temp_f=30, seconds_per_degree_s=200)],
            date(2026, 1, 1),
            date(2026, 1, 1),
            min_observations=2,
        )
        output = io.StringIO()

        rendered = render_markdown(report, file=output)

        assert rendered + "\n" == output.getvalue()
        assert "Zone models" in rendered
        assert "Outdoor-temperature bucket observations" in rendered
        assert "not a thermostat control signal" in rendered

    def test_resolve_range_defaults_to_requested_days(self):
        start, end = _resolve_range(days=DEFAULT_DAYS)

        assert (end - start).days == DEFAULT_DAYS - 1

    def test_resolve_range_requires_both_explicit_dates(self):
        with pytest.raises(ValueError, match="provided together"):
            _resolve_range(start=date(2026, 1, 1))

    def test_parse_args_accepts_prediction_options(self):
        args = _parse_args(
            [
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
                "--zone",
                "floor_2",
                "--outdoor",
                "30",
                "--delta",
                "3",
                "--min-observations",
                "4",
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
        assert args.zone == "floor_2"
        assert args.outdoor == 30
        assert args.delta == 3
        assert args.min_observations == 4
        assert args.bucket_width_f == 5
        assert args.format == "json"

    def test_rejects_invalid_model_minimum_and_partial_query(self):
        with pytest.raises(SystemExit):
            _parse_args(["--min-observations", "1"])
        with pytest.raises(ValueError, match="provided together"):
            build_report(
                [],
                date(2026, 1, 1),
                date(2026, 1, 1),
                query_zone="floor_1",
            )
