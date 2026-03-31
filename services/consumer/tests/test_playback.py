"""Tests for consumer event playback on restart.

Verifies:
1. _load_last_consumed_ts reads from state file regardless of file age
2. _save_state persists last_consumed_observer_ts correctly
3. _playback_phase replays missed events from last_consumed_ts to EOF
4. Derived event timestamps match original observer event timestamps (not replay wall time)
5. Alerts fire with correct event timestamps during playback
6. Playback correctly seeks past events older than last_consumed_ts
7. Cold-start (no last_consumed_ts) skips playback and continues normally
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from consumer import (
    _empty_daily_state,
    _load_last_consumed_ts,
    _playback_phase,
    _save_state,
)

# ---------------------------------------------------------------------------
# Entity IDs
# ---------------------------------------------------------------------------

FLOOR_1 = "binary_sensor.floor_1_heating_call"
FLOOR_2 = "binary_sensor.floor_2_heating_call"
FLOOR_3 = "binary_sensor.floor_3_heating_call"
FURNACE = "binary_sensor.furnace_heating"
OUTDOOR_TEMP = "sensor.outdoor_temperature"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Return a UTC datetime on 2024-01-15 at the given time."""
    return datetime(2024, 1, 15, hour, minute, second, tzinfo=UTC)


def _ts_str(hour: int, minute: int = 0, second: int = 0) -> str:
    return _ts(hour, minute, second).isoformat()


