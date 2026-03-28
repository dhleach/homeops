"""Unit tests for consumer.py pure event-processing functions."""

import json
import signal
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from consumer import (
    SLOW_TO_HEAT_THRESHOLDS_S,
    _emit_derived,
    _empty_daily_state,
    _load_state,
    _register_sigterm_handler,
    _save_state,
    check_floor_2_warning,
    check_observer_silence,
    emit_daily_summary,
    format_daily_summary_message,
    last_furnace_on_since,
    process_climate_event,
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


# ---------------------------------------------------------------------------
# Bootstrap validation — consumer restart scenarios
# ---------------------------------------------------------------------------


class TestFurnaceBootstrapValidation:
    """
    Validates that session state is correctly reconstructed (or not) after a
    consumer restart, and that subsequent event processing produces no
    duplicates or incorrect derived events.
    """

    def _write_log(self, tmp_path, lines):
        p = tmp_path / "events.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    # ------------------------------------------------------------------
    # (a) Restart during an active furnace session
    # ------------------------------------------------------------------

    def test_restart_during_active_session_bootstraps_on_since(self, tmp_path):
        """Bootstrap detects an in-progress furnace session."""
        on_ts = "2024-01-15T09:00:00+00:00"
        path = self._write_log(tmp_path, [_make_observer_event(FURNACE, "off", "on", on_ts)])
        result = last_furnace_on_since(path)
        assert result is not None
        assert result.isoformat() == on_ts

    def test_restart_during_active_session_emits_session_end_with_duration(self, tmp_path):
        """
        After restart, the bootstrapped furnace_on_since is used to compute
        the correct duration when the furnace next turns OFF.
        """
        on_ts_str = "2024-01-15T09:00:00+00:00"
        path = self._write_log(tmp_path, [_make_observer_event(FURNACE, "off", "on", on_ts_str)])

        # Simulate bootstrap
        furnace_on_since = last_furnace_on_since(path)
        assert furnace_on_since is not None

        # Furnace turns OFF 90 minutes after the bootstrapped start
        off_ts = furnace_on_since + timedelta(minutes=90)
        events, new_furnace_on_since = process_furnace_event(
            FURNACE, "on", "off", off_ts, off_ts.isoformat(), furnace_on_since
        )

        assert len(events) == 1
        evt = events[0]
        assert evt["schema"] == "homeops.consumer.heating_session_ended.v1"
        assert evt["data"]["duration_s"] == 5400  # 90 min = 5400 s
        assert new_furnace_on_since is None

    def test_restart_during_active_session_emits_exactly_one_session_end(self, tmp_path):
        """
        Only one heating_session_ended.v1 is emitted — not an extra one from bootstrap.
        Bootstrap does not directly emit events; only a live on->off transition does.
        """
        on_ts = "2024-01-15T08:00:00+00:00"
        path = self._write_log(tmp_path, [_make_observer_event(FURNACE, "off", "on", on_ts)])

        furnace_on_since = last_furnace_on_since(path)
        off_ts = furnace_on_since + timedelta(hours=1)

        events, _ = process_furnace_event(
            FURNACE, "on", "off", off_ts, off_ts.isoformat(), furnace_on_since
        )
        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.heating_session_ended.v1"

    # ------------------------------------------------------------------
    # (b) Restart between furnace sessions — no active session
    # ------------------------------------------------------------------

    def test_restart_between_sessions_no_session_reconstructed(self, tmp_path):
        """When the last furnace event is OFF, bootstrap returns None."""
        path = self._write_log(
            tmp_path,
            [
                _make_observer_event(FURNACE, "off", "on", "2024-01-15T08:00:00+00:00"),
                _make_observer_event(FURNACE, "on", "off", "2024-01-15T09:00:00+00:00"),
            ],
        )
        assert last_furnace_on_since(path) is None

    def test_restart_between_sessions_new_on_starts_fresh_session(self, tmp_path):
        """
        With no bootstrapped session, a new furnace ON event creates a session
        and a subsequent OFF correctly computes duration from that new start.
        """
        path = self._write_log(
            tmp_path,
            [
                _make_observer_event(FURNACE, "off", "on", "2024-01-15T08:00:00+00:00"),
                _make_observer_event(FURNACE, "on", "off", "2024-01-15T09:00:00+00:00"),
            ],
        )

        furnace_on_since = last_furnace_on_since(path)
        assert furnace_on_since is None

        # New furnace ON arrives after restart
        new_on = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        events, furnace_on_since = process_furnace_event(
            FURNACE, "off", "on", new_on, new_on.isoformat(), furnace_on_since
        )
        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.heating_session_started.v1"
        assert furnace_on_since == new_on

        # Furnace OFF 30 minutes later
        new_off = new_on + timedelta(minutes=30)
        events, furnace_on_since = process_furnace_event(
            FURNACE, "on", "off", new_off, new_off.isoformat(), furnace_on_since
        )
        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.heating_session_ended.v1"
        assert events[0]["data"]["duration_s"] == 1800
        assert furnace_on_since is None

    # ------------------------------------------------------------------
    # (c) Restart at session boundary — no duplicate session_end
    # ------------------------------------------------------------------

    def test_restart_at_boundary_last_event_off_no_active_session(self, tmp_path):
        """
        Consumer restarts immediately after the OFF event is logged.
        Bootstrap finds the OFF event as the most recent furnace event → None.
        """
        path = self._write_log(
            tmp_path,
            [
                _make_observer_event(FURNACE, "off", "on", "2024-01-15T08:00:00+00:00"),
                _make_observer_event(FURNACE, "on", "off", "2024-01-15T09:00:00+00:00"),
            ],
        )
        assert last_furnace_on_since(path) is None

    def test_restart_at_boundary_no_duplicate_session_end_emitted(self, tmp_path):
        """
        When furnace_on_since is None after bootstrap at a boundary, processing
        a hypothetical duplicate on->off transition produces an event with
        duration_s=None, confirming no inflated-duration duplicate is emitted.
        The consumer's tail-follow approach means the OFF line itself won't be
        re-processed, but this verifies the guard is in place via state alone.
        """
        path = self._write_log(
            tmp_path,
            [
                _make_observer_event(FURNACE, "off", "on", "2024-01-15T08:00:00+00:00"),
                _make_observer_event(FURNACE, "on", "off", "2024-01-15T09:00:00+00:00"),
            ],
        )

        # Bootstrap: no active session
        furnace_on_since = last_furnace_on_since(path)
        assert furnace_on_since is None

        # Hypothetical stale on->off seen with no bootstrapped start time:
        # duration_s is None, confirming the old session duration is NOT duplicated.
        off_ts = datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC)
        events, _ = process_furnace_event(
            FURNACE, "on", "off", off_ts, off_ts.isoformat(), furnace_on_since
        )
        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.heating_session_ended.v1"
        assert events[0]["data"]["duration_s"] is None  # no double-counted duration

    def test_restart_at_boundary_with_only_on_event_does_not_double_end(self, tmp_path):
        """
        If restart happens before the OFF event is logged (consumer crash mid-session),
        bootstrap recovers furnace_on_since, and only one session_end fires when
        the OFF finally arrives — not two.
        """
        on_ts_str = "2024-01-15T08:00:00+00:00"
        path = self._write_log(
            tmp_path,
            [_make_observer_event(FURNACE, "off", "on", on_ts_str)],
        )

        furnace_on_since = last_furnace_on_since(path)
        assert furnace_on_since is not None

        off_ts = furnace_on_since + timedelta(hours=1)
        events, furnace_on_since = process_furnace_event(
            FURNACE, "on", "off", off_ts, off_ts.isoformat(), furnace_on_since
        )
        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.heating_session_ended.v1"
        assert events[0]["data"]["duration_s"] == 3600

        # A second on->off (e.g., stale replay) with furnace_on_since now None
        # produces duration_s=None — no inflated double count.
        events2, _ = process_furnace_event(
            FURNACE, "on", "off", off_ts, off_ts.isoformat(), furnace_on_since
        )
        assert events2[0]["data"]["duration_s"] is None

    # ------------------------------------------------------------------
    # (d) No duplicate floor_2_long_call_warning.v1 after restart
    # ------------------------------------------------------------------

    def test_restart_resets_floor_on_since_prevents_spurious_warning(self):
        """
        After restart, floor_on_since is re-initialised to all-None (no
        bootstrap from history for floors).  check_floor_2_warning returns None
        because there is no tracked start time, so no duplicate warning fires.
        """
        # Simulate state before restart: floor 2 was calling and warn was sent
        floor_on_since_before = {FLOOR_2: datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)}
        now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        event, warn_sent = check_floor_2_warning(floor_on_since_before, True, 2700, now)
        assert event is None  # already sent, no duplicate

        # After restart: floor_on_since reset, floor_2_warn_sent reset
        floor_on_since_after = {FLOOR_1: None, FLOOR_2: None, FLOOR_3: None}
        floor_2_warn_sent = False

        # Even though floor_2_warn_sent is False, no warning fires because
        # the start time is unknown.
        event, warn_sent = check_floor_2_warning(floor_on_since_after, floor_2_warn_sent, 2700, now)
        assert event is None
        assert warn_sent is False

    def test_restart_then_new_floor2_call_triggers_warning_once(self):
        """
        After restart, once floor 2 starts a NEW call (off->on), the warning
        system resets correctly and fires exactly once at the threshold.
        """
        floor_on_since = {FLOOR_1: None, FLOOR_2: None, FLOOR_3: None}
        floor_2_warn_sent = False

        # floor 2 turns ON after restart
        call_start = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        _, floor_on_since, floor_2_warn_sent = process_floor_event(
            FLOOR_2,
            "off",
            "on",
            call_start,
            call_start.isoformat(),
            floor_on_since,
            floor_2_warn_sent,
        )
        assert floor_on_since[FLOOR_2] == call_start
        assert floor_2_warn_sent is False  # reset on new call

        # Before threshold — no warning
        before_threshold = call_start + timedelta(minutes=44)
        event, floor_2_warn_sent = check_floor_2_warning(
            floor_on_since, floor_2_warn_sent, 2700, before_threshold
        )
        assert event is None
        assert floor_2_warn_sent is False

        # At threshold — warning fires once
        at_threshold = call_start + timedelta(minutes=45)
        event, floor_2_warn_sent = check_floor_2_warning(
            floor_on_since, floor_2_warn_sent, 2700, at_threshold
        )
        assert event is not None
        assert event["schema"] == "homeops.consumer.floor_2_long_call_warning.v1"
        assert floor_2_warn_sent is True

        # Beyond threshold — no second warning
        beyond_threshold = call_start + timedelta(minutes=60)
        event2, floor_2_warn_sent = check_floor_2_warning(
            floor_on_since, floor_2_warn_sent, 2700, beyond_threshold
        )
        assert event2 is None
        assert floor_2_warn_sent is True  # unchanged


