"""Tests for floor-2 repeated long-call escalation logic."""

from datetime import UTC, datetime, timedelta

from consumer import (
    _empty_daily_state,
    check_floor_2_escalation,
    check_floor_2_warning,
)

FLOOR_2 = "binary_sensor.floor_2_heating_call"
THRESHOLD_S = 2700  # 45 min default


def _make_floor_on_since(started_at: datetime) -> dict:
    return {
        "binary_sensor.floor_1_heating_call": None,
        FLOOR_2: started_at,
        "binary_sensor.floor_3_heating_call": None,
    }


def _fire_warning(floor_on_since, threshold_s=THRESHOLD_S, climate_state=None):
    """Helper: fire a long-call warning (floor_2_warn_sent=False) and return (event, sent_flag)."""
    now_ts = list(floor_on_since.values())[1] + timedelta(seconds=threshold_s + 1)
    return check_floor_2_warning(
        floor_on_since,
        floor_2_warn_sent=False,
        floor_2_warn_threshold_s=threshold_s,
        now_ts=now_ts,
        climate_state=climate_state,
    )


# ---------------------------------------------------------------------------
# check_floor_2_escalation — pure function tests
# ---------------------------------------------------------------------------


class TestCheckFloor2Escalation:
    def test_no_escalation_on_first_warning(self):
        """count=1 → no escalation."""
        result = check_floor_2_escalation(1, THRESHOLD_S)
        assert result is None

    def test_no_escalation_on_zero(self):
        """count=0 → no escalation."""
        result = check_floor_2_escalation(0, THRESHOLD_S)
        assert result is None

    def test_escalation_fires_on_second_warning(self):
        """count=2 → escalation fires."""
        result = check_floor_2_escalation(2, THRESHOLD_S)
        assert result is not None
        assert result["schema"] == "homeops.consumer.floor_2_long_call_escalation.v1"
        assert result["source"] == "consumer.v1"
        assert result["data"]["floor"] == "floor_2"
        assert result["data"]["long_call_count_today"] == 2
        assert result["data"]["threshold_s"] == THRESHOLD_S

    def test_escalation_fires_on_third_warning(self):
        """count=3 → escalation fires again."""
        result = check_floor_2_escalation(3, THRESHOLD_S)
        assert result is not None
        assert result["data"]["long_call_count_today"] == 3

    def test_escalation_fires_on_subsequent_warnings(self):
        """Escalation fires for count >= 2 (every additional long-call)."""
        for count in range(2, 6):
            result = check_floor_2_escalation(count, THRESHOLD_S)
            assert result is not None, f"Expected escalation at count={count}"
            assert result["data"]["long_call_count_today"] == count

    def test_escalation_event_schema_fields(self):
        """Escalation event has all required fields."""
        climate_state = {
            "climate.floor_2_thermostat": {
                "current_temp": 68.0,
                "setpoint": 72.0,
            }
        }
        result = check_floor_2_escalation(2, THRESHOLD_S, climate_state=climate_state)
        assert result is not None
        data = result["data"]
        assert data["floor"] == "floor_2"
        assert data["long_call_count_today"] == 2
        assert data["threshold_s"] == THRESHOLD_S
        assert data["current_temp"] == 68.0
        assert data["setpoint"] == 72.0
        assert "ts" in result

    def test_escalation_null_temps_when_no_climate_state(self):
        """Escalation event has null temps when climate_state is None."""
        result = check_floor_2_escalation(2, THRESHOLD_S, climate_state=None)
        assert result is not None
        assert result["data"]["current_temp"] is None
        assert result["data"]["setpoint"] is None

    def test_escalation_null_temps_when_thermostat_missing(self):
        """Escalation event has null temps when floor_2 thermostat not in climate_state."""
        result = check_floor_2_escalation(2, THRESHOLD_S, climate_state={})
        assert result is not None
        assert result["data"]["current_temp"] is None
        assert result["data"]["setpoint"] is None


# ---------------------------------------------------------------------------
# Integration: simulating multiple call sessions in one day
# ---------------------------------------------------------------------------


class TestFloor2EscalationIntegration:
    """
    Simulate multiple call sessions using check_floor_2_warning + check_floor_2_escalation
    with a daily_state dict, the same way consumer.run() does it.
    """

    def _simulate_session(self, daily_state, session_num):
        """
        Simulate one floor-2 long-call session:
        1. Reset warn_sent (as happens when floor_2 starts a new call).
        2. Fire the long-call warning.
        3. Increment the daily counter.
        4. Check for escalation.
        Returns (warn_event, escalation_event).
        """
        started = datetime(2024, 1, 15, 8 + session_num, 0, 0, tzinfo=UTC)
        floor_on_since = _make_floor_on_since(started)

        warn_event, _ = _fire_warning(floor_on_since)
        assert warn_event is not None, f"Session {session_num}: expected warning to fire"

        daily_state["warnings_triggered"]["floor_2_long_call"] += 1
        count = daily_state["warnings_triggered"]["floor_2_long_call"]
        escalation_event = check_floor_2_escalation(count, THRESHOLD_S)
        if escalation_event:
            daily_state["warnings_triggered"]["floor_2_escalation"] += 1

        return warn_event, escalation_event

    def test_first_session_no_escalation(self):
        daily_state = _empty_daily_state()
        _, esc = self._simulate_session(daily_state, session_num=0)
        assert esc is None
        assert daily_state["warnings_triggered"]["floor_2_long_call"] == 1
        assert daily_state["warnings_triggered"]["floor_2_escalation"] == 0

    def test_second_session_escalation_fires(self):
        daily_state = _empty_daily_state()
        self._simulate_session(daily_state, session_num=0)  # first — no escalation
        _, esc = self._simulate_session(daily_state, session_num=1)  # second — escalates
        assert esc is not None
        assert esc["schema"] == "homeops.consumer.floor_2_long_call_escalation.v1"
        assert esc["data"]["long_call_count_today"] == 2
        assert daily_state["warnings_triggered"]["floor_2_long_call"] == 2
        assert daily_state["warnings_triggered"]["floor_2_escalation"] == 1

    def test_third_session_escalation_fires_again(self):
        daily_state = _empty_daily_state()
        self._simulate_session(daily_state, session_num=0)
        self._simulate_session(daily_state, session_num=1)
        _, esc = self._simulate_session(daily_state, session_num=2)
        assert esc is not None
        assert esc["data"]["long_call_count_today"] == 3
        assert daily_state["warnings_triggered"]["floor_2_long_call"] == 3
        assert daily_state["warnings_triggered"]["floor_2_escalation"] == 2

    def test_daily_state_counter_increments_correctly(self):
        """Run 5 sessions, verify counters are consistent."""
        daily_state = _empty_daily_state()
        for i in range(5):
            self._simulate_session(daily_state, session_num=i)

        assert daily_state["warnings_triggered"]["floor_2_long_call"] == 5
        # escalation fires on sessions 2,3,4,5 → count=4
        assert daily_state["warnings_triggered"]["floor_2_escalation"] == 4

    def test_empty_daily_state_has_escalation_key(self):
        """_empty_daily_state must include floor_2_escalation key."""
        ds = _empty_daily_state()
        assert "floor_2_escalation" in ds["warnings_triggered"]
        assert ds["warnings_triggered"]["floor_2_escalation"] == 0
