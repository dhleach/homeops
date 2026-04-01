"""Integration tests for floor-2 long-call detection and alerting.

These tests feed realistic sequences of observer events through the consumer
pipeline (process_floor_event + check_floor_2_warning + check_floor_2_escalation)
and verify the correct derived events are emitted at the right points.

This mirrors the live loop in consumer.main():
  1. process_floor_event() on each floor sensor state change
  2. check_floor_2_warning() called after each event (and on timeout ticks)
  3. check_floor_2_escalation() called when a warning fires, with the updated
     daily counter

Acceptance criteria covered:
  [AC1] Warning fires when floor-2 call exceeds threshold
  [AC2] Warning is NOT emitted if call ends before threshold
  [AC3] warn_sent flag prevents double-firing on same call
  [AC4] Escalation fires on the 2nd long call in the same day
  [AC5] (positive control) short call produces no warning
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from consumer import (
    _empty_daily_state,
    check_floor_2_escalation,
    check_floor_2_warning,
    process_floor_event,
)

# ---------------------------------------------------------------------------
# Constants & entity IDs
# ---------------------------------------------------------------------------

FLOOR_1 = "binary_sensor.floor_1_heating_call"
FLOOR_2 = "binary_sensor.floor_2_heating_call"
FLOOR_3 = "binary_sensor.floor_3_heating_call"

FLOOR_ENTITIES = {FLOOR_1, FLOOR_2, FLOOR_3}

THRESHOLD_S = 2700  # 45 minutes — mirrors FLOOR_2_WARN_THRESHOLD_S

WARN_SCHEMA = "homeops.consumer.floor_2_long_call_warning.v1"
ESCALATION_SCHEMA = "homeops.consumer.floor_2_long_call_escalation.v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Return a UTC datetime anchored to 2024-01-15."""
    return datetime(2024, 1, 15, hour, minute, second, tzinfo=UTC)


def _make_floor_on_since() -> dict:
    return {FLOOR_1: None, FLOOR_2: None, FLOOR_3: None}


def _obs(entity_id: str, old_state: str | None, new_state: str | None, ts: datetime) -> dict:
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


class PipelineRunner:
    """Drives the consumer pipeline for a sequence of floor-2-relevant events.

    Mirrors the logic in consumer.main():
      - process_floor_event() on every floor sensor change
      - check_floor_2_warning() ticked after each event
      - check_floor_2_escalation() called when a warning fires

    State is fully encapsulated so each test gets a fresh runner.
    """

    def __init__(self, threshold_s: int = THRESHOLD_S) -> None:
        self.threshold_s = threshold_s
        self.floor_on_since = _make_floor_on_since()
        self.floor_2_warn_sent = False
        self.daily_state = _empty_daily_state()
        self.derived_events: list[dict] = []

    def _tick_warning(self, now_ts: datetime) -> None:
        """Run a single check_floor_2_warning tick and collect any events."""
        warn, self.floor_2_warn_sent = check_floor_2_warning(
            self.floor_on_since,
            self.floor_2_warn_sent,
            self.threshold_s,
            now_ts,
        )
        if warn:
            self.derived_events.append(warn)
            self.daily_state["warnings_triggered"]["floor_2_long_call"] += 1
            # Also run escalation check immediately, as consumer.main() does.
            count = self.daily_state["warnings_triggered"]["floor_2_long_call"]
            esc = check_floor_2_escalation(count, self.threshold_s)
            if esc:
                self.derived_events.append(esc)
                self.daily_state["warnings_triggered"]["floor_2_escalation"] += 1

    def send(self, obs_event: dict) -> None:
        """Process one observer event through the pipeline, then tick."""
        data = obs_event["data"]
        entity_id = data["entity_id"]
        old_state = data["old_state"]
        new_state = data["new_state"]
        ts = datetime.fromisoformat(obs_event["ts"])
        ts_str = obs_event["ts"]

        if entity_id in FLOOR_ENTITIES:
            derived, self.floor_on_since, self.floor_2_warn_sent = process_floor_event(
                entity_id,
                old_state,
                new_state,
                ts,
                ts_str,
                self.floor_on_since,
                self.floor_2_warn_sent,
            )
            self.derived_events.extend(derived)

        # Post-event tick — mirrors the in-flight check in consumer.main()
        self._tick_warning(ts)

    def tick_at(self, ts: datetime) -> None:
        """Simulate a periodic timeout check at the given timestamp."""
        self._tick_warning(ts)

    def warnings(self) -> list[dict]:
        return [e for e in self.derived_events if e["schema"] == WARN_SCHEMA]

    def escalations(self) -> list[dict]:
        return [e for e in self.derived_events if e["schema"] == ESCALATION_SCHEMA]


