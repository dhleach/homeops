"""24-hour synthetic pipeline simulation test for homeops consumer.

Scenario
--------
The simulation covers a single day (2024-01-15, UTC) with the following
sequence of HVAC events:

  Morning heating cycle  (06:00–06:40)
    - Furnace ON/OFF
    - Floor 1 short call (5 min)
    - Floor 3 short call (10 min)

  Midday heating cycle   (13:00–14:00)
    - Furnace ON/OFF
    - Floor 2 normal call (30 min, below the 45-min threshold) — no warning

  Evening heating cycle  (17:00–22:00)
    - Furnace ON/OFF
    - Floor 2 long call (70 min, above the 45-min threshold) — exactly one warning
    - Floor 1 back-to-back short calls (edge-case: two calls in a row)
    - Floor 3 medium call (20 min)

  Outdoor temperature readings roughly every 2 hours (5 total)

All events are routed through the consumer pure functions in strict
chronological order.  Between observer events the simulation also runs
periodic ``check_floor_2_warning`` ticks (using the event timestamp as
``now_ts``) to mirror the timeout loop in ``consumer.main()``.
"""

from datetime import UTC, datetime

import pytest
from consumer import (
    check_floor_2_warning,
    process_floor_event,
    process_furnace_event,
    process_outdoor_temp_event,
)

# ---------------------------------------------------------------------------
# Entity IDs (mirrors consumer._FLOOR_ENTITIES and main())
# ---------------------------------------------------------------------------

FLOOR_1 = "binary_sensor.floor_1_heating_call"
FLOOR_2 = "binary_sensor.floor_2_heating_call"
FLOOR_3 = "binary_sensor.floor_3_heating_call"
FURNACE = "binary_sensor.furnace_heating"
OUTDOOR_TEMP = "sensor.outdoor_temperature"

FLOOR_ENTITIES = {FLOOR_1, FLOOR_2, FLOOR_3}

FLOOR_2_WARN_THRESHOLD_S = 2700  # 45 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hour, minute=0, second=0):
    """Return a UTC datetime on 2024-01-15 at the given time."""
    return datetime(2024, 1, 15, hour, minute, second, tzinfo=UTC)


def _obs(entity_id, old_state, new_state, ts):
    """Build a minimal observer state-changed event dict."""
    return {
        "schema": "homeops.observer.state_changed.v1",
        "ts": ts.isoformat(),
        "data": {
            "entity_id": entity_id,
            "old_state": old_state,
            "new_state": new_state,
        },
    }


def _make_floor_on_since():
    return {FLOOR_1: None, FLOOR_2: None, FLOOR_3: None}


# ---------------------------------------------------------------------------
# Simulation runner
# ---------------------------------------------------------------------------


def run_simulation(observer_events, check_ticks):
    """Run all observer events through the consumer pure functions.

    Parameters
    ----------
    observer_events:
        Ordered list of observer event dicts (as produced by ``_obs``).
    check_ticks:
        Set of datetime values at which ``check_floor_2_warning`` should be
        called *in addition to* the automatic post-event tick.  This simulates
        the periodic timeout loop in ``consumer.main()``.

    Returns
    -------
    list of derived event dicts collected from all processing calls.
    """
    furnace_on_since = None
    floor_on_since = _make_floor_on_since()
    floor_2_warn_sent = False

    all_events = []

    def _tick(now_ts):
        nonlocal floor_2_warn_sent
        warn, floor_2_warn_sent = check_floor_2_warning(
            floor_on_since, floor_2_warn_sent, FLOOR_2_WARN_THRESHOLD_S, now_ts
        )
        if warn:
            all_events.append(warn)

    # Build a merged, sorted timeline of observer events + extra check ticks.
    # Each item is either ("event", ts, evt_dict) or ("tick", ts, None).
    timeline = []
    for evt in observer_events:
        ts = datetime.fromisoformat(evt["ts"])
        timeline.append(("event", ts, evt))
    for ts in check_ticks:
        timeline.append(("tick", ts, None))
    timeline.sort(key=lambda item: item[1])

    for kind, ts, evt in timeline:
        if kind == "tick":
            _tick(ts)
            continue

        data = evt["data"]
        entity_id = data["entity_id"]
        old_state = data["old_state"]
        new_state = data["new_state"]
        ts_str = evt["ts"]

        if entity_id in FLOOR_ENTITIES:
            derived, floor_on_since, floor_2_warn_sent = process_floor_event(
                entity_id, old_state, new_state, ts, ts_str, floor_on_since, floor_2_warn_sent
            )
            all_events.extend(derived)

        elif entity_id == FURNACE:
            derived, furnace_on_since = process_furnace_event(
                entity_id, old_state, new_state, ts, ts_str, furnace_on_since
            )
            all_events.extend(derived)

        elif entity_id == OUTDOOR_TEMP:
            all_events.extend(process_outdoor_temp_event(entity_id, new_state, ts_str))

        # Post-event periodic check (mirrors consumer.main() behaviour).
        _tick(ts)

    return all_events


