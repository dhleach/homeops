"""Unit tests for consumer.py pure event-processing functions."""

import json
from datetime import UTC, datetime

import pytest
from consumer import (
    check_floor_2_warning,
    last_furnace_on_since,
    process_floor_event,
    process_furnace_event,
    process_outdoor_temp_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TS = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
TS_STR = TS.isoformat()

FLOOR_1 = "binary_sensor.floor_1_heating_call"
FLOOR_2 = "binary_sensor.floor_2_heating_call"
FLOOR_3 = "binary_sensor.floor_3_heating_call"
FURNACE = "binary_sensor.furnace_heating"
OUTDOOR_TEMP = "sensor.outdoor_temperature"


def make_floor_on_since(**overrides):
    base = {FLOOR_1: None, FLOOR_2: None, FLOOR_3: None}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# process_floor_event — floor call sessions
# ---------------------------------------------------------------------------


class TestProcessFloorEvent:
    def test_floor_1_off_to_on_emits_started(self):
        floor_on_since = make_floor_on_since()
        events, _, _ = process_floor_event(FLOOR_1, "off", "on", TS, TS_STR, floor_on_since, False)
        assert len(events) == 1
        evt = events[0]
        assert evt["schema"] == "homeops.consumer.floor_call_started.v1"
        assert evt["data"]["floor"] == "floor_1"
        assert evt["data"]["entity_id"] == FLOOR_1
        assert evt["data"]["started_at"] == TS_STR

    def test_floor_2_off_to_on_emits_started_and_resets_warn_sent(self):
        floor_on_since = make_floor_on_since()
        events, _, warn_sent = process_floor_event(
            FLOOR_2, "off", "on", TS, TS_STR, floor_on_since, True
        )
        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.floor_call_started.v1"
        assert events[0]["data"]["floor"] == "floor_2"
        assert warn_sent is False  # reset because floor_2 started a new call

    def test_floor_3_on_to_off_emits_ended_with_duration(self):
        started = datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC)
        floor_on_since = make_floor_on_since(**{FLOOR_3: started})
        ended_ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        events, _, _ = process_floor_event(
            FLOOR_3, "on", "off", ended_ts, ended_ts.isoformat(), floor_on_since, False
        )
        assert len(events) == 1
        evt = events[0]
        assert evt["schema"] == "homeops.consumer.floor_call_ended.v1"
        assert evt["data"]["floor"] == "floor_3"
        assert evt["data"]["duration_s"] == 3600

    def test_duration_s_is_none_when_no_start_time(self):
        floor_on_since = make_floor_on_since()  # floor_1 start is None
        events, _, _ = process_floor_event(FLOOR_1, "on", "off", TS, TS_STR, floor_on_since, False)
        assert len(events) == 1
        assert events[0]["data"]["duration_s"] is None

    def test_unrecognized_entity_id_returns_empty_no_crash(self):
        floor_on_since = make_floor_on_since()
        events, updated_fos, updated_warn = process_floor_event(
            "binary_sensor.unknown_entity", "off", "on", TS, TS_STR, floor_on_since, False
        )
        assert events == []
        assert updated_fos == floor_on_since
        assert updated_warn is False


# ---------------------------------------------------------------------------
# process_furnace_event — heating sessions
# ---------------------------------------------------------------------------


class TestProcessFurnaceEvent:
    def test_off_to_on_emits_started(self):
        events, furnace_on_since = process_furnace_event(FURNACE, "off", "on", TS, TS_STR, None)
        assert len(events) == 1
        evt = events[0]
        assert evt["schema"] == "homeops.consumer.heating_session_started.v1"
        assert evt["data"]["started_at"] == TS_STR
        assert evt["data"]["entity_id"] == FURNACE
        assert furnace_on_since == TS

    def test_on_to_off_emits_ended_with_correct_duration(self):
        started = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
        ended = datetime(2024, 1, 15, 9, 0, 30, tzinfo=UTC)
        events, furnace_on_since = process_furnace_event(
            FURNACE, "on", "off", ended, ended.isoformat(), started
        )
        assert len(events) == 1
        evt = events[0]
        assert evt["schema"] == "homeops.consumer.heating_session_ended.v1"
        assert evt["data"]["duration_s"] == 3630
        assert furnace_on_since is None

    def test_duration_s_is_none_when_furnace_on_since_is_none(self):
        events, furnace_on_since = process_furnace_event(FURNACE, "on", "off", TS, TS_STR, None)
        assert len(events) == 1
        assert events[0]["data"]["duration_s"] is None
        assert furnace_on_since is None


# ---------------------------------------------------------------------------
# process_outdoor_temp_event
# ---------------------------------------------------------------------------