# ---------------------------------------------------------------------------
# AC1 — Warning fires when floor-2 call exceeds threshold
# ---------------------------------------------------------------------------


class TestWarningFiresAtThreshold:
    """Floor-2 long-call warning fires when the call duration exceeds THRESHOLD_S."""

    def test_warning_fires_after_threshold_exceeded(self):
        """[AC1] Warning emitted when a periodic tick fires at T+threshold+60s."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)

        runner.send(_obs(FLOOR_2, "off", "on", start_ts))

        # Tick at exactly threshold — should not fire yet (< required).
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S - 1))
        assert len(runner.warnings()) == 0

        # Tick at threshold+60s — should fire.
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S + 60))
        assert len(runner.warnings()) == 1

    def test_warning_schema_is_correct(self):
        """[AC1] Emitted warning uses the correct schema string."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start_ts))
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S + 1))

        assert runner.warnings()[0]["schema"] == WARN_SCHEMA

    def test_warning_data_fields_correct(self):
        """[AC1] Warning payload has correct floor, entity_id, threshold_s, elapsed_s."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start_ts))
        tick_ts = start_ts + timedelta(seconds=THRESHOLD_S + 120)
        runner.tick_at(tick_ts)

        warn = runner.warnings()[0]
        d = warn["data"]
        assert d["floor"] == "floor_2"
        assert d["entity_id"] == FLOOR_2
        assert d["threshold_s"] == THRESHOLD_S
        assert d["elapsed_s"] >= THRESHOLD_S

    def test_warning_elapsed_s_matches_actual_duration(self):
        """[AC1] elapsed_s in the warning reflects the actual call duration."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start_ts))
        extra_s = 300  # 5 min beyond threshold
        tick_ts = start_ts + timedelta(seconds=THRESHOLD_S + extra_s)
        runner.tick_at(tick_ts)

        elapsed = runner.warnings()[0]["data"]["elapsed_s"]
        assert elapsed >= THRESHOLD_S + extra_s - 1  # allow 1s timing tolerance
        assert elapsed <= THRESHOLD_S + extra_s + 1

    def test_warning_only_fires_for_floor_2(self):
        """[AC1] Long calls on floor_1 and floor_3 do NOT trigger a floor_2 warning."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        # Floor 1 long call
        runner.send(_obs(FLOOR_1, "off", "on", start_ts))
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S + 60))
        runner.send(_obs(FLOOR_1, "on", "off", start_ts + timedelta(seconds=THRESHOLD_S + 120)))

        # Floor 3 long call
        runner.send(_obs(FLOOR_3, "off", "on", _dt(12, 0)))
        runner.tick_at(_dt(12, 0) + timedelta(seconds=THRESHOLD_S + 60))
        runner.send(_obs(FLOOR_3, "on", "off", _dt(12, 0) + timedelta(seconds=THRESHOLD_S + 120)))

        assert len(runner.warnings()) == 0


# ---------------------------------------------------------------------------
# AC2 — Warning NOT emitted if call ends before threshold
# ---------------------------------------------------------------------------


class TestWarningNotFiredForShortCall:
    """No warning when the floor-2 call ends before the threshold."""

    def test_short_call_no_warning(self):
        """[AC2] A 30-min call (below 45-min threshold) produces no warning."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        end_ts = start_ts + timedelta(minutes=30)  # 1800s < 2700s

        runner.send(_obs(FLOOR_2, "off", "on", start_ts))
        # Tick mid-call, still below threshold
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S - 60))
        runner.send(_obs(FLOOR_2, "on", "off", end_ts))
        # Post-end tick — floor_2 is now off, no warning possible
        runner.tick_at(end_ts + timedelta(seconds=60))

        assert len(runner.warnings()) == 0

    def test_call_ending_just_before_threshold_no_warning(self):
        """[AC2] Call ending at threshold-1s produces no warning (boundary case)."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        end_ts = start_ts + timedelta(seconds=THRESHOLD_S - 1)

        runner.send(_obs(FLOOR_2, "off", "on", start_ts))
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S - 2))
        runner.send(_obs(FLOOR_2, "on", "off", end_ts))
        runner.tick_at(end_ts + timedelta(seconds=10))

        assert len(runner.warnings()) == 0

    def test_very_short_call_no_warning(self):
        """[AC2] A 5-minute call produces no warning."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start_ts))
        runner.send(_obs(FLOOR_2, "on", "off", start_ts + timedelta(minutes=5)))
        runner.tick_at(start_ts + timedelta(minutes=6))

        assert len(runner.warnings()) == 0

    def test_multiple_short_calls_no_warning(self):
        """[AC2] Multiple short calls in succession still produce no warning."""
        runner = PipelineRunner()

        for i in range(3):
            t = _dt(8 + i * 2, 0)
            runner.send(_obs(FLOOR_2, "off", "on", t))
            runner.send(_obs(FLOOR_2, "on", "off", t + timedelta(minutes=20)))
            runner.tick_at(t + timedelta(minutes=25))

        assert len(runner.warnings()) == 0


