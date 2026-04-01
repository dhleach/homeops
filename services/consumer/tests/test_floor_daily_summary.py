"""Unit tests for emit_floor_daily_summaries() and per_floor_max_call_s tracking."""

from consumer import emit_floor_daily_summaries
from state import _empty_daily_state

FLOOR_1 = "binary_sensor.floor_1_heating_call"
FLOOR_2 = "binary_sensor.floor_2_heating_call"
FLOOR_3 = "binary_sensor.floor_3_heating_call"

DATE = "2026-01-15"


def make_daily_state(**overrides):
    state = _empty_daily_state()
    state.update(overrides)
    return state


class TestEmitFloorDailySummaries:
    def test_returns_three_events(self):
        state = make_daily_state()
        events = emit_floor_daily_summaries(state, DATE)
        assert len(events) == 3

    def test_event_schema_and_source(self):
        state = make_daily_state()
        events = emit_floor_daily_summaries(state, DATE)
        for evt in events:
            assert evt["schema"] == "homeops.consumer.floor_daily_summary.v1"
            assert evt["source"] == "consumer.v1"
            assert "ts" in evt

    def test_one_event_per_floor(self):
        state = make_daily_state()
        events = emit_floor_daily_summaries(state, DATE)
        floors = {evt["data"]["floor"] for evt in events}
        assert floors == {"floor_1", "floor_2", "floor_3"}

    def test_date_field(self):
        state = make_daily_state()
        events = emit_floor_daily_summaries(state, DATE)
        for evt in events:
            assert evt["data"]["date"] == DATE

    def test_zero_calls(self):
        state = make_daily_state()
        events = emit_floor_daily_summaries(state, DATE)
        for evt in events:
            d = evt["data"]
            assert d["total_calls"] == 0
            assert d["total_runtime_s"] == 0
            assert d["avg_duration_s"] is None
            assert d["max_duration_s"] is None

    def test_basic_floor_stats(self):
        state = make_daily_state(
            per_floor_session_count={FLOOR_1: 3, FLOOR_2: 1, FLOOR_3: 0},
            floor_runtime_s={FLOOR_1: 3600, FLOOR_2: 2700, FLOOR_3: 0},
            per_floor_max_call_s={FLOOR_1: 1500, FLOOR_2: 2700, FLOOR_3: None},
        )
        events = emit_floor_daily_summaries(state, DATE)
        by_floor = {evt["data"]["floor"]: evt["data"] for evt in events}

        f1 = by_floor["floor_1"]
        assert f1["total_calls"] == 3
        assert f1["total_runtime_s"] == 3600
        assert f1["avg_duration_s"] == 1200.0
        assert f1["max_duration_s"] == 1500

        f2 = by_floor["floor_2"]
        assert f2["total_calls"] == 1
        assert f2["total_runtime_s"] == 2700
        assert f2["avg_duration_s"] == 2700.0
        assert f2["max_duration_s"] == 2700

        f3 = by_floor["floor_3"]
        assert f3["total_calls"] == 0
        assert f3["total_runtime_s"] == 0
        assert f3["avg_duration_s"] is None
        assert f3["max_duration_s"] is None

    def test_outdoor_temp_avg(self):
        state = make_daily_state(outdoor_temps=[25.0, 30.0, 35.0])
        events = emit_floor_daily_summaries(state, DATE)
        for evt in events:
            assert evt["data"]["outdoor_temp_avg_f"] == 30.0

    def test_outdoor_temp_avg_none_when_no_readings(self):
        state = make_daily_state()
        events = emit_floor_daily_summaries(state, DATE)
        for evt in events:
            assert evt["data"]["outdoor_temp_avg_f"] is None

    def test_outdoor_temp_avg_rounded(self):
        state = make_daily_state(outdoor_temps=[28.1, 28.2, 28.3])
        events = emit_floor_daily_summaries(state, DATE)
        avg = events[0]["data"]["outdoor_temp_avg_f"]
        assert avg == round((28.1 + 28.2 + 28.3) / 3, 1)


class TestPerFloorMaxCallTracking:
    """Tests for the per_floor_max_call_s accumulation logic in _empty_daily_state."""

    def test_empty_daily_state_has_max_call_keys(self):
        state = _empty_daily_state()
        assert "per_floor_max_call_s" in state
        assert FLOOR_1 in state["per_floor_max_call_s"]
        assert FLOOR_2 in state["per_floor_max_call_s"]
        assert FLOOR_3 in state["per_floor_max_call_s"]

    def test_empty_daily_state_max_call_starts_none(self):
        state = _empty_daily_state()
        for eid in [FLOOR_1, FLOOR_2, FLOOR_3]:
            assert state["per_floor_max_call_s"][eid] is None

    def test_max_call_first_call(self):
        """First call with duration_s should set max."""
        state = _empty_daily_state()
        duration = 1800
        prev = state["per_floor_max_call_s"].get(FLOOR_2)
        if prev is None or duration > prev:
            state["per_floor_max_call_s"][FLOOR_2] = duration
        assert state["per_floor_max_call_s"][FLOOR_2] == 1800

    def test_max_call_longer_call_replaces(self):
        """A longer subsequent call should replace the max."""
        state = _empty_daily_state()
        state["per_floor_max_call_s"][FLOOR_2] = 1800
        new_duration = 2700
        prev = state["per_floor_max_call_s"].get(FLOOR_2)
        if prev is None or new_duration > prev:
            state["per_floor_max_call_s"][FLOOR_2] = new_duration
        assert state["per_floor_max_call_s"][FLOOR_2] == 2700

    def test_max_call_shorter_call_does_not_replace(self):
        """A shorter subsequent call should NOT replace the max."""
        state = _empty_daily_state()
        state["per_floor_max_call_s"][FLOOR_2] = 2700
        new_duration = 900
        prev = state["per_floor_max_call_s"].get(FLOOR_2)
        if prev is None or new_duration > prev:
            state["per_floor_max_call_s"][FLOOR_2] = new_duration
        assert state["per_floor_max_call_s"][FLOOR_2] == 2700

    def test_max_call_equal_duration_does_not_change(self):
        """A call with the same duration as max should leave max unchanged."""
        state = _empty_daily_state()
        state["per_floor_max_call_s"][FLOOR_1] = 1200
        new_duration = 1200
        prev = state["per_floor_max_call_s"].get(FLOOR_1)
        if prev is None or new_duration > prev:
            state["per_floor_max_call_s"][FLOOR_1] = new_duration
        assert state["per_floor_max_call_s"][FLOOR_1] == 1200
