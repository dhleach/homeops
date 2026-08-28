"""Regression tests for the additive consumer cooling event path.

Revision history:
  2026-08-27  Add processor, bootstrap, persistence, and playback coverage for
              cooling helper events without changing the heating contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from consumer import _empty_daily_state, _playback_phase, _save_state

from constants import _COOLING_FLOOR_ENTITIES, AC_COOLING_ENTITY
from processors import process_cooling_floor_event, process_cooling_session_event
from state import last_ac_cooling_on_since

COOLING_FLOOR_1 = "binary_sensor.floor_1_cooling_call"
COOLING_FLOOR_2 = "binary_sensor.floor_2_cooling_call"
COOLING_FLOOR_3 = "binary_sensor.floor_3_cooling_call"

START = datetime(2024, 7, 15, 14, 0, tzinfo=UTC)
START_STR = START.isoformat()


def _cooling_floor_state(**overrides: datetime | None) -> dict[str, datetime | None]:
    state = {entity_id: None for entity_id in _COOLING_FLOOR_ENTITIES}
    state.update(overrides)
    return state


def _observer_line(entity_id: str, old_state: str, new_state: str, ts: datetime) -> str:
    return json.dumps(
        {
            "schema": "homeops.observer.state_changed.v1",
            "ts": ts.isoformat(),
            "data": {
                "entity_id": entity_id,
                "old_state": old_state,
                "new_state": new_state,
                "attributes": {},
            },
        }
    )


def _write_observer_log(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "observer.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _read_derived(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestCoolingFloorEvents:
    def test_off_to_on_emits_cooling_call_started(self):
        events, updated = process_cooling_floor_event(
            COOLING_FLOOR_1,
            "off",
            "on",
            START,
            START_STR,
            _cooling_floor_state(),
        )

        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.cooling_call_started.v1"
        assert events[0]["data"] == {
            "floor": "floor_1",
            "started_at": START_STR,
            "entity_id": COOLING_FLOOR_1,
        }
        assert updated[COOLING_FLOOR_1] == START

    def test_on_to_off_emits_cooling_call_ended_with_duration(self):
        end = START + timedelta(minutes=42, seconds=15)
        events, updated = process_cooling_floor_event(
            COOLING_FLOOR_2,
            "on",
            "off",
            end,
            end.isoformat(),
            _cooling_floor_state(**{COOLING_FLOOR_2: START}),
        )

        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.cooling_call_ended.v1"
        assert events[0]["data"]["floor"] == "floor_2"
        assert events[0]["data"]["entity_id"] == COOLING_FLOOR_2
        assert events[0]["data"]["duration_s"] == 2535
        assert updated[COOLING_FLOOR_2] is None

    def test_end_without_observed_start_has_null_duration(self):
        events, updated = process_cooling_floor_event(
            COOLING_FLOOR_3,
            "on",
            "off",
            START,
            START_STR,
            _cooling_floor_state(),
        )

        assert events[0]["data"]["duration_s"] is None
        assert updated[COOLING_FLOOR_3] is None

    def test_unknown_entity_does_not_change_state(self):
        state = _cooling_floor_state()
        events, updated = process_cooling_floor_event(
            "binary_sensor.other", "off", "on", START, START_STR, state
        )

        assert events == []
        assert updated == state


class TestCoolingWholeHomeSessions:
    def test_off_to_on_emits_cooling_session_started(self):
        events, updated = process_cooling_session_event(
            AC_COOLING_ENTITY, "off", "on", START, START_STR, None
        )

        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.cooling_session_started.v1"
        assert events[0]["data"] == {
            "started_at": START_STR,
            "entity_id": AC_COOLING_ENTITY,
        }
        assert updated == START

    def test_on_to_off_emits_session_end_with_duration_and_outdoor_temp(self):
        end = START + timedelta(hours=2, seconds=3)
        events, updated = process_cooling_session_event(
            AC_COOLING_ENTITY,
            "on",
            "off",
            end,
            end.isoformat(),
            START,
            last_outdoor_temp_f=88.5,
        )

        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.cooling_session_ended.v1"
        assert events[0]["data"] == {
            "ended_at": end.isoformat(),
            "entity_id": AC_COOLING_ENTITY,
            "duration_s": 7203,
            "outdoor_temp_f": 88.5,
        }
        assert updated is None

    def test_end_without_observed_start_has_null_duration(self):
        events, updated = process_cooling_session_event(
            AC_COOLING_ENTITY, "on", "off", START, START_STR, None
        )

        assert events[0]["data"]["duration_s"] is None
        assert events[0]["data"]["outdoor_temp_f"] is None
        assert updated is None

    def test_non_aggregate_entity_does_not_emit(self):
        events, updated = process_cooling_session_event(
            COOLING_FLOOR_1, "off", "on", START, START_STR, None
        )

        assert events == []
        assert updated is None


class TestCoolingBootstrapAndPersistence:
    def test_bootstrap_recovers_active_ac_session(self, tmp_path: Path):
        observer = _write_observer_log(
            tmp_path,
            [_observer_line(AC_COOLING_ENTITY, "off", "on", START)],
        )

        assert last_ac_cooling_on_since(str(observer)) == START

    def test_bootstrap_returns_none_after_ac_session_ends(self, tmp_path: Path):
        end = START + timedelta(minutes=30)
        observer = _write_observer_log(
            tmp_path,
            [
                _observer_line(AC_COOLING_ENTITY, "off", "on", START),
                _observer_line(AC_COOLING_ENTITY, "on", "off", end),
            ],
        )

        assert last_ac_cooling_on_since(str(observer)) is None

    def test_bootstrap_ignores_unrelated_newer_entities(self, tmp_path: Path):
        observer = _write_observer_log(
            tmp_path,
            [
                _observer_line(AC_COOLING_ENTITY, "off", "on", START),
                _observer_line(COOLING_FLOOR_1, "off", "on", START + timedelta(minutes=1)),
            ],
        )

        assert last_ac_cooling_on_since(str(observer)) == START

    def test_save_state_serializes_independent_cooling_state(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        ac_start = START + timedelta(minutes=5)
        _save_state(
            {},
            None,
            {},
            {},
            cooling_floor_on_since=_cooling_floor_state(**{COOLING_FLOOR_1: START}),
            ac_cooling_on_since=ac_start,
            state_file=state_file,
        )

        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved["cooling_floor_on_since"][COOLING_FLOOR_1] == START_STR
        assert saved["cooling_floor_on_since"][COOLING_FLOOR_2] is None
        assert saved["ac_cooling_on_since"] == ac_start.isoformat()
        assert saved["floor_on_since"] == {}
        assert saved["furnace_on_since"] is None


class TestCoolingPlayback:
    def _run(self, observer: Path, derived: Path, start: datetime, **kwargs) -> dict:
        from rules.floor_no_response import FloorNoResponseRule
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        return _playback_phase(
            str(observer),
            start.isoformat(),
            derived_log=str(derived),
            floor_on_since={},
            furnace_on_since=None,
            climate_state={},
            daily_state=_empty_daily_state(),
            floor_2_warn_sent=False,
            fresh_restart=False,
            current_date="2024-07-15",
            floor_entities={},
            floor_no_response_rule=FloorNoResponseRule(),
            furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
            telegram_bot_token="",
            telegram_chat_id="",
            **kwargs,
        )

    def test_replays_floor_and_aggregate_cooling_events(self, tmp_path: Path):
        end = START + timedelta(minutes=30)
        observer = _write_observer_log(
            tmp_path,
            [
                _observer_line(COOLING_FLOOR_1, "off", "on", START),
                _observer_line(AC_COOLING_ENTITY, "off", "on", START),
                _observer_line(COOLING_FLOOR_1, "on", "off", end),
                _observer_line(AC_COOLING_ENTITY, "on", "off", end),
            ],
        )
        derived = tmp_path / "derived.jsonl"

        result = self._run(
            observer,
            derived,
            START,
            cooling_floor_on_since=_cooling_floor_state(),
            ac_cooling_on_since=None,
        )

        events = _read_derived(derived)
        schemas = [event["schema"] for event in events]
        assert schemas == [
            "homeops.consumer.cooling_call_started.v1",
            "homeops.consumer.cooling_session_started.v1",
            "homeops.consumer.cooling_call_ended.v1",
            "homeops.consumer.cooling_session_ended.v1",
        ]
        assert events[2]["data"]["duration_s"] == 1800
        assert events[3]["data"]["duration_s"] == 1800
        assert events[0]["ts"] == START_STR
        assert events[-1]["ts"] == end.isoformat()
        assert result["cooling_floor_on_since"][COOLING_FLOOR_1] is None
        assert result["ac_cooling_on_since"] is None

    def test_replay_preserves_active_cooling_state_at_eof(self, tmp_path: Path):
        observer = _write_observer_log(
            tmp_path,
            [_observer_line(AC_COOLING_ENTITY, "off", "on", START)],
        )
        derived = tmp_path / "derived.jsonl"

        result = self._run(observer, derived, START, ac_cooling_on_since=None)

        assert result["ac_cooling_on_since"] == START
        assert _read_derived(derived)[0]["schema"] == (
            "homeops.consumer.cooling_session_started.v1"
        )

    def test_replay_uses_provided_start_for_restart_end_duration(self, tmp_path: Path):
        end = START + timedelta(hours=1)
        observer = _write_observer_log(
            tmp_path,
            [_observer_line(AC_COOLING_ENTITY, "on", "off", end)],
        )
        derived = tmp_path / "derived.jsonl"

        result = self._run(observer, derived, end, ac_cooling_on_since=START)

        events = _read_derived(derived)
        assert events[0]["schema"] == "homeops.consumer.cooling_session_ended.v1"
        assert events[0]["data"]["duration_s"] == 3600
        assert result["ac_cooling_on_since"] is None
