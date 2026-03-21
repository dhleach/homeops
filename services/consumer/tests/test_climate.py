"""Unit tests for process_climate_event() in consumer.py."""

from consumer import process_climate_event

FLOOR_1_CLIMATE = "climate.floor_1_thermostat"
FLOOR_2_CLIMATE = "climate.floor_2_thermostat"
FLOOR_3_CLIMATE = "climate.floor_3_thermostat"
UNKNOWN_ENTITY = "climate.unknown_thermostat"

TS_STR = "2024-01-15T10:00:00+00:00"

BASE_ATTRS = {
    "temperature": 68.0,
    "current_temperature": 70.0,
    "hvac_mode": "heat",
    "hvac_action": "idle",
}


def make_attrs(**overrides):
    attrs = dict(BASE_ATTRS)
    attrs.update(overrides)
    return attrs


def make_prev(entity_id, setpoint=68.0, current_temp=70.0, hvac_mode="heat", hvac_action="idle"):
    return {
        entity_id: {
            "setpoint": setpoint,
            "current_temp": current_temp,
            "hvac_mode": hvac_mode,
            "hvac_action": hvac_action,
        }
    }


SCHEMA_SETPOINT = "homeops.consumer.thermostat_setpoint_changed.v1"
SCHEMA_CURRENT_TEMP = "homeops.consumer.thermostat_current_temp_updated.v1"
SCHEMA_MODE = "homeops.consumer.thermostat_mode_changed.v1"


class TestSetpointChange:
    def test_setpoint_change_emits_event(self):
        prev = make_prev(FLOOR_1_CLIMATE, setpoint=66.0)
        attrs = make_attrs(temperature=68.0)
        events, _ = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_SETPOINT in schemas

    def test_setpoint_change_event_data(self):
        prev = make_prev(FLOOR_1_CLIMATE, setpoint=66.0)
        attrs = make_attrs(temperature=68.0)
        events, _ = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_STR, prev)
        evt = next(e for e in events if e["schema"] == SCHEMA_SETPOINT)
        d = evt["data"]
        assert d["entity_id"] == FLOOR_1_CLIMATE
        assert d["zone"] == "floor_1"
        assert d["setpoint"] == 68.0
        assert d["current_temp"] == 70.0
        assert d["hvac_mode"] == "heat"
        assert d["hvac_action"] == "idle"
        assert d["ts"] == TS_STR

    def test_no_setpoint_event_when_unchanged(self):
        prev = make_prev(FLOOR_1_CLIMATE, setpoint=68.0)
        attrs = make_attrs(temperature=68.0)
        events, _ = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_SETPOINT not in schemas


class TestCurrentTempChange:
    def test_current_temp_change_emits_event(self):
        prev = make_prev(FLOOR_2_CLIMATE, current_temp=70.0)
        attrs = make_attrs(current_temperature=71.0)
        events, _ = process_climate_event(FLOOR_2_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_CURRENT_TEMP in schemas

    def test_current_temp_event_data(self):
        prev = make_prev(FLOOR_2_CLIMATE, current_temp=70.0)
        attrs = make_attrs(current_temperature=71.0)
        events, _ = process_climate_event(FLOOR_2_CLIMATE, attrs, TS_STR, prev)
        evt = next(e for e in events if e["schema"] == SCHEMA_CURRENT_TEMP)
        d = evt["data"]
        assert d["entity_id"] == FLOOR_2_CLIMATE
        assert d["zone"] == "floor_2"
        assert d["current_temp"] == 71.0

    def test_no_current_temp_event_when_unchanged(self):
        prev = make_prev(FLOOR_2_CLIMATE, current_temp=70.0)
        attrs = make_attrs(current_temperature=70.0)
        events, _ = process_climate_event(FLOOR_2_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_CURRENT_TEMP not in schemas


class TestHvacModeChange:
    def test_hvac_mode_change_emits_event(self):
        prev = make_prev(FLOOR_3_CLIMATE, hvac_mode="heat")
        attrs = make_attrs(hvac_mode="off")
        events, _ = process_climate_event(FLOOR_3_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_MODE in schemas

    def test_hvac_mode_event_data(self):
        prev = make_prev(FLOOR_3_CLIMATE, hvac_mode="heat")
        attrs = make_attrs(hvac_mode="off")
        events, _ = process_climate_event(FLOOR_3_CLIMATE, attrs, TS_STR, prev)
        evt = next(e for e in events if e["schema"] == SCHEMA_MODE)
        d = evt["data"]
        assert d["entity_id"] == FLOOR_3_CLIMATE
        assert d["zone"] == "floor_3"
        assert d["hvac_mode"] == "off"

    def test_no_mode_event_when_unchanged(self):
        prev = make_prev(FLOOR_3_CLIMATE)
        attrs = make_attrs()
        events, _ = process_climate_event(FLOOR_3_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_MODE not in schemas


class TestHvacActionChange:
    def test_hvac_action_heating_to_idle_emits_mode_event(self):
        prev = make_prev(FLOOR_1_CLIMATE, hvac_action="heating")
        attrs = make_attrs(hvac_action="idle")
        events, _ = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_MODE in schemas

    def test_hvac_action_idle_to_heating_emits_mode_event(self):
        prev = make_prev(FLOOR_1_CLIMATE, hvac_action="idle")
        attrs = make_attrs(hvac_action="heating")
        events, _ = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_STR, prev)
        schemas = [e["schema"] for e in events]
        assert SCHEMA_MODE in schemas


class TestIdempotent:
    def test_no_events_when_nothing_changes(self):
        prev = make_prev(FLOOR_1_CLIMATE)
        attrs = make_attrs()
        events, _ = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_STR, prev)
        assert events == []

    def test_state_updated_after_processing(self):
        prev = make_prev(FLOOR_1_CLIMATE, setpoint=66.0)
        attrs = make_attrs(temperature=68.0)
        _, updated = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_STR, prev)
        assert updated[FLOOR_1_CLIMATE]["setpoint"] == 68.0


class TestUnknownEntity:
    def test_unknown_entity_ignored(self):
        events, state = process_climate_event(UNKNOWN_ENTITY, BASE_ATTRS, TS_STR, {})
        assert events == []
        assert state == {}

    def test_empty_attributes_ignored(self):
        events, state = process_climate_event(FLOOR_1_CLIMATE, {}, TS_STR, {})
        assert events == []
        assert state == {}

    def test_none_attributes_ignored(self):
        events, state = process_climate_event(FLOOR_1_CLIMATE, None, TS_STR, {})
        assert events == []
        assert state == {}


class TestFirstSeen:
    def test_first_event_emits_all_three_events(self):
        """On first sight (no prior state), all fields are considered changed."""
        events, updated = process_climate_event(FLOOR_1_CLIMATE, BASE_ATTRS, TS_STR, {})
        schemas = [e["schema"] for e in events]
        assert SCHEMA_SETPOINT in schemas
        assert SCHEMA_CURRENT_TEMP in schemas
        assert SCHEMA_MODE in schemas
        assert updated[FLOOR_1_CLIMATE]["setpoint"] == 68.0