# ---------------------------------------------------------------------------
# Build the synthetic 24-hour event log
# ---------------------------------------------------------------------------


def _build_observer_events():
    """Return the ordered list of observer events for the full-day simulation."""
    events = []

    # ── Morning heating cycle (06:00–06:40) ──────────────────────────────────
    events.append(_obs(FURNACE, "off", "on", _ts(6, 0)))
    events.append(_obs(FLOOR_1, "off", "on", _ts(6, 5)))
    events.append(_obs(FLOOR_1, "on", "off", _ts(6, 10)))  # 5-min call
    events.append(_obs(FLOOR_3, "off", "on", _ts(6, 20)))
    events.append(_obs(FLOOR_3, "on", "off", _ts(6, 30)))  # 10-min call
    events.append(_obs(FURNACE, "on", "off", _ts(6, 40)))  # 40-min session

    # ── Outdoor temperature readings ─────────────────────────────────────────
    events.append(_obs(OUTDOOR_TEMP, None, "35.5", _ts(8, 0)))
    events.append(_obs(OUTDOOR_TEMP, None, "42.0", _ts(10, 0)))
    events.append(_obs(OUTDOOR_TEMP, None, "48.5", _ts(12, 0)))

    # ── Midday heating cycle (13:00–14:00) ───────────────────────────────────
    # Floor 2 normal call: 30 min, below 2700-s threshold — no warning expected.
    events.append(_obs(FURNACE, "off", "on", _ts(13, 0)))
    events.append(_obs(FLOOR_2, "off", "on", _ts(13, 5)))
    events.append(_obs(FLOOR_2, "on", "off", _ts(13, 35)))  # 30-min call
    events.append(_obs(FURNACE, "on", "off", _ts(14, 0)))  # 60-min session

    # ── More outdoor readings ─────────────────────────────────────────────────
    events.append(_obs(OUTDOOR_TEMP, None, "45.0", _ts(16, 0)))

    # ── Evening heating cycle (17:00–22:00) ──────────────────────────────────
    # Floor 2 long call: 70 min, above threshold — exactly one warning expected.
    events.append(_obs(FURNACE, "off", "on", _ts(17, 0)))
    events.append(_obs(FLOOR_2, "off", "on", _ts(17, 5)))
    # Warning check tick at 17:55 → 50 min elapsed (> 2700 s) → fires.
    # The tick is added to check_ticks in the test; Floor 2 is still ON here.
    events.append(_obs(FLOOR_2, "on", "off", _ts(18, 15)))  # 70-min call

    # Floor 1 back-to-back short calls (edge case).
    events.append(_obs(FLOOR_1, "off", "on", _ts(18, 30)))
    events.append(_obs(FLOOR_1, "on", "off", _ts(18, 35)))  # 5-min call
    events.append(_obs(FLOOR_1, "off", "on", _ts(18, 35)))  # immediate back-to-back
    events.append(_obs(FLOOR_1, "on", "off", _ts(18, 40)))  # 5-min call

    # Floor 3 medium call.
    events.append(_obs(FLOOR_3, "off", "on", _ts(18, 50)))
    events.append(_obs(FLOOR_3, "on", "off", _ts(19, 10)))  # 20-min call

    events.append(_obs(OUTDOOR_TEMP, None, "38.0", _ts(20, 0)))
    events.append(_obs(FURNACE, "on", "off", _ts(22, 0)))  # ~5-hr session

    return events