# ---------------------------------------------------------------------------
# process_climate_event — zone_setpoint_miss.v1
# ---------------------------------------------------------------------------

CLIMATE_FLOOR_1 = "climate.floor_1_thermostat"
CLIMATE_FLOOR_3 = "climate.floor_3_thermostat"


def _make_attrs(setpoint=68.0, current_temp=65.0, hvac_action="heating"):
    return {
        "temperature": setpoint,
        "current_temperature": current_temp,
        "hvac_action": hvac_action,
    }


def _heating_start_state(entity_id, start_temp=64.0, setpoint=68.0, start_ts=None):
    """Return a climate_state dict simulating mid-heating session with no setpoint reached."""
    if start_ts is None:
        start_ts = TS - timedelta(minutes=30)
    return {
        entity_id: {
            "setpoint": setpoint,
            "current_temp": start_temp,
            "hvac_mode": "heat",
            "hvac_action": "heating",
            "heating_start_temp": start_temp,
            "heating_start_ts": start_ts,
            "setpoint_reached_ts": None,
            "setpoint_reached_temp": None,
            "post_setpoint_temps": [],
            "session_temps": [],
            "heating_start_other_zones": [],
            "setpoint_changed_during_heating": False,
        }
    }


class TestZoneSetpointMiss:
    """Tests for zone_setpoint_miss.v1 emission in process_climate_event."""

    def test_normal_miss_emits_setpoint_miss(self):
        """Heating ends without reaching setpoint — miss event is emitted."""
        entity_id = CLIMATE_FLOOR_1
        start_temp = 64.0
        setpoint = 68.0
        # Heating was active; now hvac_action leaves "heating"
        climate_state = _heating_start_state(entity_id, start_temp=start_temp, setpoint=setpoint)
        # Add some session temps to check closest_temp
        climate_state[entity_id]["session_temps"] = [64.5, 65.0, 65.5, 66.0]
        attrs = _make_attrs(setpoint=setpoint, current_temp=65.5, hvac_action="idle")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        miss_events = [e for e in events if e["schema"] == "homeops.consumer.zone_setpoint_miss.v1"]
        assert len(miss_events) == 1
        d = miss_events[0]["data"]
        assert d["entity_id"] == entity_id
        assert d["zone"] == "floor_1"
        assert d["start_temp"] == start_temp
        assert d["setpoint"] == setpoint
        assert d["setpoint_delta"] == pytest.approx(setpoint - start_temp)
        assert d["closest_temp"] == pytest.approx(66.0)  # max of session_temps
        assert d["delta"] == pytest.approx(setpoint - 66.0)
        assert d["outdoor_temp_f"] is None
        assert d["other_zones_calling"] == []

    def test_session_temps_tracks_closest_temp(self):
        """session_temps are accumulated during heating and used for closest_temp."""
        entity_id = CLIMATE_FLOOR_1
        start_temp = 63.0
        setpoint = 68.0
        climate_state = {entity_id: {}}

        # Step 1: Heating starts
        attrs_start = _make_attrs(setpoint=setpoint, current_temp=start_temp, hvac_action="heating")
        _, climate_state = process_climate_event(
            entity_id,
            attrs_start,
            (TS - timedelta(minutes=40)).isoformat(),
            climate_state,
            new_state="heat",
            floor_on_since=make_floor_on_since(),
        )

        # Step 2: Temperature rises but doesn't reach setpoint
        for temp in [63.5, 64.0, 65.0, 66.5]:
            attrs_mid = _make_attrs(setpoint=setpoint, current_temp=temp, hvac_action="heating")
            _, climate_state = process_climate_event(
                entity_id,
                attrs_mid,
                (TS - timedelta(minutes=20)).isoformat(),
                climate_state,
                new_state="heat",
            )

        # Step 3: Heating ends without reaching setpoint
        attrs_end = _make_attrs(setpoint=setpoint, current_temp=66.5, hvac_action="idle")
        events, _ = process_climate_event(
            entity_id,
            attrs_end,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        miss_events = [e for e in events if e["schema"] == "homeops.consumer.zone_setpoint_miss.v1"]
        assert len(miss_events) == 1
        assert miss_events[0]["data"]["closest_temp"] == pytest.approx(66.5)

    def test_miss_with_thermostat_adjustment_likely_cause_field(self):
        """zone_setpoint_miss.v1 includes likely_cause='thermostat_adjustment' when setpoint changed."""  # noqa: E501
        entity_id = CLIMATE_FLOOR_1
        climate_state = _heating_start_state(entity_id, start_temp=65.0, setpoint=70.0)
        climate_state[entity_id]["setpoint_changed_during_heating"] = True
        attrs = _make_attrs(setpoint=70.0, current_temp=67.0, hvac_action="idle")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        miss_events = [e for e in events if e["schema"] == "homeops.consumer.zone_setpoint_miss.v1"]
        assert len(miss_events) == 1
        d = miss_events[0]["data"]
        assert d["likely_cause"] == "thermostat_adjustment"
        assert d["start_temp"] == pytest.approx(65.0)
        assert d["setpoint"] == pytest.approx(70.0)

    def test_miss_with_other_zones_calling(self):
        """other_zones_calling reflects heating_start_other_zones."""
        entity_id = CLIMATE_FLOOR_1
        climate_state = _heating_start_state(entity_id, start_temp=64.0, setpoint=68.0)
        climate_state[entity_id]["heating_start_other_zones"] = [
            "binary_sensor.floor_2_heating_call",
            "binary_sensor.floor_3_heating_call",
        ]
        attrs = _make_attrs(setpoint=68.0, current_temp=65.5, hvac_action="idle")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        miss_events = [e for e in events if e["schema"] == "homeops.consumer.zone_setpoint_miss.v1"]
        assert len(miss_events) == 1
        assert miss_events[0]["data"]["other_zones_calling"] == [
            "binary_sensor.floor_2_heating_call",
            "binary_sensor.floor_3_heating_call",
        ]

    def test_no_miss_when_setpoint_was_reached(self):
        """When setpoint_reached_ts is set, zone_overshoot fires instead of miss."""
        entity_id = CLIMATE_FLOOR_1
        setpoint_reached = TS - timedelta(minutes=5)
        climate_state = _heating_start_state(entity_id, start_temp=64.0, setpoint=68.0)
        climate_state[entity_id]["setpoint_reached_ts"] = setpoint_reached
        climate_state[entity_id]["setpoint_reached_temp"] = 68.1
        climate_state[entity_id]["post_setpoint_temps"] = [68.1, 68.3]
        attrs = _make_attrs(setpoint=68.0, current_temp=68.3, hvac_action="idle")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        miss_schemas = [e["schema"] for e in events if "miss" in e["schema"]]
        overshoot_schemas = [e["schema"] for e in events if "overshoot" in e["schema"]]
        assert miss_schemas == []
        assert len(overshoot_schemas) == 1

    def test_miss_edge_case_empty_session_temps_uses_start_temp(self):
        """When session_temps is empty, closest_temp falls back to start_temp."""
        entity_id = CLIMATE_FLOOR_1
        start_temp = 64.0
        setpoint = 68.0
        climate_state = _heating_start_state(entity_id, start_temp=start_temp, setpoint=setpoint)
        # session_temps is [] (already the default in _heating_start_state)
        attrs = _make_attrs(setpoint=setpoint, current_temp=64.0, hvac_action="idle")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        miss_events = [e for e in events if e["schema"] == "homeops.consumer.zone_setpoint_miss.v1"]
        assert len(miss_events) == 1
        d = miss_events[0]["data"]
        assert d["closest_temp"] == pytest.approx(start_temp)
        assert d["delta"] == pytest.approx(setpoint - start_temp)

    def test_miss_outdoor_temp_populated(self):
        """outdoor_temp_f is taken from daily_state['last_outdoor_temp_f']."""
        entity_id = CLIMATE_FLOOR_1
        climate_state = _heating_start_state(entity_id, start_temp=63.0, setpoint=68.0)
        attrs = _make_attrs(setpoint=68.0, current_temp=65.0, hvac_action="idle")
        daily_state = {"last_outdoor_temp_f": 22.5}
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
            daily_state=daily_state,
        )
        miss_events = [e for e in events if e["schema"] == "homeops.consumer.zone_setpoint_miss.v1"]
        assert len(miss_events) == 1
        assert miss_events[0]["data"]["outdoor_temp_f"] == pytest.approx(22.5)

    def test_no_miss_emitted_without_undershoot_event(self):
        """zone_undershoot.v1 should NOT appear — it has been replaced by zone_setpoint_miss.v1."""
        entity_id = CLIMATE_FLOOR_1
        climate_state = _heating_start_state(entity_id, start_temp=64.0, setpoint=68.0)
        attrs = _make_attrs(setpoint=68.0, current_temp=65.5, hvac_action="idle")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        undershoot_schemas = [e["schema"] for e in events if "undershoot" in e["schema"]]
        assert undershoot_schemas == []


