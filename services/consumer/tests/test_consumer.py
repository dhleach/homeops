"""Unit tests for consumer.py pure event-processing functions."""

import json
from datetime import UTC, datetime, timedelta

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