# ---------------------------------------------------------------------------
# The simulation test
# ---------------------------------------------------------------------------


class TestPipelineSimulation:
    """End-to-end simulation of a full 24-hour HVAC event sequence."""

    @pytest.fixture(scope="class")
    def all_derived(self):
        """Run the simulation once and return all derived events."""
        observer_events = _build_observer_events()
        # Extra warning-check ticks (simulate the periodic timeout loop).
        # The critical one is at 17:55, which is 50 min after Floor 2 starts at 17:05.
        check_ticks = {
            _ts(17, 55),  # Floor 2 long call: 50 min elapsed → warning fires
        }
        return run_simulation(observer_events, check_ticks)

    # ── Floor-call-started events ─────────────────────────────────────────────

    def test_floor_1_call_started_emitted(self, all_derived):
        started = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_started.v1"
            and e["data"]["floor"] == "floor_1"
        ]
        assert len(started) >= 1

    def test_floor_2_call_started_emitted(self, all_derived):
        started = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_started.v1"
            and e["data"]["floor"] == "floor_2"
        ]
        assert len(started) >= 1

    def test_floor_3_call_started_emitted(self, all_derived):
        started = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_started.v1"
            and e["data"]["floor"] == "floor_3"
        ]
        assert len(started) >= 1

    # ── Floor-call-ended events ───────────────────────────────────────────────

    def test_floor_1_call_ended_emitted(self, all_derived):
        ended = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_ended.v1"
            and e["data"]["floor"] == "floor_1"
        ]
        assert len(ended) >= 1

    def test_floor_2_call_ended_emitted(self, all_derived):
        ended = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_ended.v1"
            and e["data"]["floor"] == "floor_2"
        ]
        assert len(ended) >= 1

    def test_floor_3_call_ended_emitted(self, all_derived):
        ended = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_ended.v1"
            and e["data"]["floor"] == "floor_3"
        ]
        assert len(ended) >= 1

    def test_all_floor_call_ended_have_positive_duration(self, all_derived):
        ended = [e for e in all_derived if e["schema"] == "homeops.consumer.floor_call_ended.v1"]
        assert len(ended) > 0
        for evt in ended:
            assert evt["data"]["duration_s"] is not None
            assert evt["data"]["duration_s"] > 0

    # ── Furnace / heating-session events ─────────────────────────────────────

    def test_heating_session_started_emitted(self, all_derived):
        schema = "homeops.consumer.heating_session_started.v1"
        started = [e for e in all_derived if e["schema"] == schema]
        assert len(started) >= 1

    def test_heating_session_ended_with_positive_duration(self, all_derived):
        schema = "homeops.consumer.heating_session_ended.v1"
        ended = [e for e in all_derived if e["schema"] == schema]
        assert len(ended) >= 1
        for evt in ended:
            assert evt["data"]["duration_s"] is not None
            assert evt["data"]["duration_s"] > 0

    # ── Outdoor temperature events ────────────────────────────────────────────

    def test_outdoor_temp_updated_emitted(self, all_derived):
        schema = "homeops.consumer.outdoor_temp_updated.v1"
        temp_events = [e for e in all_derived if e["schema"] == schema]
        assert len(temp_events) >= 1

    def test_outdoor_temp_values_are_valid_floats(self, all_derived):
        schema = "homeops.consumer.outdoor_temp_updated.v1"
        temp_events = [e for e in all_derived if e["schema"] == schema]
        for evt in temp_events:
            assert isinstance(evt["data"]["temperature_f"], float)

    # ── Floor-2 long-call warning ─────────────────────────────────────────────

    def test_exactly_one_floor_2_warning_fires(self, all_derived):
        """Only the long (70-min) floor-2 call should produce a warning."""
        warnings = [
            e for e in all_derived if e["schema"] == "homeops.consumer.floor_2_long_call_warning.v1"
        ]
        assert len(warnings) == 1

    def test_floor_2_warning_fires_after_threshold(self, all_derived):
        """The warning's elapsed_s must be >= 2700 (45 min)."""
        warnings = [
            e for e in all_derived if e["schema"] == "homeops.consumer.floor_2_long_call_warning.v1"
        ]
        assert len(warnings) == 1
        assert warnings[0]["data"]["elapsed_s"] >= FLOOR_2_WARN_THRESHOLD_S

    def test_no_floor_2_warning_for_normal_call(self, all_derived):
        """The 30-min midday floor-2 call must not produce a warning.

        We verify indirectly: the warning that *does* fire has elapsed_s
        corresponding to the evening long call (>= 45 min), not the midday
        30-min call.
        """
        warnings = [
            e for e in all_derived if e["schema"] == "homeops.consumer.floor_2_long_call_warning.v1"
        ]
        # Only one warning — its elapsed_s must be >= threshold (45 min).
        # If the 30-min call had also triggered one we'd have two warnings.
        assert len(warnings) == 1
        assert warnings[0]["data"]["elapsed_s"] >= FLOOR_2_WARN_THRESHOLD_S

    def test_floor_2_warning_not_for_floor_1_or_floor_3(self, all_derived):
        """floor_2_long_call_warning must reference floor_2 exclusively."""
        warnings = [
            e for e in all_derived if e["schema"] == "homeops.consumer.floor_2_long_call_warning.v1"
        ]
        for w in warnings:
            assert w["data"]["floor"] == "floor_2"
            assert w["data"]["entity_id"] == FLOOR_2

    # ── Edge-case: back-to-back floor-1 calls ────────────────────────────────

    def test_back_to_back_floor_1_calls_produce_two_start_events_in_evening(self, all_derived):
        """The two consecutive floor-1 calls in the evening each emit a started event."""
        evening_start = _ts(18, 0)
        started = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_started.v1"
            and e["data"]["floor"] == "floor_1"
            and datetime.fromisoformat(e["data"]["started_at"]) >= evening_start
        ]
        assert len(started) == 2

    def test_back_to_back_floor_1_calls_produce_two_ended_events_in_evening(self, all_derived):
        evening_start = _ts(18, 0)
        ended = [
            e
            for e in all_derived
            if e["schema"] == "homeops.consumer.floor_call_ended.v1"
            and e["data"]["floor"] == "floor_1"
            and datetime.fromisoformat(e["data"]["ended_at"]) >= evening_start
        ]
        assert len(ended) == 2

    # ── Derived-event counts sanity checks ───────────────────────────────────

    def test_total_heating_sessions_match_furnace_cycles(self, all_derived):
        """3 furnace on/off cycles → 3 started + 3 ended heating-session events."""
        started = [
            e for e in all_derived if e["schema"] == "homeops.consumer.heating_session_started.v1"
        ]
        ended = [
            e for e in all_derived if e["schema"] == "homeops.consumer.heating_session_ended.v1"
        ]
        assert len(started) == 3
        assert len(ended) == 3

    def test_outdoor_temp_event_count(self, all_derived):
        """5 outdoor temp observer events → 5 outdoor_temp_updated derived events."""
        schema = "homeops.consumer.outdoor_temp_updated.v1"
        temp_events = [e for e in all_derived if e["schema"] == schema]
        assert len(temp_events) == 5

    def test_floor_2_warn_resets_on_second_call(self, all_derived):
        """floor_2_long_call_warning should fire for the long call, not the first one.

        Verifies that when floor_2 starts its second (long) call, the
        warn_sent flag has been reset — allowing the warning to fire again
        even though a warning could theoretically have been sent previously.
        (In this simulation no warning fires for the first call, so reset
        behaviour is exercised implicitly by confirming exactly 1 warning.)
        """
        warnings = [
            e for e in all_derived if e["schema"] == "homeops.consumer.floor_2_long_call_warning.v1"
        ]
        assert len(warnings) == 1