# ---------------------------------------------------------------------------
# _save_state / _load_state
# ---------------------------------------------------------------------------

FLOOR_1 = "binary_sensor.floor_1_heating_call"
FLOOR_2 = "binary_sensor.floor_2_heating_call"
FLOOR_3 = "binary_sensor.floor_3_heating_call"


class TestSaveState:
    def _base_state(self):
        floor_on_since = {FLOOR_1: None, FLOOR_2: None, FLOOR_3: None}
        furnace_on_since = None
        climate_state = {}
        daily_state = {
            "furnace_runtime_s": 0,
            "session_count": 0,
            "floor_runtime_s": {},
            "outdoor_temps": [],
            "last_outdoor_temp_f": None,
        }
        return floor_on_since, furnace_on_since, climate_state, daily_state

    def test_writes_correct_json_structure(self, tmp_path):
        sf = tmp_path / "state.json"
        fos, furnace, cs, daily = self._base_state()
        _save_state(fos, furnace, cs, daily, state_file=sf)
        assert sf.exists()
        data = json.loads(sf.read_text())
        assert "floor_on_since" in data
        assert "furnace_on_since" in data
        assert "climate_state" in data
        assert "daily_state" in data
        assert "saved_at" in data
        assert "shutdown_ts" not in data

    def test_serializes_floor_on_since_datetimes(self, tmp_path):
        sf = tmp_path / "state.json"
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        fos = {FLOOR_1: ts, FLOOR_2: None, FLOOR_3: None}
        _save_state(fos, None, {}, {}, state_file=sf)
        data = json.loads(sf.read_text())
        assert data["floor_on_since"][FLOOR_1] == ts.isoformat()
        assert data["floor_on_since"][FLOOR_2] is None

    def test_serializes_furnace_on_since(self, tmp_path):
        sf = tmp_path / "state.json"
        ts = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
        _save_state({}, ts, {}, {}, state_file=sf)
        data = json.loads(sf.read_text())
        assert data["furnace_on_since"] == ts.isoformat()

    def test_serializes_climate_state_datetime_fields(self, tmp_path):
        sf = tmp_path / "state.json"
        hts = datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC)
        cs = {
            "climate.floor_1_thermostat": {
                "setpoint": 68.0,
                "current_temp": 65.0,
                "hvac_action": "heating",
                "heating_start_ts": hts,
                "setpoint_reached_ts": None,
                "post_setpoint_temps": [],
                "session_temps": [],
            }
        }
        _save_state({}, None, cs, {}, state_file=sf)
        data = json.loads(sf.read_text())
        cs_saved = data["climate_state"]["climate.floor_1_thermostat"]
        assert cs_saved["heating_start_ts"] == hts.isoformat()
        assert cs_saved["setpoint_reached_ts"] is None

    def test_atomic_write_uses_tmp_then_rename(self, tmp_path):
        sf = tmp_path / "state.json"
        _save_state({}, None, {}, {}, state_file=sf)
        # After write, only the final file exists (tmp is renamed away)
        assert sf.exists()
        assert not sf.with_suffix(".tmp").exists()