# ---------------------------------------------------------------------------
# AC3 — warn_sent flag prevents double-firing on the same call
# ---------------------------------------------------------------------------


class TestWarnSentFlagPreventsDoubleFire:
    """Once warn_sent is True, no second warning fires for the same call."""

    def test_second_tick_after_warning_fires_no_duplicate(self):
        """[AC3] A second tick during the same long call does not emit a second warning."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start_ts))

        # First tick at threshold+60s — fires warning
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S + 60))
        assert len(runner.warnings()) == 1

        # Second tick 5 min later — call still ongoing, warn_sent already True
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S + 360))
        assert len(runner.warnings()) == 1  # no second warning

    def test_many_ticks_produce_exactly_one_warning(self):
        """[AC3] Ten periodic ticks after the threshold produce exactly one warning."""
        runner = PipelineRunner()
        start_ts = _dt(10, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start_ts))

        for i in range(1, 11):
            runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S + i * 60))

        assert len(runner.warnings()) == 1

    def test_warn_sent_resets_on_new_call(self):
        """[AC3] warn_sent resets when floor_2 starts a new call; warning fires again."""
        runner = PipelineRunner()

        # First long call
        start1 = _dt(8, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start1))
        runner.tick_at(start1 + timedelta(seconds=THRESHOLD_S + 60))
        runner.send(_obs(FLOOR_2, "on", "off", start1 + timedelta(seconds=THRESHOLD_S + 120)))

        assert len(runner.warnings()) == 1

        # Second long call — warn_sent should have been reset on floor_2 start
        start2 = _dt(14, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start2))
        runner.tick_at(start2 + timedelta(seconds=THRESHOLD_S + 60))

        # Warning must fire again for the second call
        assert len(runner.warnings()) == 2


# ---------------------------------------------------------------------------
# AC4 — Escalation fires on the 2nd long call in the same day
# ---------------------------------------------------------------------------


class TestEscalationOnSecondLongCall:
    """Escalation event fires on the 2nd (and subsequent) long call in a day."""

    def test_no_escalation_on_first_long_call(self):
        """[AC4] First long call → warning only, no escalation."""
        runner = PipelineRunner()
        start_ts = _dt(8, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start_ts))
        runner.tick_at(start_ts + timedelta(seconds=THRESHOLD_S + 60))

        assert len(runner.warnings()) == 1
        assert len(runner.escalations()) == 0

    def test_escalation_fires_on_second_long_call(self):
        """[AC4] Second long call → both warning and escalation fire."""
        runner = PipelineRunner()

        # First long call (no escalation)
        start1 = _dt(8, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start1))
        runner.tick_at(start1 + timedelta(seconds=THRESHOLD_S + 60))
        runner.send(_obs(FLOOR_2, "on", "off", start1 + timedelta(seconds=THRESHOLD_S + 120)))

        # Second long call (escalation should fire)
        start2 = _dt(12, 0)
        runner.send(_obs(FLOOR_2, "off", "on", start2))
        runner.tick_at(start2 + timedelta(seconds=THRESHOLD_S + 60))

        assert len(runner.warnings()) == 2
        assert len(runner.escalations()) == 1

    def test_escalation_schema_correct(self):
        """[AC4] Escalation event uses the correct schema string."""
        runner = PipelineRunner()
        for i in range(2):
            start = _dt(8 + i * 4, 0)
            runner.send(_obs(FLOOR_2, "off", "on", start))
            runner.tick_at(start + timedelta(seconds=THRESHOLD_S + 60))
            runner.send(_obs(FLOOR_2, "on", "off", start + timedelta(seconds=THRESHOLD_S + 120)))

        assert runner.escalations()[0]["schema"] == ESCALATION_SCHEMA

    def test_escalation_data_fields(self):
        """[AC4] Escalation payload has correct floor, long_call_count_today, threshold_s."""
        runner = PipelineRunner()
        for i in range(2):
            start = _dt(8 + i * 4, 0)
            runner.send(_obs(FLOOR_2, "off", "on", start))
            runner.tick_at(start + timedelta(seconds=THRESHOLD_S + 60))
            runner.send(_obs(FLOOR_2, "on", "off", start + timedelta(seconds=THRESHOLD_S + 120)))

        esc = runner.escalations()[0]
        d = esc["data"]
        assert d["floor"] == "floor_2"
        assert d["long_call_count_today"] == 2
        assert d["threshold_s"] == THRESHOLD_S

    def test_escalation_fires_on_each_subsequent_long_call(self):
        """[AC4] Escalation fires on 2nd, 3rd, 4th long calls."""
        runner = PipelineRunner()

        for i in range(4):
            start = _dt(6 + i * 4, 0)
            runner.send(_obs(FLOOR_2, "off", "on", start))
            runner.tick_at(start + timedelta(seconds=THRESHOLD_S + 60))
            runner.send(_obs(FLOOR_2, "on", "off", start + timedelta(seconds=THRESHOLD_S + 120)))

        # 4 warnings, 3 escalations (on calls 2, 3, 4)
        assert len(runner.warnings()) == 4
        assert len(runner.escalations()) == 3

    def test_escalation_count_increments_correctly(self):
        """[AC4] long_call_count_today in escalation increments per call."""
        runner = PipelineRunner()

        for i in range(4):
            start = _dt(6 + i * 4, 0)
            runner.send(_obs(FLOOR_2, "off", "on", start))
            runner.tick_at(start + timedelta(seconds=THRESHOLD_S + 60))
            runner.send(_obs(FLOOR_2, "on", "off", start + timedelta(seconds=THRESHOLD_S + 120)))

        counts = [e["data"]["long_call_count_today"] for e in runner.escalations()]
        assert counts == [2, 3, 4]

    def test_daily_state_counters_accurate(self):
        """[AC4] daily_state tracks warning and escalation counts correctly."""
        runner = PipelineRunner()

        for i in range(3):
            start = _dt(6 + i * 4, 0)
            runner.send(_obs(FLOOR_2, "off", "on", start))
            runner.tick_at(start + timedelta(seconds=THRESHOLD_S + 60))
            runner.send(_obs(FLOOR_2, "on", "off", start + timedelta(seconds=THRESHOLD_S + 120)))

        assert runner.daily_state["warnings_triggered"]["floor_2_long_call"] == 3
        assert runner.daily_state["warnings_triggered"]["floor_2_escalation"] == 2


# ---------------------------------------------------------------------------
# AC5 — Full sequence: normal call + long call + escalation
# ---------------------------------------------------------------------------


class TestFullDaySequence:
    """End-to-end scenario: normal call (no alert) then two long calls (warning + escalation)."""

    @pytest.fixture(scope="class")
    def runner(self):
        r = PipelineRunner()

        # ── Morning: normal 30-min call — no warning ─────────────────────────
        morning_start = _dt(8, 0)
        r.send(_obs(FLOOR_2, "off", "on", morning_start))
        r.tick_at(morning_start + timedelta(minutes=15))  # mid-call tick, below threshold
        r.send(_obs(FLOOR_2, "on", "off", morning_start + timedelta(minutes=30)))
        r.tick_at(morning_start + timedelta(minutes=35))

        # ── Afternoon: first long call (50 min) — warning, no escalation ─────
        afternoon_start = _dt(13, 0)
        r.send(_obs(FLOOR_2, "off", "on", afternoon_start))
        r.tick_at(afternoon_start + timedelta(seconds=THRESHOLD_S + 5 * 60))  # 50 min in
        r.send(_obs(FLOOR_2, "on", "off", afternoon_start + timedelta(minutes=50)))

        # ── Evening: second long call (60 min) — warning + escalation ────────
        evening_start = _dt(18, 0)
        r.send(_obs(FLOOR_2, "off", "on", evening_start))
        r.tick_at(evening_start + timedelta(seconds=THRESHOLD_S + 5 * 60))  # 50 min in
        r.send(_obs(FLOOR_2, "on", "off", evening_start + timedelta(minutes=60)))

        return r

    def test_no_warning_for_short_morning_call(self, runner):
        """[AC2] 30-min morning call produces no warning."""
        # All warnings that fired should be for calls >= threshold duration
        for w in runner.warnings():
            assert w["data"]["elapsed_s"] >= THRESHOLD_S

    def test_exactly_two_warnings_total(self, runner):
        """[AC1] Two long calls → two warnings total."""
        assert len(runner.warnings()) == 2

    def test_exactly_one_escalation_total(self, runner):
        """[AC4] Second long call → one escalation total."""
        assert len(runner.escalations()) == 1

    def test_escalation_references_second_call(self, runner):
        """[AC4] Escalation long_call_count_today == 2."""
        assert runner.escalations()[0]["data"]["long_call_count_today"] == 2

    def test_daily_counters_correct(self, runner):
        """[AC4] daily_state has 2 long-call warnings and 1 escalation."""
        assert runner.daily_state["warnings_triggered"]["floor_2_long_call"] == 2
        assert runner.daily_state["warnings_triggered"]["floor_2_escalation"] == 1

    def test_warn_sent_reset_at_call_start(self):
        """[AC3] warn_sent is reset to False at the START of each new floor-2 call.

        process_floor_event resets warn_sent on off->on (call start), not on
        call end. This test verifies the reset happens when the next call begins.
        """
        r = PipelineRunner()

        # First long call fires warning → warn_sent=True
        start1 = _dt(8, 0)
        r.send(_obs(FLOOR_2, "off", "on", start1))
        r.tick_at(start1 + timedelta(seconds=THRESHOLD_S + 60))
        assert r.floor_2_warn_sent is True

        r.send(_obs(FLOOR_2, "on", "off", start1 + timedelta(seconds=THRESHOLD_S + 120)))
        # warn_sent still True after call ends (reset happens on next start, not end)
        assert r.floor_2_warn_sent is True

        # Starting a new call resets warn_sent
        start2 = _dt(14, 0)
        r.send(_obs(FLOOR_2, "off", "on", start2))
        assert r.floor_2_warn_sent is False

    def test_floor_call_started_events_emitted(self, runner):
        """Pipeline emits floor_call_started.v1 for each floor-2 call start."""
        started = [
            e
            for e in runner.derived_events
            if e["schema"] == "homeops.consumer.floor_call_started.v1"
            and e["data"]["floor"] == "floor_2"
        ]
        assert len(started) == 3  # morning + afternoon + evening

    def test_floor_call_ended_events_emitted(self, runner):
        """Pipeline emits floor_call_ended.v1 for each floor-2 call end."""
        ended = [
            e
            for e in runner.derived_events
            if e["schema"] == "homeops.consumer.floor_call_ended.v1"
            and e["data"]["floor"] == "floor_2"
        ]
        assert len(ended) == 3
