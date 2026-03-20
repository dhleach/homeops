"""Unit tests for emit_daily_summary()."""

from consumer import emit_daily_summary

FLOOR_1 = "binary_sensor.floor_1_heating_call"
FLOOR_2 = "binary_sensor.floor_2_heating_call"
FLOOR_3 = "binary_sensor.floor_3_heating_call"


def make_daily_state(**overrides):
    base = {
        "furnace_runtime_s": 0,
        "session_count": 0,
        "floor_runtime_s": {},
        "outdoor_temps": [],
    }
    base.update(overrides)
    return base


class TestEmitDailySummary:
    def test_basic_case(self):
        state = make_daily_state(
            furnace_runtime_s=3600,
            session_count=5,
            floor_runtime_s={FLOOR_1: 1200, FLOOR_2: 1800, FLOOR_3: 600},
            outdoor_temps=[28.5, 35.0, 41.2],
        )
        evt = emit_daily_summary(state, "2024-01-15")
        assert evt["schema"] == "homeops.consumer.furnace_daily_summary.v1"
        assert evt["source"] == "consumer.v1"
        assert "ts" in evt
        data = evt["data"]
        assert data["date"] == "2024-01-15"
        assert data["total_furnace_runtime_s"] == 3600
        assert data["session_count"] == 5
        assert data["per_floor_runtime_s"] == {"floor_1": 1200, "floor_2": 1800, "floor_3": 600}
        assert data["outdoor_temp_min_f"] == 28.5
        assert data["outdoor_temp_max_f"] == 41.2

    def test_no_outdoor_temps(self):
        state = make_daily_state(furnace_runtime_s=1800, session_count=3)
        evt = emit_daily_summary(state, "2024-01-16")
        data = evt["data"]
        assert data["outdoor_temp_min_f"] is None
        assert data["outdoor_temp_max_f"] is None

    def test_only_some_floors_active(self):
        state = make_daily_state(
            furnace_runtime_s=900,
            session_count=2,
            floor_runtime_s={FLOOR_1: 900},
        )
        evt = emit_daily_summary(state, "2024-01-17")
        data = evt["data"]
        assert data["per_floor_runtime_s"]["floor_1"] == 900
        assert data["per_floor_runtime_s"]["floor_2"] == 0
        assert data["per_floor_runtime_s"]["floor_3"] == 0

    def test_zero_sessions(self):
        state = make_daily_state()
        evt = emit_daily_summary(state, "2024-01-18")
        data = evt["data"]
        assert data["session_count"] == 0
        assert data["total_furnace_runtime_s"] == 0
        assert data["per_floor_runtime_s"] == {"floor_1": 0, "floor_2": 0, "floor_3": 0}
        assert data["outdoor_temp_min_f"] is None
        assert data["outdoor_temp_max_f"] is None
