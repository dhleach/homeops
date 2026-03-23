"""Tests for zone_time_to_temp.v1 and zone_overshoot.v1 events."""

from consumer import process_climate_event
from dateutil.parser import isoparse

FLOOR_1_CLIMATE = "climate.floor_1_thermostat"
FLOOR_2_CLIMATE = "climate.floor_2_thermostat"
FLOOR_3_CLIMATE = "climate.floor_3_thermostat"

SCHEMA_ZONE_TIME_TO_TEMP = "homeops.consumer.zone_time_to_temp.v1"
SCHEMA_ZONE_OVERSHOOT = "homeops.consumer.zone_overshoot.v1"
SCHEMA_SETPOINT_REACHED = "homeops.consumer.thermostat_setpoint_reached.v1"

F1_FLOOR_ENTITY = "binary_sensor.floor_1_heating_call"
F2_FLOOR_ENTITY = "binary_sensor.floor_2_heating_call"
F3_FLOOR_ENTITY = "binary_sensor.floor_3_heating_call"

TS_START = "2024-01-15T10:00:00+00:00"
TS_SETPOINT_REACHED = "2024-01-15T10:30:00+00:00"  # 30 min after start
TS_SESSION_END = "2024-01-15T10:40:00+00:00"  # 10 min after setpoint reached


def make_prev_heating(
    entity_id,
    setpoint=70.0,
    current_temp=67.5,
    heating_start_temp=65.0,
    heating_start_ts_str=TS_START,
    setpoint_reached_ts=None,
    setpoint_reached_temp=None,
    post_setpoint_temps=None,
    heating_start_other_zones=None,
    setpoint_changed_during_heating=False,
):
    return {
        entity_id: {
            "setpoint": setpoint,
            "current_temp": current_temp,
            "hvac_mode": "heat",
            "hvac_action": "heating",
            "heating_start_temp": heating_start_temp,
            "heating_start_ts": isoparse(heating_start_ts_str) if heating_start_ts_str else None,
            "setpoint_reached_ts": setpoint_reached_ts,
            "setpoint_reached_temp": setpoint_reached_temp,
            "post_setpoint_temps": post_setpoint_temps or [],
            "heating_start_other_zones": heating_start_other_zones or [],
            "setpoint_changed_during_heating": setpoint_changed_during_heating,
        }
    }