class TestLoadState:
    def _write_state(self, path, saved_at_override=None):
        from datetime import UTC, datetime

        saved_at = saved_at_override or datetime.now(UTC).isoformat()
        payload = {
            "floor_on_since": {},
            "furnace_on_since": None,
            "climate_state": {},
            "daily_state": {},
            "saved_at": saved_at,
        }
        path.write_text(json.dumps(payload))

    def test_returns_none_for_missing_file(self, tmp_path):
        sf = tmp_path / "state.json"
        assert _load_state(state_file=sf) is None

    def test_returns_none_for_stale_file(self, tmp_path):
        sf = tmp_path / "state.json"
        # 63 minutes ago — older than 3720 s limit
        stale_ts = datetime(2024, 1, 15, 8, 0, 0, tzinfo=UTC)
        self._write_state(sf, saved_at_override=stale_ts.isoformat())
        # Mock "now" to be 63 min after saved_at
        fake_now = datetime(2024, 1, 15, 9, 3, 0, tzinfo=UTC)
        with mock.patch("consumer.datetime") as mock_dt:
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = _load_state(state_file=sf)
        assert result is None

    def test_returns_dict_for_recent_file(self, tmp_path):
        sf = tmp_path / "state.json"
        recent_ts = datetime.now(UTC).isoformat()
        self._write_state(sf, saved_at_override=recent_ts)
        result = _load_state(state_file=sf)
        assert result is not None
        assert "floor_on_since" in result

    def test_returns_none_when_saved_at_missing(self, tmp_path):
        sf = tmp_path / "state.json"
        sf.write_text(json.dumps({"floor_on_since": {}}))
        assert _load_state(state_file=sf) is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        sf = tmp_path / "state.json"
        sf.write_text("not json {{{")
        assert _load_state(state_file=sf) is None

    def test_round_trip_preserves_state(self, tmp_path):
        sf = tmp_path / "state.json"
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        fos = {FLOOR_1: ts, FLOOR_2: None, FLOOR_3: None}
        furnace = datetime(2024, 1, 15, 9, 30, 0, tzinfo=UTC)
        daily = {"furnace_runtime_s": 300, "session_count": 1}
        _save_state(fos, furnace, {}, daily, state_file=sf)
        loaded = _load_state(state_file=sf)
        assert loaded is not None
        assert loaded["furnace_on_since"] == furnace.isoformat()
        assert loaded["floor_on_since"][FLOOR_1] == ts.isoformat()
        assert loaded["daily_state"]["furnace_runtime_s"] == 300


