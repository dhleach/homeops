"""Tests for scripts/scheduling_query.py.

Revision history:
  2026-08-25  Added coverage for ready schedules, inferred snapshots, safety
              threshold refusal, sparse history, invalid inputs, deterministic
              output, and the read-only event-log boundary.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from scheduling_query import (
    CURRENT_TEMP_SCHEMA,
    PRIMARY_ZONE,
    SCHEDULING_SCHEMA,
    TOOL_DEFINITION,
    _build_recommendation,
    build_schedule_query,
    load_current_temperature_snapshots,
    recommend_multi_zone_schedule,
    render_text,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
QUERY_DATE = date(2026, 1, 1)
AS_OF = datetime(2026, 1, 1, 23, tzinfo=UTC)
DEADLINE = datetime(2026, 1, 2, 7, tzinfo=UTC)


def _event(schema: str, timestamp: datetime, **data) -> dict:
    return {
        "schema": schema,
        "source": "consumer.v1",
        "ts": timestamp.isoformat(),
        "data": {"ts": timestamp.isoformat(), **data},
    }


def _time_to_temp(outdoor_temp_f: float, seconds_per_degree_s: float, index: int) -> dict:
    timestamp = BASE + timedelta(minutes=10 + index)
    return _event(
        "homeops.consumer.zone_time_to_temp.v1",
        timestamp,
        entity_id="climate.floor_2_thermostat",
        zone="floor_2",
        start_temp=65.0,
        setpoint=68.0,
        setpoint_delta=3.0,
        duration_s=3.0 * seconds_per_degree_s,
        end_temp=68.1,
        degrees_gained=3.1,
        outdoor_temp_f=outdoor_temp_f,
        other_zones_calling=[],
    )


def _current(zone: str, temperature_f: float, timestamp: datetime = AS_OF) -> dict:
    return _event(
        CURRENT_TEMP_SCHEMA,
        timestamp,
        entity_id=f"climate.{zone}_thermostat",
        zone=zone,
        current_temp=temperature_f,
        hvac_action="idle",
        setpoint=temperature_f,
    )


def _heat_loss_window(zone: str, start_hour: int, start_temp: float = 70.0) -> list[dict]:
    call_end = BASE.replace(hour=start_hour, minute=0)
    call_start = call_end - timedelta(minutes=30)
    next_call = call_end + timedelta(hours=2)
    return [
        _event(
            "homeops.consumer.floor_call_started.v1",
            call_start,
            entity_id=f"binary_sensor.{zone}_heating_call",
            floor=zone,
            started_at=call_start.isoformat(),
        ),
        _event(
            "homeops.consumer.floor_call_ended.v1",
            call_end,
            entity_id=f"binary_sensor.{zone}_heating_call",
            floor=zone,
            ended_at=call_end.isoformat(),
            duration_s=1800,
        ),
        _event(
            "homeops.consumer.floor_call_started.v1",
            next_call,
            entity_id=f"binary_sensor.{zone}_heating_call",
            floor=zone,
            started_at=next_call.isoformat(),
        ),
        _event(
            "homeops.consumer.thermostat_current_temp_updated.v1",
            call_end,
            entity_id=f"climate.{zone}_thermostat",
            zone=zone,
            current_temp=start_temp,
            hvac_action="idle",
        ),
        _event(
            "homeops.consumer.thermostat_current_temp_updated.v1",
            call_end + timedelta(minutes=30),
            entity_id=f"climate.{zone}_thermostat",
            zone=zone,
            current_temp=start_temp - 2.0,
            hvac_action="idle",
        ),
        _event(
            "homeops.consumer.thermostat_current_temp_updated.v1",
            call_end + timedelta(hours=1),
            entity_id=f"climate.{zone}_thermostat",
            zone=zone,
            current_temp=start_temp - 4.0,
            hvac_action="idle",
        ),
    ]


def _fixture_log(
    tmp_path: Path,
    *,
    seconds_per_degree: tuple[float, ...] = (600, 500, 400, 300, 200),
    include_floor_3_heat_loss: bool = True,
) -> Path:
    events = [
        _event(
            "homeops.consumer.heating_session_ended.v1",
            BASE,
            entity_id="binary_sensor.furnace_heating",
            ended_at=BASE.isoformat(),
            duration_s=60,
            outdoor_temp_f=30.0,
        )
    ]
    events.extend(
        _time_to_temp(outdoor, rate, index)
        for index, (outdoor, rate) in enumerate(zip((10, 20, 30, 40, 50), seconds_per_degree))
    )
    for hour in (1, 4, 7):
        events.extend(_heat_loss_window("floor_1", hour))
        if include_floor_3_heat_loss:
            events.extend(_heat_loss_window("floor_3", hour, start_temp=69.0))
    events.extend(
        [
            _current("floor_1", 70.0),
            _current("floor_2", 65.0),
            _current("floor_3", 69.0),
        ]
    )
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


def _query(log: Path, **kwargs) -> dict:
    return build_schedule_query(
        68.0,
        30.0,
        DEADLINE,
        current_temp_f=65.0,
        floor_1_current_temp_f=70.0,
        floor_3_current_temp_f=69.0,
        as_of=AS_OF,
        log_path=log,
        start=QUERY_DATE,
        end=QUERY_DATE,
        **kwargs,
    )


class TestScheduleQuery:
    def test_ready_schedule_uses_thermal_models_and_threshold(self, tmp_path):
        log = _fixture_log(tmp_path)

        result = _query(log)

        assert result["schema"] == SCHEDULING_SCHEMA
        assert result["answerability"] == {"status": "ready", "can_recommend": True, "reasons": []}
        recommendation = result["recommendation"]
        assert recommendation["primary_zone"] == PRIMARY_ZONE
        assert recommendation["predicted_duration_s"] == pytest.approx(1200)
        assert recommendation["recommended_start"] == "2026-01-02T06:40:00+00:00"
        assert recommendation["safety"]["configured_long_call_threshold_s"] == 2700
        assert recommendation["secondary_zones"]["floor_1"]["candidate_max_setpoint_f"] == 68.0
        assert recommendation["secondary_zones"]["floor_3"]["candidate_max_setpoint_f"] == 67.0
        assert recommendation["secondary_zones"]["floor_1"]["blocked_window"] == {
            "start": "2026-01-02T06:40:00+00:00",
            "end": "2026-01-02T07:00:00+00:00",
        }
        assert (
            recommendation["secondary_zones"]["floor_3"]["allowed_call_timing"][
                "after_primary_deadline"
            ]
            == "2026-01-02T07:00:00+00:00"
        )
        assert result["analysis"]["secondary_zones"]["floor_1"]["heat_loss_rate_basis"] == "p75"

    def test_current_temperatures_are_inferred_from_fresh_snapshots(self, tmp_path):
        log = _fixture_log(tmp_path)

        result = build_schedule_query(
            68.0,
            30.0,
            DEADLINE,
            as_of=AS_OF,
            log_path=log,
            start=QUERY_DATE,
            end=QUERY_DATE,
        )

        assert result["answerability"]["status"] == "ready"
        assert result["request"]["current_temp_source"] == AS_OF.isoformat()
        assert result["request"]["secondary_current_temp_sources"] == {
            "floor_1": AS_OF.isoformat(),
            "floor_3": AS_OF.isoformat(),
        }

    def test_extrapolated_primary_model_fails_closed(self, tmp_path):
        log = _fixture_log(tmp_path)

        result = build_schedule_query(
            68.0,
            60.0,
            DEADLINE,
            current_temp_f=65.0,
            floor_1_current_temp_f=70.0,
            floor_3_current_temp_f=69.0,
            as_of=AS_OF,
            log_path=log,
            start=QUERY_DATE,
            end=QUERY_DATE,
        )

        assert result["answerability"]["status"] == "unsafe_to_recommend"
        assert result["recommendation"] is None
        assert "outside the observed training range" in " ".join(result["answerability"]["reasons"])

    def test_predicted_call_near_threshold_fails_closed(self, tmp_path):
        log = _fixture_log(tmp_path, seconds_per_degree=(1000, 900, 800, 700, 600))

        result = _query(log)

        assert result["answerability"]["status"] == "unsafe_to_recommend"
        assert result["recommendation"] is None
        assert "safety reserve" in " ".join(result["answerability"]["reasons"])

    def test_missing_secondary_heat_loss_is_insufficient(self, tmp_path):
        log = _fixture_log(tmp_path, include_floor_3_heat_loss=False)

        result = _query(log)

        assert result["answerability"]["status"] == "insufficient_data"
        assert result["recommendation"] is None
        assert any("floor_3" in reason for reason in result["answerability"]["reasons"])

    def test_missing_primary_temperature_is_insufficient(self, tmp_path):
        log = _fixture_log(tmp_path)
        log.write_text(
            "\n".join(
                line
                for line in log.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("data", {}).get("zone") != "floor_2"
                or json.loads(line).get("schema") != CURRENT_TEMP_SCHEMA
            )
            + "\n",
            encoding="utf-8",
        )

        result = build_schedule_query(
            68.0,
            30.0,
            DEADLINE,
            as_of=AS_OF,
            log_path=log,
            start=QUERY_DATE,
            end=QUERY_DATE,
        )

        assert result["answerability"]["status"] == "insufficient_data"
        assert result["recommendation"] is None
        assert "no fresh floor-2 current temperature" in " ".join(
            result["answerability"]["reasons"]
        )

    def test_stale_inferred_snapshots_are_rejected(self, tmp_path):
        log = _fixture_log(tmp_path)
        historical = [
            _current(zone, temperature, AS_OF - timedelta(hours=7))
            for zone, temperature in (("floor_1", 70.0), ("floor_2", 65.0), ("floor_3", 69.0))
        ]
        retained = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("schema") != CURRENT_TEMP_SCHEMA
        ]
        log.write_text(
            "\n".join([*retained, *(json.dumps(event) for event in historical)]) + "\n",
            encoding="utf-8",
        )

        result = build_schedule_query(
            68.0,
            30.0,
            DEADLINE,
            as_of=AS_OF,
            log_path=log,
            start=QUERY_DATE,
            end=QUERY_DATE,
        )

        assert result["answerability"]["status"] == "insufficient_data"
        assert result["request"]["current_temp_f"] is None
        assert result["data_quality"]["current_temperature"]["stale_for_as_of"] == 3

    def test_deadline_before_candidate_start_is_unsafe(self, tmp_path):
        log = _fixture_log(tmp_path)
        deadline = AS_OF + timedelta(minutes=10)

        result = build_schedule_query(
            68.0,
            30.0,
            deadline,
            current_temp_f=65.0,
            floor_1_current_temp_f=70.0,
            floor_3_current_temp_f=69.0,
            as_of=AS_OF,
            log_path=log,
            start=QUERY_DATE,
            end=QUERY_DATE,
        )

        assert result["answerability"]["status"] == "unsafe_to_recommend"
        assert "deadline is too soon" in " ".join(result["answerability"]["reasons"])

    def test_provider_neutral_dispatch_rejects_unknown_and_missing_arguments(self, tmp_path):
        log = _fixture_log(tmp_path)
        args = {
            "target_temp_f": 68,
            "outdoor_temp_f": 30,
            "deadline": DEADLINE.isoformat(),
            "current_temp_f": 65,
            "floor_1_current_temp_f": 70,
            "floor_3_current_temp_f": 69,
            "as_of": AS_OF.isoformat(),
        }
        result = recommend_multi_zone_schedule(
            args,
            log_path=log,
            start=QUERY_DATE,
            end=QUERY_DATE,
        )
        assert result["answerability"]["status"] == "ready"
        with pytest.raises(ValueError, match="unknown tool argument"):
            recommend_multi_zone_schedule({**args, "write_thermostat": True}, log_path=log)
        with pytest.raises(ValueError, match="missing required"):
            recommend_multi_zone_schedule({"target_temp_f": 68}, log_path=log)
        assert TOOL_DEFINITION["parameters"]["additionalProperties"] is False

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"target_temp_f": 121}, "target_temp_f must be"),
            ({"outdoor_temp_f": 151}, "outdoor_temp_f must be"),
            ({"current_temp_f": 68}, "target_temp_f must be greater"),
            ({"safety_margin_minutes": -1}, "safety_margin_minutes must be"),
            ({"max_snapshot_age_hours": 0}, "max_snapshot_age_hours must be"),
        ],
    )
    def test_request_validation(self, tmp_path, kwargs, message):
        log = _fixture_log(tmp_path)
        values = {
            "target_temp_f": 68.0,
            "outdoor_temp_f": 30.0,
            "deadline": DEADLINE,
            "current_temp_f": 65.0,
            "floor_1_current_temp_f": 70.0,
            "floor_3_current_temp_f": 69.0,
            "as_of": AS_OF,
            "log_path": log,
            "start": QUERY_DATE,
            "end": QUERY_DATE,
        }
        values.update(kwargs)
        with pytest.raises(ValueError, match=message):
            build_schedule_query(**values)

    def test_result_is_deterministic_and_does_not_write_log(self, tmp_path):
        log = _fixture_log(tmp_path)
        before = log.read_bytes()

        first = _query(log)
        second = _query(log)

        assert first == second
        assert log.read_bytes() == before
        assert first["read_only"] is True

    def test_sparse_history_returns_explicit_insufficient_data(self, tmp_path):
        log = tmp_path / "sparse.jsonl"
        log.write_text(
            json.dumps(_current("floor_2", 65.0)) + "\n",
            encoding="utf-8",
        )

        result = build_schedule_query(
            68.0,
            30.0,
            DEADLINE,
            as_of=AS_OF,
            log_path=log,
            start=QUERY_DATE,
            end=QUERY_DATE,
        )

        assert result["answerability"]["status"] == "insufficient_data"
        assert result["answerability"]["can_recommend"] is False
        assert result["recommendation"] is None
        assert (
            result["model_outputs"]["time_to_temperature"]["prediction"]["status"]
            == "insufficient_data"
        )

    def test_snapshot_loader_deduplicates_and_counts_invalid_input(self, tmp_path):
        event = _current("floor_2", 65.0)
        log = tmp_path / "snapshots.jsonl"
        log.write_text(
            "\n".join(
                [
                    json.dumps(event),
                    json.dumps(event),
                    "not json",
                    json.dumps(["not", "an", "event"]),
                    json.dumps(
                        _event(CURRENT_TEMP_SCHEMA, AS_OF, zone="floor_2", current_temp=999)
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        snapshots, quality = load_current_temperature_snapshots(log)

        assert len(snapshots) == 1
        assert quality["duplicate_events"] == 1
        assert quality["malformed_json"] == 1
        assert quality["non_object_events"] == 1
        assert quality["events_with_invalid_temperature"] == 1

    def test_text_renderer_explains_no_schedule(self):
        result = {
            "answerability": {"status": "insufficient_data", "reasons": ["missing history"]},
            "request": {"target_temp_f": 68.0, "outdoor_temp_f": 30.0, "deadline": "deadline"},
            "recommendation": None,
        }

        rendered = render_text(result)

        assert "No safe schedule produced." in rendered
        assert "missing history" in rendered


class TestRecommendationCore:
    def test_core_builder_marks_disabled_threshold_unsafe(self):
        status, recommendation, reasons, _ = _build_recommendation(
            target_temp_f=68,
            current_temp_f=65,
            deadline=DEADLINE,
            as_of=AS_OF,
            prediction={"status": "ok", "predicted_duration_s": 1200},
            heat_report={"zones": []},
            secondary_current_temps={"floor_1": 70, "floor_3": 69},
            threshold={"status": "ok", "enabled": False, "threshold_s": 2700},
            safety_margin_minutes=5,
        )

        assert status == "unsafe_to_recommend"
        assert recommendation is None
        assert "threshold is unavailable or disabled" in " ".join(reasons)