class TestProcessOutdoorTempEvent:
    def test_valid_float_string_emits_event(self):
        events = process_outdoor_temp_event(OUTDOOR_TEMP, "42.5", TS_STR)
        assert len(events) == 1
        evt = events[0]
        assert evt["schema"] == "homeops.consumer.outdoor_temp_updated.v1"
        assert evt["data"]["temperature_f"] == pytest.approx(42.5)
        assert evt["data"]["entity_id"] == OUTDOOR_TEMP

    def test_unavailable_returns_empty(self):
        assert process_outdoor_temp_event(OUTDOOR_TEMP, "unavailable", TS_STR) == []

    def test_unknown_returns_empty(self):
        assert process_outdoor_temp_event(OUTDOOR_TEMP, "unknown", TS_STR) == []

    def test_non_numeric_string_returns_empty(self):
        assert process_outdoor_temp_event(OUTDOOR_TEMP, "abc", TS_STR) == []

    def test_none_returns_empty(self):
        assert process_outdoor_temp_event(OUTDOOR_TEMP, None, TS_STR) == []


# ---------------------------------------------------------------------------
# check_floor_2_warning
# ---------------------------------------------------------------------------


class TestCheckFloor2Warning:
    def _floor_on_since(self, started):
        return {FLOOR_2: started}

    def test_no_warning_when_elapsed_below_threshold(self):
        started = datetime(2024, 1, 15, 9, 30, 0, tzinfo=UTC)
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)  # 30 min elapsed
        fos = self._floor_on_since(started)
        event, warn_sent = check_floor_2_warning(fos, False, 2700, now)
        assert event is None
        assert warn_sent is False

    def test_warning_emitted_when_elapsed_meets_threshold(self):
        started = datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC)
        now = datetime(2024, 1, 15, 9, 45, 0, tzinfo=UTC)  # exactly 45 min = 2700 s
        fos = self._floor_on_since(started)
        event, warn_sent = check_floor_2_warning(fos, False, 2700, now)
        assert event is not None
        assert event["schema"] == "homeops.consumer.floor_2_long_call_warning.v1"
        assert event["data"]["elapsed_s"] == 2700
        assert warn_sent is True

    def test_warning_not_emitted_again_if_already_sent(self):
        started = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)  # 2 hrs elapsed
        fos = self._floor_on_since(started)
        event, warn_sent = check_floor_2_warning(fos, True, 2700, now)
        assert event is None
        assert warn_sent is True  # unchanged

    def test_warning_contains_correct_elapsed_s(self):
        started = datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC)
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)  # 3600 s elapsed
        fos = self._floor_on_since(started)
        event, _ = check_floor_2_warning(fos, False, 2700, now)
        assert event["data"]["elapsed_s"] == 3600


# ---------------------------------------------------------------------------
# last_furnace_on_since — bootstrap
# ---------------------------------------------------------------------------


def _make_observer_event(entity_id, old_state, new_state, ts_str):
    return json.dumps(
        {
            "schema": "homeops.observer.state_changed.v1",
            "ts": ts_str,
            "data": {
                "entity_id": entity_id,
                "old_state": old_state,
                "new_state": new_state,
            },
        }
    )


class TestLastFurnaceOnSince:
    def _write_lines(self, tmp_path, lines):
        p = tmp_path / "events.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def test_returns_datetime_when_last_furnace_event_is_off_to_on(self, tmp_path):
        ts = "2024-01-15T09:00:00+00:00"
        path = self._write_lines(tmp_path, [_make_observer_event(FURNACE, "off", "on", ts)])
        result = last_furnace_on_since(path)
        assert result is not None
        assert result.isoformat() == ts

    def test_returns_none_when_last_furnace_event_is_on_to_off(self, tmp_path):
        ts = "2024-01-15T09:00:00+00:00"
        path = self._write_lines(
            tmp_path,
            [
                _make_observer_event(FURNACE, "off", "on", "2024-01-15T08:00:00+00:00"),
                _make_observer_event(FURNACE, "on", "off", ts),
            ],
        )
        assert last_furnace_on_since(path) is None

    def test_returns_none_when_no_furnace_events(self, tmp_path):
        path = self._write_lines(tmp_path, [_make_observer_event(FLOOR_1, "off", "on", TS_STR)])
        assert last_furnace_on_since(path) is None

    def test_returns_none_when_file_does_not_exist(self, tmp_path):
        assert last_furnace_on_since(str(tmp_path / "missing.jsonl")) is None

    def test_returns_none_when_file_is_empty(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text("", encoding="utf-8")
        assert last_furnace_on_since(str(p)) is None

    def test_skips_malformed_json_lines(self, tmp_path):
        ts = "2024-01-15T09:00:00+00:00"
        path = self._write_lines(
            tmp_path,
            [
                "not valid json",
                _make_observer_event(FURNACE, "off", "on", ts),
            ],
        )
        result = last_furnace_on_since(path)
        # The malformed line is at the end (reversed order), so it is skipped;
        # the furnace off->on line is found next.
        assert result is not None
        assert result.isoformat() == ts

    def test_handles_mixed_non_furnace_and_furnace_events(self, tmp_path):
        ts = "2024-01-15T09:30:00+00:00"
        path = self._write_lines(
            tmp_path,
            [
                _make_observer_event(FLOOR_1, "off", "on", "2024-01-15T08:00:00+00:00"),
                _make_observer_event(FURNACE, "off", "on", ts),
                _make_observer_event(FLOOR_2, "on", "off", "2024-01-15T09:45:00+00:00"),
            ],
        )
        result = last_furnace_on_since(path)
        assert result is not None
        assert result.isoformat() == ts