# ---------------------------------------------------------------------------
# State restoration on startup
# ---------------------------------------------------------------------------


class TestStateRestoration:
    def test_floor_on_since_restored_with_datetime(self, tmp_path):
        """floor_on_since datetimes are correctly deserialized from ISO strings."""

        sf = tmp_path / "state.json"
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        fos = {FLOOR_1: ts, FLOOR_2: None, FLOOR_3: None}
        _save_state(fos, None, {}, {}, state_file=sf)
        loaded = _load_state(state_file=sf)
        assert loaded is not None
        from consumer import _parse_dt

        restored_fos = {k: _parse_dt(v) for k, v in loaded["floor_on_since"].items()}
        assert restored_fos[FLOOR_1] == ts
        assert restored_fos[FLOOR_2] is None

    def test_climate_state_datetime_fields_restored(self, tmp_path):
        from consumer import _parse_dt

        sf = tmp_path / "state.json"
        hts = datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC)
        cs = {
            "climate.floor_1_thermostat": {
                "setpoint": 68.0,
                "current_temp": 65.0,
                "hvac_action": "heating",
                "heating_start_ts": hts,
                "setpoint_reached_ts": None,
                "post_setpoint_temps": [],
                "session_temps": [],
            }
        }
        _save_state({}, None, cs, {}, state_file=sf)
        loaded = _load_state(state_file=sf)
        assert loaded is not None
        raw_cs = loaded["climate_state"]["climate.floor_1_thermostat"]
        assert _parse_dt(raw_cs["heating_start_ts"]) == hts
        assert _parse_dt(raw_cs["setpoint_reached_ts"]) is None

    def test_daily_state_intact_after_round_trip(self, tmp_path):
        sf = tmp_path / "state.json"
        daily = {
            "furnace_runtime_s": 1800,
            "session_count": 3,
            "floor_runtime_s": {FLOOR_1: 900},
            "outdoor_temps": [32.0, 33.5],
            "last_outdoor_temp_f": 33.5,
        }
        _save_state({}, None, {}, daily, state_file=sf)
        loaded = _load_state(state_file=sf)
        assert loaded["daily_state"] == daily


# ---------------------------------------------------------------------------
# across_restart flag via _emit_derived
# ---------------------------------------------------------------------------


class TestAcrossRestart:
    def _make_event(self, schema="homeops.consumer.floor_call_started.v1"):
        return {
            "schema": schema,
            "source": "consumer.v1",
            "ts": TS.isoformat(),
            "data": {"floor": "floor_1"},
        }

    def test_across_restart_true_on_first_event_when_fresh_restart(self, tmp_path):
        derived_log = str(tmp_path / "events.jsonl")
        evt = self._make_event()
        new_fresh = _emit_derived(evt, derived_log, fresh_restart=True)
        assert evt["data"].get("across_restart") is True
        assert new_fresh is True  # not cleared by a non-terminal event

    def test_across_restart_not_added_when_not_fresh_restart(self, tmp_path):
        derived_log = str(tmp_path / "events.jsonl")
        evt = self._make_event()
        _emit_derived(evt, derived_log, fresh_restart=False)
        assert "across_restart" not in evt["data"]

    def test_fresh_restart_cleared_after_zone_time_to_temp(self, tmp_path):
        derived_log = str(tmp_path / "events.jsonl")
        evt = self._make_event("homeops.consumer.zone_time_to_temp.v1")
        new_fresh = _emit_derived(evt, derived_log, fresh_restart=True)
        assert evt["data"].get("across_restart") is True
        assert new_fresh is False  # cleared

    def test_fresh_restart_cleared_after_zone_setpoint_miss(self, tmp_path):
        derived_log = str(tmp_path / "events.jsonl")
        evt = self._make_event("homeops.consumer.zone_setpoint_miss.v1")
        new_fresh = _emit_derived(evt, derived_log, fresh_restart=True)
        assert new_fresh is False

    def test_fresh_restart_not_cleared_by_non_terminal_schemas(self, tmp_path):
        derived_log = str(tmp_path / "events.jsonl")
        for schema in [
            "homeops.consumer.floor_call_started.v1",
            "homeops.consumer.heating_session_started.v1",
            "homeops.consumer.thermostat_setpoint_changed.v1",
            "homeops.consumer.outdoor_temp_updated.v1",
        ]:
            evt = self._make_event(schema)
            new_fresh = _emit_derived(evt, derived_log, fresh_restart=True)
            assert new_fresh is True, f"Expected fresh_restart to remain True for {schema}"

    def test_events_written_to_log(self, tmp_path):
        derived_log = str(tmp_path / "events.jsonl")
        evt = self._make_event()
        _emit_derived(evt, derived_log, fresh_restart=False)
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        written = json.loads(lines[0])
        assert written["schema"] == evt["schema"]


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------


class TestSigtermHandler:
    def test_writes_shutdown_ts_to_existing_state_file(self, tmp_path):
        sf = tmp_path / "state.json"
        sf.write_text(json.dumps({"saved_at": datetime.now(UTC).isoformat()}))
        _register_sigterm_handler(state_file=sf)
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
        state = json.loads(sf.read_text())
        assert "shutdown_ts" in state
        assert "saved_at" in state  # original content preserved

    def test_writes_shutdown_ts_when_no_state_file(self, tmp_path):
        sf = tmp_path / "state.json"
        _register_sigterm_handler(state_file=sf)
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
        state = json.loads(sf.read_text())
        assert "shutdown_ts" in state

    def test_exits_cleanly_on_sigterm(self, tmp_path):
        sf = tmp_path / "state.json"
        _register_sigterm_handler(state_file=sf)
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as exc_info:
            handler(signal.SIGTERM, None)
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# check_observer_silence
# ---------------------------------------------------------------------------