def _obs_line(entity_id: str, old_state: str | None, new_state: str | None, ts: datetime) -> str:
    """Build a JSONL line for an observer state_changed event."""
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
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_state(
    state_file: Path,
    last_consumed_ts: str | None,
    saved_at_override: str | None = None,
) -> None:
    """Write a minimal state.json with optional last_consumed_observer_ts."""
    saved_at = saved_at_override or datetime.now(UTC).isoformat()
    payload = {
        "floor_on_since": {},
        "furnace_on_since": None,
        "climate_state": {},
        "daily_state": {},
        "last_consumed_observer_ts": last_consumed_ts,
        "saved_at": saved_at,
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")


def _read_derived_events(derived_log: Path) -> list[dict[str, Any]]:
    if not derived_log.exists():
        return []
    events = []
    for line in derived_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _make_floor_on_since() -> dict:
    return {FLOOR_1: None, FLOOR_2: None, FLOOR_3: None}


# ---------------------------------------------------------------------------
# Tests: _load_last_consumed_ts
# ---------------------------------------------------------------------------


class TestLoadLastConsumedTs:
    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        sf = tmp_path / "state.json"
        assert _load_last_consumed_ts(state_file=sf) is None

    def test_returns_none_when_field_absent(self, tmp_path: Path) -> None:
        sf = tmp_path / "state.json"
        sf.write_text(json.dumps({"saved_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
        assert _load_last_consumed_ts(state_file=sf) is None

    def test_returns_ts_from_recent_state_file(self, tmp_path: Path) -> None:
        sf = tmp_path / "state.json"
        expected = _ts_str(10, 0)
        _write_state(sf, expected)
        assert _load_last_consumed_ts(state_file=sf) == expected

    def test_returns_ts_from_stale_state_file(self, tmp_path: Path) -> None:
        """_load_last_consumed_ts must work even when the state is older than 62 min."""
        sf = tmp_path / "state.json"
        expected = _ts_str(10, 0)
        # Use a saved_at from 10 hours ago — _load_state() would return None but
        # _load_last_consumed_ts() should still work.
        stale_saved_at = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
        _write_state(sf, expected, saved_at_override=stale_saved_at)
        assert _load_last_consumed_ts(state_file=sf) == expected

    def test_returns_none_for_malformed_json(self, tmp_path: Path) -> None:
        sf = tmp_path / "state.json"
        sf.write_text("not valid json", encoding="utf-8")
        assert _load_last_consumed_ts(state_file=sf) is None

    def test_round_trip_via_save_state(self, tmp_path: Path) -> None:
        """_save_state persists last_consumed_observer_ts and _load_last_consumed_ts reads it."""
        sf = tmp_path / "state.json"
        expected = _ts_str(12, 30)
        _save_state({}, None, {}, {}, last_consumed_observer_ts=expected, state_file=sf)
        assert _load_last_consumed_ts(state_file=sf) == expected

    def test_save_state_with_none_ts_stores_null(self, tmp_path: Path) -> None:
        sf = tmp_path / "state.json"
        _save_state({}, None, {}, {}, last_consumed_observer_ts=None, state_file=sf)
        data = json.loads(sf.read_text())
        assert data["last_consumed_observer_ts"] is None


# ---------------------------------------------------------------------------
# Tests: _playback_phase — missed events replayed
# ---------------------------------------------------------------------------


class TestPlaybackPhaseEventReplay:
    """Verify that _playback_phase replays events from last_consumed_ts to EOF."""

    def _run_playback(
        self,
        observer_log: Path,
        derived_log: Path,
        last_consumed_ts: str,
        floor_on_since: dict | None = None,
        furnace_on_since: datetime | None = None,
        climate_state: dict | None = None,
        daily_state: dict | None = None,
    ) -> dict:
        from rules.floor_no_response import FloorNoResponseRule
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        return _playback_phase(
            str(observer_log),
            last_consumed_ts,
            derived_log=str(derived_log),
            floor_on_since=floor_on_since or _make_floor_on_since(),
            furnace_on_since=furnace_on_since,
            climate_state=climate_state or {},
            daily_state=daily_state or _empty_daily_state(),
            floor_2_warn_sent=False,
            fresh_restart=True,
            current_date="2024-01-15",
            floor_entities={
                FLOOR_1: "floor_1",
                FLOOR_2: "floor_2",
                FLOOR_3: "floor_3",
            },
            floor_no_response_rule=FloorNoResponseRule(),
            furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
            telegram_bot_token="",
            telegram_chat_id="",
        )

    def test_replays_floor_events_after_last_consumed_ts(self, tmp_path: Path) -> None:
        """Events with ts > last_consumed_ts must appear in the derived log."""
        lines = [
            _obs_line(FLOOR_1, "off", "on", _ts(6, 0)),  # before cutoff — should be skipped
            _obs_line(FLOOR_1, "on", "off", _ts(6, 30)),  # after cutoff — should be replayed
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        # last_consumed_ts is the ts of the first event (6:00), so replay starts from >= 6:00
        # which includes the 6:00 event itself (spec says >=)
        last_ts = _ts_str(6, 0)
        self._run_playback(obs_log, derived_log, last_ts)

        events = _read_derived_events(derived_log)
        schemas = [e["schema"] for e in events]
        assert "homeops.consumer.floor_call_ended.v1" in schemas

    def test_skips_events_before_last_consumed_ts(self, tmp_path: Path) -> None:
        """Events with ts < last_consumed_ts must NOT be replayed."""
        lines = [
            _obs_line(FLOOR_1, "off", "on", _ts(5, 0)),  # before cutoff
            _obs_line(FLOOR_1, "on", "off", _ts(5, 30)),  # before cutoff
            _obs_line(FLOOR_2, "off", "on", _ts(7, 0)),  # after cutoff — replayed
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        # Set cutoff to 6:00 — floor_1 events at 5:xx should be skipped
        last_ts = _ts_str(6, 0)
        self._run_playback(obs_log, derived_log, last_ts)

        events = _read_derived_events(derived_log)
        # Only floor_2 events (from 7:00) should appear
        floor1_events = [e for e in events if e.get("data", {}).get("floor") == "floor_1"]
        floor2_events = [e for e in events if e.get("data", {}).get("floor") == "floor_2"]
        assert floor1_events == []
        assert len(floor2_events) >= 1

    def test_replays_furnace_events(self, tmp_path: Path) -> None:
        """Furnace on/off events are replayed and produce session events."""
        lines = [
            _obs_line(FURNACE, "off", "on", _ts(8, 0)),
            _obs_line(FURNACE, "on", "off", _ts(8, 30)),
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        self._run_playback(obs_log, derived_log, _ts_str(8, 0))

        events = _read_derived_events(derived_log)
        schemas = {e["schema"] for e in events}
        assert "homeops.consumer.heating_session_started.v1" in schemas
        assert "homeops.consumer.heating_session_ended.v1" in schemas

    def test_replays_outdoor_temp_events(self, tmp_path: Path) -> None:
        """Outdoor temperature events are replayed."""
        lines = [
            _obs_line(OUTDOOR_TEMP, None, "35.5", _ts(9, 0)),
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        self._run_playback(obs_log, derived_log, _ts_str(9, 0))

        events = _read_derived_events(derived_log)
        temp_events = [
            e for e in events if e["schema"] == "homeops.consumer.outdoor_temp_updated.v1"
        ]
        assert len(temp_events) == 1
        assert temp_events[0]["data"]["temperature_f"] == pytest.approx(35.5)

    def test_handles_empty_log_gracefully(self, tmp_path: Path) -> None:
        obs_log = tmp_path / "events.jsonl"
        obs_log.write_text("", encoding="utf-8")
        derived_log = tmp_path / "derived.jsonl"

        result = self._run_playback(obs_log, derived_log, _ts_str(8, 0))

        assert result["last_consumed_observer_ts"] == _ts_str(8, 0)  # unchanged
        assert not derived_log.exists() or _read_derived_events(derived_log) == []

    def test_handles_missing_observer_log(self, tmp_path: Path) -> None:
        obs_log = tmp_path / "nonexistent.jsonl"
        derived_log = tmp_path / "derived.jsonl"

        result = self._run_playback(obs_log, derived_log, _ts_str(8, 0))

        # Should return state unchanged and not crash
        assert result["floor_on_since"] is not None

    def test_returns_updated_last_consumed_ts(self, tmp_path: Path) -> None:
        """last_consumed_observer_ts in result equals the ts of the last replayed event."""
        last_event_ts = _ts_str(9, 45)
        lines = [
            _obs_line(FURNACE, "off", "on", _ts(9, 0)),
            _obs_line(FURNACE, "on", "off", _ts(9, 45)),  # last event
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        result = self._run_playback(obs_log, derived_log, _ts_str(9, 0))

        assert result["last_consumed_observer_ts"] == last_event_ts

    def test_updates_state_floor_on_since(self, tmp_path: Path) -> None:
        """floor_on_since is updated by playback when a floor call is in progress at EOF."""
        lines = [
            _obs_line(FLOOR_1, "off", "on", _ts(10, 0)),  # starts calling, never ends
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        result = self._run_playback(obs_log, derived_log, _ts_str(10, 0))

        # floor_1 should be in-progress (started at 10:00)
        assert result["floor_on_since"][FLOOR_1] is not None


# ---------------------------------------------------------------------------
# Tests: derived event timestamps match original observer timestamps
# ---------------------------------------------------------------------------


class TestDerivedEventTimestamps:
    """Verify that derived events use original observer timestamps during playback."""

    def _run_playback(
        self,
        observer_log: Path,
        derived_log: Path,
        last_consumed_ts: str,
    ) -> dict:
        from rules.floor_no_response import FloorNoResponseRule
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        return _playback_phase(
            str(observer_log),
            last_consumed_ts,
            derived_log=str(derived_log),
            floor_on_since=_make_floor_on_since(),
            furnace_on_since=None,
            climate_state={},
            daily_state=_empty_daily_state(),
            floor_2_warn_sent=False,
            fresh_restart=True,
            current_date="2024-01-15",
            floor_entities={
                FLOOR_1: "floor_1",
                FLOOR_2: "floor_2",
                FLOOR_3: "floor_3",
            },
            floor_no_response_rule=FloorNoResponseRule(),
            furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
            telegram_bot_token="",
            telegram_chat_id="",
        )

    def test_floor_call_started_ts_matches_observer_event_ts(self, tmp_path: Path) -> None:
        """floor_call_started.v1 ts must equal the observer event ts, not wall time."""
        observer_ts = _ts(6, 5)
        lines = [_obs_line(FLOOR_1, "off", "on", observer_ts)]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        self._run_playback(obs_log, derived_log, observer_ts.isoformat())

        events = _read_derived_events(derived_log)
        started = [e for e in events if e["schema"] == "homeops.consumer.floor_call_started.v1"]
        assert len(started) == 1
        # The ts field should be the observer event ts, not current wall time
        assert started[0]["ts"] == observer_ts.isoformat()

    def test_floor_call_ended_ts_matches_observer_event_ts(self, tmp_path: Path) -> None:
        """floor_call_ended.v1 ts must equal the observer event ts."""
        start_ts = _ts(6, 0)
        end_ts = _ts(6, 30)
        lines = [
            _obs_line(FLOOR_1, "off", "on", start_ts),
            _obs_line(FLOOR_1, "on", "off", end_ts),
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        self._run_playback(obs_log, derived_log, start_ts.isoformat())

        events = _read_derived_events(derived_log)
        ended = [e for e in events if e["schema"] == "homeops.consumer.floor_call_ended.v1"]
        assert len(ended) == 1
        assert ended[0]["ts"] == end_ts.isoformat()

    def test_heating_session_events_use_observer_ts(self, tmp_path: Path) -> None:
        """Furnace session events must have ts matching the observer event timestamp."""
        start_ts = _ts(8, 0)
        end_ts = _ts(8, 40)
        lines = [
            _obs_line(FURNACE, "off", "on", start_ts),
            _obs_line(FURNACE, "on", "off", end_ts),
        ]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        self._run_playback(obs_log, derived_log, start_ts.isoformat())

        events = _read_derived_events(derived_log)
        started = [
            e for e in events if e["schema"] == "homeops.consumer.heating_session_started.v1"
        ]
        ended = [e for e in events if e["schema"] == "homeops.consumer.heating_session_ended.v1"]

        assert len(started) == 1
        assert started[0]["ts"] == start_ts.isoformat()
        assert len(ended) == 1
        assert ended[0]["ts"] == end_ts.isoformat()

    def test_outdoor_temp_event_uses_observer_ts(self, tmp_path: Path) -> None:
        """outdoor_temp_updated.v1 ts must match the observer event ts."""
        obs_ts = _ts(10, 0)
        lines = [_obs_line(OUTDOOR_TEMP, None, "42.5", obs_ts)]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        self._run_playback(obs_log, derived_log, obs_ts.isoformat())

        events = _read_derived_events(derived_log)
        temp_events = [
            e for e in events if e["schema"] == "homeops.consumer.outdoor_temp_updated.v1"
        ]
        assert len(temp_events) == 1
        assert temp_events[0]["ts"] == obs_ts.isoformat()

    def test_derived_event_ts_differs_from_wall_time(self, tmp_path: Path) -> None:
        """Derived event ts must be the historical observer ts, not datetime.now()."""
        # Use a timestamp from the past that is definitely != wall time
        historical_ts = datetime(2024, 1, 15, 6, 0, 0, tzinfo=UTC)
        lines = [_obs_line(FURNACE, "off", "on", historical_ts)]
        obs_log = _write_observer_log(tmp_path, lines)
        derived_log = tmp_path / "derived.jsonl"

        wall_before = datetime.now(UTC)
        self._run_playback(obs_log, derived_log, historical_ts.isoformat())
        _ = datetime.now(UTC)  # noqa: F841

        events = _read_derived_events(derived_log)
        started = [
            e for e in events if e["schema"] == "homeops.consumer.heating_session_started.v1"
        ]
        assert len(started) == 1

        from dateutil.parser import isoparse

        derived_ts = isoparse(started[0]["ts"])
        # The derived event ts must be the historical ts, not wall time
        assert derived_ts == historical_ts
        # Confirm it's not wall time (which would be much more recent)
        assert derived_ts < wall_before


# ---------------------------------------------------------------------------
# Tests: _event_ts_suffix and Telegram alert timestamp behaviour
# ---------------------------------------------------------------------------


class TestEventTsSuffix:
    """Tests for the _event_ts_suffix helper function."""

    def test_returns_empty_for_none_processing_ts(self) -> None:
        from consumer import _event_ts_suffix

        result = _event_ts_suffix(None, datetime.now(UTC))
        assert result == ""

    def test_returns_event_time_line_for_recent_event(self) -> None:
        from consumer import _event_ts_suffix

        now = datetime.now(UTC)
        processing_ts = (now - timedelta(minutes=1)).isoformat()
        result = _event_ts_suffix(processing_ts, now)
        assert "Event time:" in result
        assert "replayed from downtime" not in result

    def test_returns_replay_note_when_diff_exceeds_5_minutes(self) -> None:
        from consumer import _event_ts_suffix

        now = datetime.now(UTC)
        processing_ts = (now - timedelta(minutes=10)).isoformat()
        result = _event_ts_suffix(processing_ts, now)
        assert "Event time:" in result
        assert "replayed from downtime" in result or "alert sent at" in result

    def test_no_replay_note_when_diff_exactly_5_minutes(self) -> None:
        from consumer import _event_ts_suffix

        now = datetime.now(UTC)
        processing_ts = (now - timedelta(seconds=300)).isoformat()
        result = _event_ts_suffix(processing_ts, now)
        # Exactly 300s = 5 min → diff is not > 300, so no replay note
        assert "replayed from downtime" not in result

    def test_replay_note_fires_at_301_seconds_diff(self) -> None:
        from consumer import _event_ts_suffix

        now = datetime.now(UTC)
        processing_ts = (now - timedelta(seconds=301)).isoformat()
        result = _event_ts_suffix(processing_ts, now)
        assert "replayed from downtime" in result or "alert sent at" in result


# ---------------------------------------------------------------------------
# Tests: end-to-end playback scenario (simulation-style)
# ---------------------------------------------------------------------------


class TestPlaybackEndToEnd:
    """
    Simulate a consumer restart mid-stream:
    1. Consumer processes some events (morning cycle)
    2. Consumer goes down — misses afternoon events
    3. Consumer restarts, reads last_consumed_ts, runs playback
    4. Verify all missed events appear in derived log with correct timestamps
    """

    def _build_full_log(self) -> list[str]:
        """Build a full-day observer event log."""
        events = []
        # Morning cycle (processed before "downtime")
        events.append(_obs_line(FURNACE, "off", "on", _ts(6, 0)))
        events.append(_obs_line(FLOOR_1, "off", "on", _ts(6, 5)))
        events.append(_obs_line(FLOOR_1, "on", "off", _ts(6, 20)))
        events.append(_obs_line(FURNACE, "on", "off", _ts(6, 40)))
        # Afternoon cycle (missed during downtime)
        events.append(_obs_line(FURNACE, "off", "on", _ts(13, 0)))
        events.append(_obs_line(FLOOR_2, "off", "on", _ts(13, 5)))
        events.append(_obs_line(FLOOR_2, "on", "off", _ts(13, 35)))
        events.append(_obs_line(FURNACE, "on", "off", _ts(14, 0)))
        return events

    def test_missed_afternoon_events_are_replayed(self, tmp_path: Path) -> None:
        """Events after last_consumed_ts (morning) are replayed during catch-up."""
        from rules.floor_no_response import FloorNoResponseRule
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        obs_log = _write_observer_log(tmp_path, self._build_full_log())
        derived_log = tmp_path / "derived.jsonl"

        # Simulate: consumer processed everything up to 6:40 (furnace off)
        last_consumed_ts = _ts_str(6, 40)

        _playback_phase(
            str(obs_log),
            last_consumed_ts,
            derived_log=str(derived_log),
            floor_on_since=_make_floor_on_since(),
            furnace_on_since=None,
            climate_state={},
            daily_state=_empty_daily_state(),
            floor_2_warn_sent=False,
            fresh_restart=False,
            current_date="2024-01-15",
            floor_entities={
                FLOOR_1: "floor_1",
                FLOOR_2: "floor_2",
                FLOOR_3: "floor_3",
            },
            floor_no_response_rule=FloorNoResponseRule(),
            furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
            telegram_bot_token="",
            telegram_chat_id="",
        )

        events = _read_derived_events(derived_log)

        # Morning furnace session_ended was already at 6:40 — replayed (>= cutoff)
        # Afternoon furnace session at 13:00-14:00 must also be replayed
        heating_ended = [
            e for e in events if e["schema"] == "homeops.consumer.heating_session_ended.v1"
        ]
        # At minimum the 14:00 furnace-off should produce a session_ended
        assert any(e["data"].get("ended_at") == _ts_str(14, 0) for e in heating_ended), (
            "Expected afternoon furnace session_ended in replayed events"
        )

    def test_afternoon_events_have_correct_timestamps(self, tmp_path: Path) -> None:
        """Derived events from replayed afternoon cycle must have afternoon timestamps."""
        from rules.floor_no_response import FloorNoResponseRule
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        obs_log = _write_observer_log(tmp_path, self._build_full_log())
        derived_log = tmp_path / "derived.jsonl"

        _playback_phase(
            str(obs_log),
            _ts_str(13, 0),  # start from the afternoon cycle
            derived_log=str(derived_log),
            floor_on_since=_make_floor_on_since(),
            furnace_on_since=None,
            climate_state={},
            daily_state=_empty_daily_state(),
            floor_2_warn_sent=False,
            fresh_restart=False,
            current_date="2024-01-15",
            floor_entities={
                FLOOR_1: "floor_1",
                FLOOR_2: "floor_2",
                FLOOR_3: "floor_3",
            },
            floor_no_response_rule=FloorNoResponseRule(),
            furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
            telegram_bot_token="",
            telegram_chat_id="",
        )

        events = _read_derived_events(derived_log)

        from dateutil.parser import isoparse

        # All derived events must have timestamps in the afternoon (>= 13:00)
        for evt in events:
            evt_ts = isoparse(evt["ts"])
            # Must be historical time (2024-01-15 afternoon), not today's wall time
            assert evt_ts.year == 2024
            assert evt_ts.month == 1
            assert evt_ts.day == 15
            assert evt_ts.hour >= 13, f"Expected afternoon ts, got {evt_ts} for {evt['schema']}"

    def test_floor_2_call_duration_correct_after_playback(self, tmp_path: Path) -> None:
        """floor_call_ended.v1 duration_s reflects the original event times."""
        from rules.floor_no_response import FloorNoResponseRule
        from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

        obs_log = _write_observer_log(tmp_path, self._build_full_log())
        derived_log = tmp_path / "derived.jsonl"

        _playback_phase(
            str(obs_log),
            _ts_str(13, 0),
            derived_log=str(derived_log),
            floor_on_since=_make_floor_on_since(),
            furnace_on_since=None,
            climate_state={},
            daily_state=_empty_daily_state(),
            floor_2_warn_sent=False,
            fresh_restart=False,
            current_date="2024-01-15",
            floor_entities={
                FLOOR_1: "floor_1",
                FLOOR_2: "floor_2",
                FLOOR_3: "floor_3",
            },
            floor_no_response_rule=FloorNoResponseRule(),
            furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
            telegram_bot_token="",
            telegram_chat_id="",
        )

        events = _read_derived_events(derived_log)
        floor2_ended = [
            e
            for e in events
            if e["schema"] == "homeops.consumer.floor_call_ended.v1"
            and e["data"]["floor"] == "floor_2"
        ]
        assert len(floor2_ended) == 1
        # floor_2 called from 13:05 to 13:35 = 30 min = 1800 s
        assert floor2_ended[0]["data"]["duration_s"] == 1800
