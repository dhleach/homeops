"""Tests for the additive thermostat-driven cooling event path.

Revision history:
  2026-08-28  Add all-zone cooling session, directional outcome, persistence,
              and playback coverage while retaining the heating contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from consumer import _empty_daily_state, _load_state, _parse_dt, _playback_phase, _save_state
from dateutil.parser import isoparse

from processors import process_climate_event

FLOORS = (
    "climate.floor_1_thermostat",
    "climate.floor_2_thermostat",
    "climate.floor_3_thermostat",
)
COOLING_FLOOR_ENTITIES = {
    "binary_sensor.floor_1_cooling_call": "floor_1",
    "binary_sensor.floor_2_cooling_call": "floor_2",
    "binary_sensor.floor_3_cooling_call": "floor_3",
}

FLOOR_1 = FLOORS[0]
FLOOR_2 = FLOORS[1]
COOLING_FLOOR_1 = "binary_sensor.floor_1_cooling_call"
COOLING_FLOOR_2 = "binary_sensor.floor_2_cooling_call"
COOLING_FLOOR_3 = "binary_sensor.floor_3_cooling_call"

START = datetime(2024, 7, 15, 14, 0, tzinfo=UTC)
START_STR = START.isoformat()
TARGET_STR = (START + timedelta(minutes=30)).isoformat()
END_STR = (START + timedelta(minutes=40)).isoformat()

SESSION_STARTED = "homeops.consumer.thermostat_cooling_session_started.v1"
SESSION_ENDED = "homeops.consumer.thermostat_cooling_session_ended.v1"
SETPOINT_REACHED = "homeops.consumer.thermostat_cooling_setpoint_reached.v1"
TIME_TO_COOL = "homeops.consumer.zone_time_to_cool.v1"
SETPOINT_MISS = "homeops.consumer.zone_cooling_setpoint_miss.v1"
UNDERSHOOT = "homeops.consumer.zone_cooling_undershoot.v1"


def _attrs(
    *, setpoint: float = 72.0, current_temp: float = 78.0, hvac_action: str = "cooling"
) -> dict[str, float | str]:
    return {
        "temperature": setpoint,
        "current_temperature": current_temp,
        "hvac_action": hvac_action,
    }


def _cooling_prev(
    entity_id: str = FLOOR_1,
    *,
    setpoint: float = 72.0,
    current_temp: float = 75.0,
    start_temp: float | None = 78.0,
    start_setpoint: float | None = 72.0,
    start_ts: datetime | None = START,
    reached_ts: datetime | None = None,
    reached_temp: float | None = None,
    post_temps: list[float] | None = None,
    session_temps: list[float] | None = None,
    other_zones: list[str] | None = None,
    setpoint_changed: bool = False,
    hvac_mode: str = "cool",
    hvac_action: str = "cooling",
) -> dict[str, dict]:
    return {
        entity_id: {
            "setpoint": setpoint,
            "current_temp": current_temp,
            "hvac_mode": hvac_mode,
            "hvac_action": hvac_action,
            "heating_start_temp": None,
            "heating_start_ts": None,
            "setpoint_reached_ts": None,
            "setpoint_reached_temp": None,
            "post_setpoint_temps": [],
            "session_temps": [],
            "heating_start_other_zones": None,
            "setpoint_changed_during_heating": False,
            "slow_to_heat_sent": False,
            "cooling_start_temp": start_temp,
            "cooling_start_setpoint": start_setpoint,
            "cooling_start_ts": start_ts,
            "cooling_setpoint_reached_ts": reached_ts,
            "cooling_setpoint_reached_temp": reached_temp,
            "cooling_post_setpoint_temps": post_temps or [],
            "cooling_session_temps": session_temps or [],
            "cooling_start_other_zones": other_zones,
            "setpoint_changed_during_cooling": setpoint_changed,
        }
    }


def _schemas(events: list[dict]) -> list[str]:
    return [event["schema"] for event in events]


class TestCoolingSessionBoundaries:
    @pytest.mark.parametrize("entity_id", FLOORS)
    def test_cooling_action_starts_independent_session_for_each_zone(self, entity_id: str):
        previous = {
            entity_id: {
                "setpoint": 72.0,
                "current_temp": 78.0,
                "hvac_mode": "cool",
                "hvac_action": "idle",
                "heating_start_temp": None,
                "heating_start_ts": None,
            }
        }
        events, updated = process_climate_event(
            entity_id,
            _attrs(),
            START_STR,
            previous,
            new_state="cool",
            cooling_floor_on_since={COOLING_FLOOR_2: START},
            processing_ts=START_STR,
        )

        started = next(event for event in events if event["schema"] == SESSION_STARTED)
        assert started["ts"] == START_STR
        assert started["data"] == {
            "entity_id": entity_id,
            "zone": entity_id.removeprefix("climate.").removesuffix("_thermostat"),
            "started_at": START_STR,
            "mode": "cool",
            "hvac_mode": "cool",
            "hvac_action": "cooling",
            "setpoint": 72.0,
            "current_temp": 78.0,
            "other_zones_calling": ([] if entity_id == FLOOR_2 else [COOLING_FLOOR_2]),
        }
        state = updated[entity_id]
        assert state["cooling_start_temp"] == 78.0
        assert state["cooling_start_setpoint"] == 72.0
        assert state["cooling_start_ts"] == START
        assert state["heating_start_temp"] is None

    def test_cooling_end_emits_boundary_and_keeps_heating_state_untouched(self):
        previous = _cooling_prev()
        previous[FLOOR_1]["heating_start_temp"] = 64.0
        previous[FLOOR_1]["heating_start_ts"] = START - timedelta(hours=1)
        attrs = _attrs(current_temp=74.0, hvac_action="idle")

        events, updated = process_climate_event(
            FLOOR_1, attrs, END_STR, previous, new_state="cool", processing_ts=END_STR
        )

        ended = next(event for event in events if event["schema"] == SESSION_ENDED)
        assert ended["ts"] == END_STR
        assert ended["data"]["mode"] == "cool"
        assert ended["data"]["duration_s"] == 2400
        assert ended["data"]["target_reached"] is False
        state = updated[FLOOR_1]
        assert state["cooling_start_ts"] is None
        assert state["cooling_start_temp"] is None
        assert state["heating_start_temp"] == 64.0
        assert state["heating_start_ts"] == START - timedelta(hours=1)

    def test_idle_in_cool_mode_does_not_start_a_cooling_session(self):
        previous = _cooling_prev(
            hvac_action="idle", hvac_mode="cool", start_temp=None, start_ts=None
        )
        attrs = _attrs(current_temp=70.0, hvac_action="idle")

        events, updated = process_climate_event(
            FLOOR_1, attrs, START_STR, previous, new_state="cool"
        )

        assert SESSION_STARTED not in _schemas(events)
        assert SETPOINT_REACHED not in _schemas(events)
        assert TIME_TO_COOL not in _schemas(events)
        assert updated[FLOOR_1].get("cooling_start_ts") is None


class TestCoolingTargetOutcomes:
    def test_target_crossing_is_downward_and_uses_session_start_setpoint(self):
        previous = _cooling_prev(
            current_temp=74.0,
            session_temps=[78.0, 76.0, 74.0],
            other_zones=[COOLING_FLOOR_2],
        )
        attrs = _attrs(current_temp=72.0)
        events, updated = process_climate_event(
            FLOOR_1,
            attrs,
            TARGET_STR,
            previous,
            new_state="cool",
            cooling_floor_on_since={COOLING_FLOOR_1: START, COOLING_FLOOR_2: START},
            daily_state={"last_outdoor_temp_f": 88.5},
            processing_ts=TARGET_STR,
        )

        schemas = _schemas(events)
        assert SETPOINT_REACHED in schemas
        assert TIME_TO_COOL in schemas
        assert "homeops.consumer.thermostat_setpoint_reached.v1" not in schemas
        reached = next(event for event in events if event["schema"] == SETPOINT_REACHED)
        assert reached["data"]["mode"] == "cool"
        assert reached["data"]["setpoint"] == 72.0
        timed = next(event for event in events if event["schema"] == TIME_TO_COOL)
        assert timed["ts"] == TARGET_STR
        assert timed["data"] == {
            "entity_id": FLOOR_1,
            "zone": "floor_1",
            "mode": "cool",
            "start_temp": 78.0,
            "setpoint": 72.0,
            "setpoint_delta": 6.0,
            "duration_s": 1800,
            "end_temp": 72.0,
            "degrees_cooled": 6.0,
            "degrees_per_min": 0.2,
            "outdoor_temp_f": 88.5,
            "other_zones_calling": [COOLING_FLOOR_2],
        }
        assert updated[FLOOR_1]["cooling_setpoint_reached_ts"] == isoparse(TARGET_STR)

    def test_setpoint_change_does_not_retarget_active_cooling_session(self):
        previous = _cooling_prev(current_temp=74.0, setpoint=72.0)
        attrs = _attrs(setpoint=70.0, current_temp=72.0)

        events, updated = process_climate_event(
            FLOOR_1, attrs, TARGET_STR, previous, new_state="cool"
        )

        timed = next(event for event in events if event["schema"] == TIME_TO_COOL)
        assert timed["data"]["setpoint"] == 72.0
        assert timed["data"]["setpoint_delta"] == 6.0
        assert updated[FLOOR_1]["setpoint_changed_during_cooling"] is True

    def test_already_at_cooling_target_does_not_create_training_crossing(self):
        previous = _cooling_prev(
            current_temp=71.0,
            start_temp=71.0,
            start_setpoint=72.0,
            session_temps=[71.0],
        )
        attrs = _attrs(current_temp=70.0)

        events, _ = process_climate_event(FLOOR_1, attrs, TARGET_STR, previous, new_state="cool")

        assert SETPOINT_REACHED not in _schemas(events)
        assert TIME_TO_COOL not in _schemas(events)

    def test_target_crossing_is_emitted_once_after_temperature_bounce(self):
        previous = _cooling_prev(current_temp=74.0, session_temps=[78.0, 74.0])
        first_events, after_crossing = process_climate_event(
            FLOOR_1,
            _attrs(current_temp=72.0),
            TARGET_STR,
            previous,
            new_state="cool",
        )

        second_ts = START + timedelta(minutes=35)
        _, after_warm_bounce = process_climate_event(
            FLOOR_1,
            _attrs(current_temp=73.0),
            second_ts.isoformat(),
            after_crossing,
            new_state="cool",
        )
        third_events, after_second_crossing = process_climate_event(
            FLOOR_1,
            _attrs(current_temp=71.5),
            END_STR,
            after_warm_bounce,
            new_state="cool",
        )

        assert _schemas(first_events).count(SETPOINT_REACHED) == 1
        assert _schemas(first_events).count(TIME_TO_COOL) == 1
        assert SETPOINT_REACHED not in _schemas(third_events)
        assert TIME_TO_COOL not in _schemas(third_events)
        assert after_second_crossing[FLOOR_1]["cooling_setpoint_reached_ts"] == isoparse(TARGET_STR)

    def test_simultaneous_target_crossing_and_end_emits_undershoot_and_end(self):
        previous = _cooling_prev(current_temp=74.0, session_temps=[78.0, 74.0])
        attrs = _attrs(current_temp=72.0, hvac_action="idle")

        events, updated = process_climate_event(
            FLOOR_1, attrs, TARGET_STR, previous, new_state="cool"
        )

        schemas = _schemas(events)
        assert SETPOINT_REACHED in schemas
        assert TIME_TO_COOL in schemas
        assert UNDERSHOOT in schemas
        assert SESSION_ENDED in schemas
        assert SETPOINT_MISS not in schemas
        undershoot = next(event for event in events if event["schema"] == UNDERSHOOT)
        assert undershoot["data"]["undershoot_s"] == 0
        assert undershoot["data"]["trough_temp"] is None
        assert updated[FLOOR_1]["cooling_start_ts"] is None

    def test_cooling_miss_uses_lowest_temperature_and_positive_delta(self):
        previous = _cooling_prev(
            current_temp=75.0,
            session_temps=[78.0, 77.0, 75.0],
            other_zones=[COOLING_FLOOR_3],
            setpoint_changed=True,
        )
        attrs = _attrs(current_temp=74.5, hvac_action="idle")

        events, _ = process_climate_event(
            FLOOR_1,
            attrs,
            END_STR,
            previous,
            new_state="cool",
            daily_state={"last_outdoor_temp_f": 90.0},
        )

        miss = next(event for event in events if event["schema"] == SETPOINT_MISS)
        assert miss["data"] == {
            "entity_id": FLOOR_1,
            "zone": "floor_1",
            "mode": "cool",
            "start_temp": 78.0,
            "setpoint": 72.0,
            "setpoint_delta": 6.0,
            "duration_s": 2400,
            "closest_temp": 74.5,
            "delta": 2.5,
            "outdoor_temp_f": 90.0,
            "other_zones_calling": [COOLING_FLOOR_3],
            "likely_cause": "thermostat_adjustment",
        }
        assert UNDERSHOOT not in _schemas(events)

    def test_cooling_undershoot_uses_lowest_post_target_reading(self):
        previous = _cooling_prev(
            current_temp=71.5,
            reached_ts=START + timedelta(minutes=30),
            reached_temp=72.0,
            post_temps=[72.0, 71.5],
        )
        attrs = _attrs(current_temp=71.0, hvac_action="idle")

        events, _ = process_climate_event(FLOOR_1, attrs, END_STR, previous, new_state="cool")

        undershoot = next(event for event in events if event["schema"] == UNDERSHOOT)
        assert undershoot["data"]["end_temp"] == 71.0
        assert undershoot["data"]["undershoot_s"] == 600
        assert undershoot["data"]["trough_temp"] == 71.0
        assert SETPOINT_MISS not in _schemas(events)


class TestCoolingPersistenceAndPlayback:
    def test_state_round_trip_serializes_cooling_climate_timestamps(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        reached = START + timedelta(minutes=30)
        climate_state = _cooling_prev(
            start_ts=START,
            reached_ts=reached,
            reached_temp=72.0,
            post_temps=[72.0],
        )

        _save_state({}, None, climate_state, {}, state_file=state_file)
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        saved = raw["climate_state"][FLOOR_1]
        assert saved["cooling_start_ts"] == START_STR
        assert saved["cooling_setpoint_reached_ts"] == reached.isoformat()

        loaded = _load_state(state_file=state_file)
        assert loaded is not None
        assert _parse_dt(loaded["climate_state"][FLOOR_1]["cooling_start_ts"]) == START
        assert _parse_dt(loaded["climate_state"][FLOOR_1]["cooling_setpoint_reached_ts"]) == reached

    def test_playback_emits_cooling_events_with_observer_timestamps(self, tmp_path: Path):
        observer = tmp_path / "observer.jsonl"
        derived = tmp_path / "derived.jsonl"
        lines = [
            {
                "schema": "homeops.observer.state_changed.v1",
                "ts": START_STR,
                "data": {
                    "entity_id": FLOOR_1,
                    "old_state": "cool",
                    "new_state": "cool",
                    "attributes": _attrs(current_temp=78.0),
                },
            },
            {
                "schema": "homeops.observer.state_changed.v1",
                "ts": TARGET_STR,
                "data": {
                    "entity_id": FLOOR_1,
                    "old_state": "cool",
                    "new_state": "cool",
                    "attributes": _attrs(current_temp=72.0),
                },
            },
            {
                "schema": "homeops.observer.state_changed.v1",
                "ts": END_STR,
                "data": {
                    "entity_id": FLOOR_1,
                    "old_state": "cool",
                    "new_state": "cool",
                    "attributes": _attrs(current_temp=71.0, hvac_action="idle"),
                },
            },
        ]
        observer.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        from rules.floor_no_response import FloorNoResponseRule
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        result = _playback_phase(
            str(observer),
            START_STR,
            derived_log=str(derived),
            floor_on_since={
                "binary_sensor.floor_1_heating_call": None,
                "binary_sensor.floor_2_heating_call": None,
                "binary_sensor.floor_3_heating_call": None,
            },
            furnace_on_since=None,
            climate_state={},
            daily_state=_empty_daily_state(),
            floor_2_warn_sent=False,
            fresh_restart=True,
            current_date="2024-07-15",
            floor_entities={
                "binary_sensor.floor_1_heating_call": "floor_1",
                "binary_sensor.floor_2_heating_call": "floor_2",
                "binary_sensor.floor_3_heating_call": "floor_3",
            },
            floor_no_response_rule=FloorNoResponseRule(),
            furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
            telegram_bot_token="",
            telegram_chat_id="",
            cooling_floor_on_since={entity_id: None for entity_id in COOLING_FLOOR_ENTITIES},
        )

        events = [
            json.loads(line) for line in derived.read_text(encoding="utf-8").splitlines() if line
        ]
        assert any(event["schema"] == SESSION_STARTED for event in events)
        timed = next(event for event in events if event["schema"] == TIME_TO_COOL)
        assert timed["ts"] == TARGET_STR
        ended = next(event for event in events if event["schema"] == SESSION_ENDED)
        assert ended["ts"] == END_STR
        assert ended["data"]["duration_s"] == 2400
        assert result["climate_state"][FLOOR_1]["cooling_start_ts"] is None