class TestZoneTimeToTemp:
    def test_fires_with_all_fields_populated(self):
        """zone_time_to_temp.v1 fires at setpoint crossing with correct field values."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=69.5,
            heating_start_temp=65.0,
            heating_start_ts_str=TS_START,
        )
        attrs = {
            "temperature": 70.0,
            "current_temperature": 70.5,
            "hvac_action": "heating",
        }
        floor_on_since = {
            F1_FLOOR_ENTITY: isoparse(TS_START),
            F2_FLOOR_ENTITY: isoparse(TS_START),
            F3_FLOOR_ENTITY: None,
        }
        daily_state = {"last_outdoor_temp_f": 32.0}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE,
            attrs,
            TS_SETPOINT_REACHED,
            prev,
            new_state="heat",
            floor_on_since=floor_on_since,
            daily_state=daily_state,
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_ZONE_TIME_TO_TEMP in schemas

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_TIME_TO_TEMP)
        d = evt["data"]
        assert d["entity_id"] == FLOOR_1_CLIMATE
        assert d["zone"] == "floor_1"
        assert d["start_temp"] == 65.0
        assert d["setpoint"] == 70.0
        assert d["setpoint_delta"] == 5.0  # 70.0 - 65.0
        assert d["duration_s"] == 1800  # 30 minutes
        assert d["end_temp"] == 70.5
        assert d["degrees_gained"] == 5.5  # 70.5 - 65.0
        assert d["degrees_per_min"] == round(5.5 / 30, 3)
        assert d["outdoor_temp_f"] == 32.0
        # floor_1's own entity excluded; floor_2 is active; floor_3 is None
        assert F2_FLOOR_ENTITY in d["other_zones_calling"]
        assert F1_FLOOR_ENTITY not in d["other_zones_calling"]
        assert F3_FLOOR_ENTITY not in d["other_zones_calling"]

    def test_also_fires_setpoint_reached(self):
        """zone_time_to_temp.v1 fires alongside thermostat_setpoint_reached.v1."""
        prev = make_prev_heating(FLOOR_1_CLIMATE, setpoint=70.0, current_temp=69.5)
        attrs = {"temperature": 70.0, "current_temperature": 70.0, "hvac_action": "heating"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SETPOINT_REACHED, prev, new_state="heat"
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_SETPOINT_REACHED in schemas
        assert SCHEMA_ZONE_TIME_TO_TEMP in schemas

    def test_no_fire_without_heating_start_state(self):
        """zone_time_to_temp.v1 does NOT fire when heating_start_ts is None."""
        prev = {
            FLOOR_1_CLIMATE: {
                "setpoint": 70.0,
                "current_temp": 69.5,
                "hvac_mode": "heat",
                "hvac_action": "heating",
                "heating_start_temp": None,
                "heating_start_ts": None,
                "setpoint_reached_ts": None,
                "setpoint_reached_temp": None,
                "post_setpoint_temps": [],
                "heating_start_other_zones": None,
            }
        }
        attrs = {"temperature": 70.0, "current_temperature": 70.5, "hvac_action": "heating"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SETPOINT_REACHED, prev, new_state="heat"
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_SETPOINT_REACHED in schemas
        assert SCHEMA_ZONE_TIME_TO_TEMP not in schemas

    def test_other_zones_calling_excludes_own_zone(self):
        """other_zones_calling excludes the emitting zone's floor entity."""
        prev = make_prev_heating(FLOOR_2_CLIMATE, setpoint=70.0, current_temp=69.5)
        attrs = {"temperature": 70.0, "current_temperature": 70.0, "hvac_action": "heating"}
        floor_on_since = {
            F1_FLOOR_ENTITY: isoparse(TS_START),
            F2_FLOOR_ENTITY: isoparse(TS_START),
            F3_FLOOR_ENTITY: isoparse(TS_START),
        }

        events, _ = process_climate_event(
            FLOOR_2_CLIMATE,
            attrs,
            TS_SETPOINT_REACHED,
            prev,
            new_state="heat",
            floor_on_since=floor_on_since,
        )

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_TIME_TO_TEMP)
        calling = evt["data"]["other_zones_calling"]
        assert F2_FLOOR_ENTITY not in calling
        assert F1_FLOOR_ENTITY in calling
        assert F3_FLOOR_ENTITY in calling

    def test_other_zones_calling_empty_when_no_active_zones(self):
        """other_zones_calling is empty when no other floors are active."""
        prev = make_prev_heating(FLOOR_1_CLIMATE, setpoint=70.0, current_temp=69.5)
        attrs = {"temperature": 70.0, "current_temperature": 70.0, "hvac_action": "heating"}
        floor_on_since = {
            F1_FLOOR_ENTITY: isoparse(TS_START),
            F2_FLOOR_ENTITY: None,
            F3_FLOOR_ENTITY: None,
        }

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE,
            attrs,
            TS_SETPOINT_REACHED,
            prev,
            new_state="heat",
            floor_on_since=floor_on_since,
        )

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_TIME_TO_TEMP)
        assert evt["data"]["other_zones_calling"] == []

    def test_outdoor_temp_null_when_not_set(self):
        """outdoor_temp_f is None when daily_state has no last_outdoor_temp_f."""
        prev = make_prev_heating(FLOOR_1_CLIMATE, setpoint=70.0, current_temp=69.5)
        attrs = {"temperature": 70.0, "current_temperature": 70.0, "hvac_action": "heating"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SETPOINT_REACHED, prev, new_state="heat", daily_state={}
        )

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_TIME_TO_TEMP)
        assert evt["data"]["outdoor_temp_f"] is None

    def test_state_records_setpoint_reached_ts(self):
        """After setpoint reached, setpoint_reached_ts is persisted in climate_state."""
        prev = make_prev_heating(FLOOR_1_CLIMATE, setpoint=70.0, current_temp=69.5)
        attrs = {"temperature": 70.0, "current_temperature": 70.0, "hvac_action": "heating"}

        _, updated = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SETPOINT_REACHED, prev, new_state="heat"
        )

        state = updated[FLOOR_1_CLIMATE]
        assert state["setpoint_reached_ts"] is not None
        assert state["post_setpoint_temps"] == [70.0]