class TestCheckObserverSilence:
    """Tests for the observer heartbeat silence watchdog."""

    THRESHOLD_S = 600  # 10 min

    def _now(self):
        return datetime.now(UTC)

    def test_observer_silence_warning_not_emitted_before_threshold(self):
        """No warning when silence duration is below the threshold."""
        now = self._now()
        last_event_ts = now - timedelta(seconds=self.THRESHOLD_S - 1)
        event, sent = check_observer_silence(last_event_ts, False, self.THRESHOLD_S, now)
        assert event is None
        assert sent is False

    def test_observer_silence_warning_emitted_after_threshold(self):
        """Warning fires and flag is set when silence exceeds threshold."""
        now = self._now()
        last_event_ts = now - timedelta(seconds=self.THRESHOLD_S + 120)
        event, sent = check_observer_silence(last_event_ts, False, self.THRESHOLD_S, now)
        assert event is not None
        assert sent is True
        assert event["schema"] == "homeops.consumer.observer_silence_warning.v1"
        assert event["source"] == "consumer.v1"
        data = event["data"]
        assert data["silence_s"] >= self.THRESHOLD_S
        assert data["threshold_s"] == self.THRESHOLD_S
        assert data["last_event_ts"] == last_event_ts.isoformat()

    def test_observer_silence_warning_not_emitted_twice(self):
        """Once silence_sent=True, no further warnings are emitted."""
        now = self._now()
        last_event_ts = now - timedelta(seconds=self.THRESHOLD_S + 300)
        # First call fires.
        event1, sent1 = check_observer_silence(last_event_ts, False, self.THRESHOLD_S, now)
        assert event1 is not None
        assert sent1 is True
        # Second call with sent=True — should be suppressed.
        event2, sent2 = check_observer_silence(last_event_ts, sent1, self.THRESHOLD_S, now)
        assert event2 is None
        assert sent2 is True  # flag unchanged

    def test_observer_silence_warning_resets_after_new_event(self):
        """After observer_silence_sent resets, watchdog re-arms for subsequent episodes."""
        now = self._now()
        last_event_ts = now - timedelta(seconds=self.THRESHOLD_S + 60)
        # Fire once.
        event, sent = check_observer_silence(last_event_ts, False, self.THRESHOLD_S, now)
        assert event is not None
        assert sent is True
        # Simulate a new event arriving: consumer resets sent to False and updates last_event_ts.
        sent = False
        last_event_ts = now  # fresh event just arrived
        # Immediately after reset, silence_s should be ~0 — no new warning.
        event2, sent2 = check_observer_silence(last_event_ts, sent, self.THRESHOLD_S, now)
        assert event2 is None
        assert sent2 is False

    def test_observer_silence_warning_not_emitted_when_last_event_ts_none(self):
        """No warning when last_observer_event_ts is None (consumer just started, no events yet)."""
        now = self._now()
        event, sent = check_observer_silence(None, False, self.THRESHOLD_S, now)
        assert event is None
        assert sent is False


# ---------------------------------------------------------------------------
# zone_slow_to_heat_warning.v1
# ---------------------------------------------------------------------------

CLIMATE_FLOOR_2 = "climate.floor_2_thermostat"

THRESHOLD_FLOOR_1 = SLOW_TO_HEAT_THRESHOLDS_S["floor_1"]  # 900 s default
THRESHOLD_FLOOR_2 = SLOW_TO_HEAT_THRESHOLDS_S["floor_2"]  # 1800 s default


def _slow_to_heat_state(entity_id, start_temp=64.0, setpoint=68.0, elapsed_s=0, slow_sent=False):
    """Return a climate_state dict simulating an in-progress heating session."""
    start_ts = TS - timedelta(seconds=elapsed_s)
    return {
        entity_id: {
            "setpoint": setpoint,
            "current_temp": start_temp,
            "hvac_mode": "heat",
            "hvac_action": "heating",
            "heating_start_temp": start_temp,
            "heating_start_ts": start_ts,
            "setpoint_reached_ts": None,
            "setpoint_reached_temp": None,
            "post_setpoint_temps": [],
            "session_temps": [],
            "heating_start_other_zones": [],
            "setpoint_changed_during_heating": False,
            "slow_to_heat_sent": slow_sent,
        }
    }