class TestZoneOvershoot:
    def test_fires_when_setpoint_reached_before_session_end(self):
        """zone_overshoot.v1 fires when heating ends after setpoint was reached."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=71.0,
            heating_start_temp=65.0,
            heating_start_ts_str=TS_START,
            setpoint_reached_ts=isoparse(TS_SETPOINT_REACHED),
            setpoint_reached_temp=70.0,
            post_setpoint_temps=[70.0, 71.0],
            heating_start_other_zones=[F2_FLOOR_ENTITY],
        )
        attrs = {"temperature": 70.0, "current_temperature": 71.5, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE,
            attrs,
            TS_SESSION_END,
            prev,
            new_state="heat",
            daily_state={"last_outdoor_temp_f": 35.0},
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_ZONE_OVERSHOOT in schemas

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_OVERSHOOT)
        d = evt["data"]
        assert d["entity_id"] == FLOOR_1_CLIMATE
        assert d["zone"] == "floor_1"
        assert d["start_temp"] == 65.0
        assert d["setpoint"] == 70.0
        assert d["setpoint_delta"] == 5.0
        assert d["end_temp"] == 71.5
        assert d["overshoot_s"] == 600  # 10 minutes between setpoint reached and session end
        assert d["outdoor_temp_f"] == 35.0
        assert d["other_zones_calling"] == [F2_FLOOR_ENTITY]

    def test_no_fire_when_setpoint_never_reached(self):
        """zone_overshoot.v1 does NOT fire when setpoint was never reached in the session."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=69.0,
            heating_start_temp=65.0,
            setpoint_reached_ts=None,
            post_setpoint_temps=[],
        )
        attrs = {"temperature": 70.0, "current_temperature": 69.0, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SESSION_END, prev, new_state="heat"
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_ZONE_OVERSHOOT not in schemas

    def test_peak_temp_null_with_one_post_setpoint_reading(self):
        """peak_temp is None when only one reading exists in the post-setpoint window."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=70.0,
            setpoint_reached_ts=isoparse(TS_SETPOINT_REACHED),
            post_setpoint_temps=[70.0],  # only one reading
        )
        attrs = {"temperature": 70.0, "current_temperature": 70.5, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SESSION_END, prev, new_state="heat"
        )

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_OVERSHOOT)
        assert evt["data"]["peak_temp"] is None

    def test_peak_temp_correct_with_multiple_readings(self):
        """peak_temp is the maximum temperature observed after setpoint was reached."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=71.5,
            setpoint_reached_ts=isoparse(TS_SETPOINT_REACHED),
            post_setpoint_temps=[70.0, 71.5, 71.0, 70.8],  # multiple readings
        )
        attrs = {"temperature": 70.0, "current_temperature": 70.5, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SESSION_END, prev, new_state="heat"
        )

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_OVERSHOOT)
        assert evt["data"]["peak_temp"] == 71.5

    def test_state_cleared_after_session_end(self):
        """Heating session state is fully cleared when hvac_action leaves 'heating'."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=71.0,
            setpoint_reached_ts=isoparse(TS_SETPOINT_REACHED),
            post_setpoint_temps=[70.0, 71.0],
        )
        attrs = {"temperature": 70.0, "current_temperature": 70.5, "hvac_action": "idle"}

        _, updated = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SESSION_END, prev, new_state="heat"
        )

        state = updated[FLOOR_1_CLIMATE]
        assert state["heating_start_temp"] is None
        assert state["heating_start_ts"] is None
        assert state["setpoint_reached_ts"] is None
        assert state["setpoint_reached_temp"] is None
        assert state["post_setpoint_temps"] == []
        assert state["heating_start_other_zones"] is None

    def test_other_zones_calling_reflects_session_start(self):
        """other_zones_calling in overshoot comes from the captured state at heating start."""
        start_zones = [F3_FLOOR_ENTITY]
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=71.0,
            setpoint_reached_ts=isoparse(TS_SETPOINT_REACHED),
            post_setpoint_temps=[70.0, 71.0],
            heating_start_other_zones=start_zones,
        )
        attrs = {"temperature": 70.0, "current_temperature": 70.5, "hvac_action": "idle"}
        # Pass different floor_on_since — should not affect the overshoot event
        floor_on_since = {
            F1_FLOOR_ENTITY: isoparse(TS_START),
            F2_FLOOR_ENTITY: isoparse(TS_START),  # newly active, not in start_zones
            F3_FLOOR_ENTITY: None,
        }

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE,
            attrs,
            TS_SESSION_END,
            prev,
            new_state="heat",
            floor_on_since=floor_on_since,
        )

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_OVERSHOOT)
        assert evt["data"]["other_zones_calling"] == [F3_FLOOR_ENTITY]


class TestHeatingSessionTracking:
    def test_heating_start_records_state(self):
        """When hvac_action transitions to 'heating', session start state is recorded."""
        prev = {
            FLOOR_1_CLIMATE: {
                "setpoint": 70.0,
                "current_temp": 65.0,
                "hvac_mode": "heat",
                "hvac_action": "idle",
                "heating_start_temp": None,
                "heating_start_ts": None,
                "setpoint_reached_ts": None,
                "setpoint_reached_temp": None,
                "post_setpoint_temps": [],
                "heating_start_other_zones": None,
            }
        }
        attrs = {"temperature": 70.0, "current_temperature": 65.0, "hvac_action": "heating"}
        floor_on_since = {
            F1_FLOOR_ENTITY: isoparse(TS_START),
            F2_FLOOR_ENTITY: isoparse(TS_START),
            F3_FLOOR_ENTITY: None,
        }

        _, updated = process_climate_event(
            FLOOR_1_CLIMATE,
            attrs,
            TS_START,
            prev,
            new_state="heat",
            floor_on_since=floor_on_since,
        )

        state = updated[FLOOR_1_CLIMATE]
        assert state["heating_start_temp"] == 65.0
        assert state["heating_start_ts"] is not None
        assert state["setpoint_reached_ts"] is None
        # floor_1 excluded from other_zones_calling at start; floor_2 active
        assert F2_FLOOR_ENTITY in state["heating_start_other_zones"]
        assert F1_FLOOR_ENTITY not in state["heating_start_other_zones"]

    def test_post_setpoint_temps_accumulate(self):
        """Temperature readings after setpoint reached are appended to post_setpoint_temps."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=70.5,
            setpoint_reached_ts=isoparse(TS_SETPOINT_REACHED),
            setpoint_reached_temp=70.0,
            post_setpoint_temps=[70.0],
        )
        attrs = {"temperature": 70.0, "current_temperature": 71.0, "hvac_action": "heating"}

        _, updated = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SESSION_END, prev, new_state="heat"
        )

        state = updated[FLOOR_1_CLIMATE]
        assert 71.0 in state["post_setpoint_temps"]

    def test_simultaneous_setpoint_reached_and_session_end(self):
        """When setpoint is crossed and heating ends in the same event, both events fire."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=69.5,
            heating_start_temp=65.0,
            heating_start_ts_str=TS_START,
        )
        attrs = {"temperature": 70.0, "current_temperature": 70.5, "hvac_action": "idle"}

        events, updated = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SETPOINT_REACHED, prev, new_state="heat"
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_SETPOINT_REACHED in schemas
        assert SCHEMA_ZONE_TIME_TO_TEMP in schemas
        assert SCHEMA_ZONE_OVERSHOOT in schemas

        overshoot = next(e for e in events if e["schema"] == SCHEMA_ZONE_OVERSHOOT)
        assert overshoot["data"]["overshoot_s"] == 0
        assert overshoot["data"]["peak_temp"] is None  # only one reading

        # State fully cleared
        state = updated[FLOOR_1_CLIMATE]
        assert state["heating_start_ts"] is None
        assert state["setpoint_reached_ts"] is None


SCHEMA_ZONE_UNDERSHOOT = "homeops.consumer.zone_undershoot.v1"

TS_UNDERSHOOT_END = "2024-01-15T10:20:40+00:00"  # 1240s after TS_START


class TestZoneUndershoot:
    def test_fires_with_all_fields_populated(self):
        """zone_undershoot.v1 fires when heating ends without reaching setpoint."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=71.0,
            current_temp=69.5,
            heating_start_temp=68.0,
            heating_start_ts_str=TS_START,
            setpoint_reached_ts=None,
        )
        attrs = {"temperature": 71.0, "current_temperature": 69.5, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE,
            attrs,
            TS_UNDERSHOOT_END,
            prev,
            new_state="heat",
            daily_state={"last_outdoor_temp_f": 28.0},
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_ZONE_UNDERSHOOT in schemas

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_UNDERSHOOT)
        d = evt["data"]
        assert d["entity_id"] == FLOOR_1_CLIMATE
        assert d["zone"] == "floor_1"
        assert d["start_temp_f"] == 68.0
        assert d["final_temp_f"] == 69.5
        assert d["setpoint_f"] == 71.0
        assert d["shortfall_f"] == 1.5
        assert d["call_duration_s"] == 1240
        assert d["outdoor_temp_f"] == 28.0
        assert d["likely_cause"] == "unknown"

    def test_likely_cause_thermostat_adjustment(self):
        """likely_cause is 'thermostat_adjustment' when setpoint changed during heating."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=71.0,
            current_temp=69.5,
            heating_start_temp=68.0,
            setpoint_reached_ts=None,
            setpoint_changed_during_heating=True,
        )
        attrs = {"temperature": 71.0, "current_temperature": 69.5, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_UNDERSHOOT_END, prev, new_state="heat"
        )

        evt = next(e for e in events if e["schema"] == SCHEMA_ZONE_UNDERSHOOT)
        assert evt["data"]["likely_cause"] == "thermostat_adjustment"

    def test_no_fire_when_setpoint_was_reached(self):
        """zone_undershoot.v1 does NOT fire when setpoint was reached (overshoot path)."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=70.5,
            setpoint_reached_ts=isoparse(TS_SETPOINT_REACHED),
            post_setpoint_temps=[70.0, 70.5],
        )
        attrs = {"temperature": 70.0, "current_temperature": 70.5, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_SESSION_END, prev, new_state="heat"
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_ZONE_UNDERSHOOT not in schemas
        assert SCHEMA_ZONE_OVERSHOOT in schemas

    def test_no_fire_when_setpoint_is_none(self):
        """zone_undershoot.v1 is skipped when setpoint is None."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=None,
            current_temp=69.5,
            heating_start_temp=68.0,
            setpoint_reached_ts=None,
        )
        attrs = {"temperature": None, "current_temperature": 69.5, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_UNDERSHOOT_END, prev, new_state="heat"
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_ZONE_UNDERSHOOT not in schemas

    def test_no_fire_when_current_temp_is_none(self):
        """zone_undershoot.v1 is skipped when current_temp is None."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=71.0,
            current_temp=69.5,
            heating_start_temp=68.0,
            setpoint_reached_ts=None,
        )
        attrs = {"temperature": 71.0, "current_temperature": None, "hvac_action": "idle"}

        events, _ = process_climate_event(
            FLOOR_1_CLIMATE, attrs, TS_UNDERSHOOT_END, prev, new_state="heat"
        )

        schemas = [e["schema"] for e in events]
        assert SCHEMA_ZONE_UNDERSHOOT not in schemas

    def test_setpoint_changed_flag_set_when_setpoint_changes_during_heating(self):
        """setpoint_changed_during_heating is set True when setpoint changes while heating."""
        prev = make_prev_heating(
            FLOOR_1_CLIMATE,
            setpoint=70.0,
            current_temp=68.0,
            setpoint_reached_ts=None,
            setpoint_changed_during_heating=False,
        )
        # Setpoint changes from 70.0 to 71.0 while still heating
        attrs = {"temperature": 71.0, "current_temperature": 68.0, "hvac_action": "heating"}

        _, updated = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_START, prev, new_state="heat")

        assert updated[FLOOR_1_CLIMATE]["setpoint_changed_during_heating"] is True

    def test_setpoint_changed_flag_reset_on_new_heating_session(self):
        """setpoint_changed_during_heating resets to False at the start of each heating session."""
        prev = {
            FLOOR_1_CLIMATE: {
                "setpoint": 70.0,
                "current_temp": 68.0,
                "hvac_mode": "heat",
                "hvac_action": "idle",
                "heating_start_temp": None,
                "heating_start_ts": None,
                "setpoint_reached_ts": None,
                "setpoint_reached_temp": None,
                "post_setpoint_temps": [],
                "heating_start_other_zones": None,
                "setpoint_changed_during_heating": True,  # leftover from previous session
            }
        }
        attrs = {"temperature": 70.0, "current_temperature": 68.0, "hvac_action": "heating"}

        _, updated = process_climate_event(FLOOR_1_CLIMATE, attrs, TS_START, prev, new_state="heat")

        assert updated[FLOOR_1_CLIMATE]["setpoint_changed_during_heating"] is False