class TestZoneSlowToHeatWarning:
    """Tests for zone_slow_to_heat_warning.v1 emission in process_climate_event."""

    def test_slow_to_heat_warning_not_emitted_before_threshold(self):
        """No warning when elapsed time is below the per-floor threshold."""
        entity_id = CLIMATE_FLOOR_1
        elapsed_s = THRESHOLD_FLOOR_1 - 1  # one second short
        climate_state = _slow_to_heat_state(
            entity_id, start_temp=64.0, setpoint=68.0, elapsed_s=elapsed_s
        )
        attrs = _make_attrs(setpoint=68.0, current_temp=65.0, hvac_action="heating")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        warn_events = [
            e for e in events if e["schema"] == "homeops.consumer.zone_slow_to_heat_warning.v1"
        ]
        assert warn_events == []

    def test_slow_to_heat_warning_emitted_after_threshold(self):
        """Warning fires when elapsed time meets or exceeds the per-floor threshold."""
        entity_id = CLIMATE_FLOOR_2
        elapsed_s = THRESHOLD_FLOOR_2 + 60  # 1 minute over
        climate_state = _slow_to_heat_state(
            entity_id, start_temp=64.0, setpoint=68.0, elapsed_s=elapsed_s
        )
        attrs = _make_attrs(setpoint=68.0, current_temp=65.0, hvac_action="heating")
        events, updated = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        warn_events = [
            e for e in events if e["schema"] == "homeops.consumer.zone_slow_to_heat_warning.v1"
        ]
        assert len(warn_events) == 1
        d = warn_events[0]["data"]
        assert d["zone"] == "floor_2"
        assert d["entity_id"] == entity_id
        assert d["elapsed_s"] >= THRESHOLD_FLOOR_2
        assert d["threshold_s"] == THRESHOLD_FLOOR_2
        assert d["start_temp"] == pytest.approx(64.0)
        assert d["current_temp"] == pytest.approx(65.0)
        assert d["setpoint"] == pytest.approx(68.0)
        assert d["setpoint_delta"] == pytest.approx(4.0)
        assert d["degrees_gained"] == pytest.approx(1.0)
        assert d["outdoor_temp_f"] is None
        # Flag is persisted so the warning won't fire again this session.
        assert updated[entity_id]["slow_to_heat_sent"] is True

    def test_slow_to_heat_warning_not_emitted_if_setpoint_already_reached(self):
        """No warning when setpoint_reached_ts is set (zone already made it)."""
        entity_id = CLIMATE_FLOOR_1
        elapsed_s = THRESHOLD_FLOOR_1 + 300
        start_ts = TS - timedelta(seconds=elapsed_s)
        climate_state = {
            entity_id: {
                "setpoint": 68.0,
                "current_temp": 68.0,
                "hvac_mode": "heat",
                "hvac_action": "heating",
                "heating_start_temp": 64.0,
                "heating_start_ts": start_ts,
                "setpoint_reached_ts": start_ts + timedelta(seconds=700),
                "setpoint_reached_temp": 68.0,
                "post_setpoint_temps": [68.0],
                "session_temps": [65.0, 66.0, 68.0],
                "heating_start_other_zones": [],
                "setpoint_changed_during_heating": False,
                "slow_to_heat_sent": False,
            }
        }
        attrs = _make_attrs(setpoint=68.0, current_temp=68.5, hvac_action="heating")
        events, _ = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        warn_events = [
            e for e in events if e["schema"] == "homeops.consumer.zone_slow_to_heat_warning.v1"
        ]
        assert warn_events == []

    def test_slow_to_heat_warning_not_emitted_twice_per_session(self):
        """Warning fires at most once per heating session (slow_to_heat_sent flag)."""
        entity_id = CLIMATE_FLOOR_1
        elapsed_s = THRESHOLD_FLOOR_1 + 300
        # slow_to_heat_sent already True from a prior call.
        climate_state = _slow_to_heat_state(
            entity_id, start_temp=64.0, setpoint=68.0, elapsed_s=elapsed_s, slow_sent=True
        )
        attrs = _make_attrs(setpoint=68.0, current_temp=65.0, hvac_action="heating")
        events, updated = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
        )
        warn_events = [
            e for e in events if e["schema"] == "homeops.consumer.zone_slow_to_heat_warning.v1"
        ]
        assert warn_events == []
        assert updated[entity_id]["slow_to_heat_sent"] is True  # flag unchanged

    def test_slow_to_heat_warning_resets_on_new_session(self):
        """slow_to_heat_sent is cleared when a new heating session starts."""
        entity_id = CLIMATE_FLOOR_1
        # Previous session had the flag set and hvac went idle.
        climate_state = {
            entity_id: {
                "setpoint": 68.0,
                "current_temp": 68.0,
                "hvac_mode": "heat",
                "hvac_action": "idle",  # session ended
                "heating_start_temp": None,
                "heating_start_ts": None,
                "setpoint_reached_ts": None,
                "setpoint_reached_temp": None,
                "post_setpoint_temps": [],
                "session_temps": [],
                "heating_start_other_zones": [],
                "setpoint_changed_during_heating": False,
                "slow_to_heat_sent": True,  # was set in prior session
            }
        }
        # New heating session starts.
        attrs = _make_attrs(setpoint=68.0, current_temp=64.0, hvac_action="heating")
        _, updated = process_climate_event(
            entity_id,
            attrs,
            TS.isoformat(),
            climate_state,
            new_state="heat",
            floor_on_since=make_floor_on_since(),
        )
        assert updated[entity_id]["slow_to_heat_sent"] is False


# ---------------------------------------------------------------------------
# emit_daily_summary — new fields
# ---------------------------------------------------------------------------


class TestEmitDailySummaryNewFields:
    """Tests for new fields added in the daily-efficiency-summary feature."""

    def _make_state(
        self,
        outdoor_temps=None,
        per_floor_session_count=None,
        warnings_triggered=None,
        floor_runtime_s=None,
        session_count=3,
        furnace_runtime_s=7200,
    ) -> dict:
        state = _empty_daily_state()
        state["session_count"] = session_count
        state["furnace_runtime_s"] = furnace_runtime_s
        if outdoor_temps is not None:
            state["outdoor_temps"] = outdoor_temps
        if per_floor_session_count is not None:
            state["per_floor_session_count"] = per_floor_session_count
        if warnings_triggered is not None:
            state["warnings_triggered"] = warnings_triggered
        if floor_runtime_s is not None:
            state["floor_runtime_s"] = floor_runtime_s
        return state

    def test_outdoor_temp_avg_calculated_correctly(self):
        """outdoor_temp_avg_f should be the mean of outdoor_temps, rounded to 1 dp."""
        state = self._make_state(outdoor_temps=[20.0, 30.0, 40.0])
        evt = emit_daily_summary(state, "2026-01-15")
        assert evt["data"]["outdoor_temp_avg_f"] == 30.0

    def test_outdoor_temp_avg_none_when_no_readings(self):
        """outdoor_temp_avg_f should be None when outdoor_temps list is empty."""
        state = self._make_state(outdoor_temps=[])
        evt = emit_daily_summary(state, "2026-01-15")
        assert evt["data"]["outdoor_temp_avg_f"] is None

    def test_per_floor_session_count_in_event(self):
        """per_floor_session_count should map floor names to session counts."""
        pfsc = {
            "binary_sensor.floor_1_heating_call": 4,
            "binary_sensor.floor_2_heating_call": 2,
            "binary_sensor.floor_3_heating_call": 1,
        }
        state = self._make_state(per_floor_session_count=pfsc)
        evt = emit_daily_summary(state, "2026-01-15")
        data = evt["data"]["per_floor_session_count"]
        assert data["floor_1"] == 4
        assert data["floor_2"] == 2
        assert data["floor_3"] == 1

    def test_warnings_triggered_passed_through(self):
        """warnings_triggered dict should appear in event data."""
        warnings = {
            "floor_2_long_call": 2,
            "floor_no_response": 1,
            "zone_slow_to_heat": 0,
            "observer_silence": 0,
            "setpoint_miss": 3,
        }
        state = self._make_state(warnings_triggered=warnings)
        evt = emit_daily_summary(state, "2026-01-15")
        assert evt["data"]["warnings_triggered"] == warnings

    def test_empty_daily_state_has_new_fields(self):
        """_empty_daily_state() must contain per_floor_session_count and warnings_triggered."""
        state = _empty_daily_state()
        assert "per_floor_session_count" in state
        assert "warnings_triggered" in state
        assert state["per_floor_session_count"] == {
            "binary_sensor.floor_1_heating_call": 0,
            "binary_sensor.floor_2_heating_call": 0,
            "binary_sensor.floor_3_heating_call": 0,
        }
        assert state["warnings_triggered"] == {
            "floor_2_long_call": 0,
            "floor_2_escalation": 0,
            "floor_no_response": 0,
            "zone_slow_to_heat": 0,
            "observer_silence": 0,
            "setpoint_miss": 0,
        }


# ---------------------------------------------------------------------------
# format_daily_summary_message
# ---------------------------------------------------------------------------


class TestFormatDailySummaryMessage:
    """Tests for the format_daily_summary_message helper."""

    def _base_data(self, **overrides) -> dict:
        data = {
            "date": "2026-01-15",
            "total_furnace_runtime_s": 7200,  # 2h 0m
            "session_count": 5,
            "per_floor_runtime_s": {
                "floor_1": 3600,
                "floor_2": 1800,
                "floor_3": 1200,
            },
            "per_floor_session_count": {
                "floor_1": 3,
                "floor_2": 1,
                "floor_3": 1,
            },
            "outdoor_temp_min_f": 22.0,
            "outdoor_temp_max_f": 38.0,
            "outdoor_temp_avg_f": 30.5,
            "warnings_triggered": {
                "floor_2_long_call": 0,
                "floor_no_response": 0,
                "zone_slow_to_heat": 0,
                "observer_silence": 0,
                "setpoint_miss": 0,
            },
        }
        data.update(overrides)
        return data

    def test_header_contains_date(self):
        """Message should start with summary header containing the date."""
        msg = format_daily_summary_message(self._base_data())
        assert "📊 Daily Heating Summary — 2026-01-15" in msg

    def test_outdoor_temp_line_present(self):
        """Outdoor temp line should show min, max, and avg when data is available."""
        msg = format_daily_summary_message(self._base_data())
        assert "🌡️ Outdoor temp:" in msg
        assert "22°F" in msg
        assert "38°F" in msg
        assert "30.5°F" in msg

    def test_outdoor_temp_line_omitted_when_all_none(self):
        """Outdoor temp line should be omitted when all outdoor temp values are None."""
        data = self._base_data(
            outdoor_temp_min_f=None, outdoor_temp_max_f=None, outdoor_temp_avg_f=None
        )
        msg = format_daily_summary_message(data)
        assert "🌡️" not in msg

    def test_zero_warnings_shows_none_checkmark(self):
        """When all warnings are zero, the message should say 'None ✅'."""
        msg = format_daily_summary_message(self._base_data())
        assert "None ✅" in msg

    def test_warnings_shown_when_nonzero(self):
        """Non-zero warnings should be listed individually."""
        data = self._base_data(
            warnings_triggered={
                "floor_2_long_call": 2,
                "floor_no_response": 0,
                "zone_slow_to_heat": 1,
                "observer_silence": 0,
                "setpoint_miss": 0,
            }
        )
        msg = format_daily_summary_message(data)
        assert "Floor-2 long call: 2" in msg
        assert "Slow to heat: 1" in msg
        # Zero-count lines should be omitted
        assert "Floor no-response" not in msg
        assert "Observer silence" not in msg

    def test_floor2_avg_warning_appended_when_over_30min(self):
        """Floor 2 line should append ⚠️ when avg session > 30 minutes."""
        # floor_2: 1 session, 2000s runtime = ~33m avg (> 30m threshold)
        data = self._base_data(
            per_floor_runtime_s={"floor_1": 3600, "floor_2": 2000, "floor_3": 1200},
            per_floor_session_count={"floor_1": 3, "floor_2": 1, "floor_3": 1},
        )
        msg = format_daily_summary_message(data)
        lines = msg.splitlines()
        floor2_line = next(ln for ln in lines if "Floor 2" in ln)
        assert "⚠️" in floor2_line

    def test_floor2_avg_no_warning_when_under_30min(self):
        """Floor 2 line should NOT append ⚠️ when avg session ≤ 30 minutes."""
        # floor_2: 2 sessions, 3000s total = 1500s avg = 25m (under threshold)
        data = self._base_data(
            per_floor_runtime_s={"floor_1": 3600, "floor_2": 3000, "floor_3": 1200},
            per_floor_session_count={"floor_1": 3, "floor_2": 2, "floor_3": 1},
        )
        msg = format_daily_summary_message(data)
        lines = msg.splitlines()
        floor2_line = next(ln for ln in lines if "Floor 2" in ln)
        assert "⚠️" not in floor2_line

    def test_floor_with_zero_sessions_shows_no_avg(self):
        """A floor with 0 sessions should show '0 sessions' with no avg."""
        data = self._base_data(
            per_floor_runtime_s={"floor_1": 0, "floor_2": 0, "floor_3": 0},
            per_floor_session_count={"floor_1": 0, "floor_2": 0, "floor_3": 0},
        )
        msg = format_daily_summary_message(data)
        assert "Floor 1: 0 sessions" in msg
        assert "Floor 2: 0 sessions" in msg
        assert "Floor 3: 0 sessions" in msg

    def test_total_runtime_formatted_as_hours_and_minutes(self):
        """Total furnace runtime should be expressed as Xh Ym."""
        data = self._base_data(total_furnace_runtime_s=5400)  # 1h 30m
        msg = format_daily_summary_message(data)
        assert "1h 30m" in msg
