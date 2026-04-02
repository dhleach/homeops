"""Tests for outdoor_temp_f enrichment on heating_session_ended.v1."""

from __future__ import annotations

from datetime import UTC, datetime

from processors import process_furnace_event

FURNACE = "binary_sensor.furnace_heating"
TS_STR = "2024-01-15T10:00:00+00:00"
TS = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
TS_START = datetime(2024, 1, 15, 9, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# outdoor_temp_f field on heating_session_ended.v1
# ---------------------------------------------------------------------------


class TestHeatingSessionOutdoorTemp:
    def test_outdoor_temp_included_when_provided(self):
        """outdoor_temp_f from last_outdoor_temp_f is written into the event."""
        events, _ = process_furnace_event(
            FURNACE, "on", "off", TS, TS_STR, TS_START, last_outdoor_temp_f=38.5
        )
        assert len(events) == 1
        assert events[0]["data"]["outdoor_temp_f"] == 38.5

    def test_outdoor_temp_none_when_not_provided(self):
        """outdoor_temp_f is None when no temp reading has arrived yet."""
        events, _ = process_furnace_event(FURNACE, "on", "off", TS, TS_STR, TS_START)
        assert len(events) == 1
        assert events[0]["data"]["outdoor_temp_f"] is None

    def test_outdoor_temp_none_when_explicitly_none(self):
        """Explicit None passes through cleanly."""
        events, _ = process_furnace_event(
            FURNACE, "on", "off", TS, TS_STR, TS_START, last_outdoor_temp_f=None
        )
        assert len(events) == 1
        assert events[0]["data"]["outdoor_temp_f"] is None

    def test_outdoor_temp_zero_degrees(self):
        """Zero is a valid temp (not falsy-filtered)."""
        events, _ = process_furnace_event(
            FURNACE, "on", "off", TS, TS_STR, TS_START, last_outdoor_temp_f=0.0
        )
        assert len(events) == 1
        assert events[0]["data"]["outdoor_temp_f"] == 0.0

    def test_outdoor_temp_negative(self):
        """Negative temps are valid."""
        events, _ = process_furnace_event(
            FURNACE, "on", "off", TS, TS_STR, TS_START, last_outdoor_temp_f=-5.2
        )
        assert len(events) == 1
        assert events[0]["data"]["outdoor_temp_f"] == -5.2

    def test_outdoor_temp_not_in_session_started(self):
        """heating_session_started.v1 does not include outdoor_temp_f."""
        events, _ = process_furnace_event(
            FURNACE, "off", "on", TS, TS_STR, None, last_outdoor_temp_f=42.0
        )
        assert len(events) == 1
        assert events[0]["schema"] == "homeops.consumer.heating_session_started.v1"
        assert "outdoor_temp_f" not in events[0]["data"]

    def test_outdoor_temp_field_present_in_schema(self):
        """outdoor_temp_f key is always present in ended event data."""
        events, _ = process_furnace_event(
            FURNACE, "on", "off", TS, TS_STR, TS_START, last_outdoor_temp_f=55.0
        )
        assert "outdoor_temp_f" in events[0]["data"]

    def test_outdoor_temp_high_value(self):
        """High summer temps are valid (e.g. 95°F)."""
        events, _ = process_furnace_event(
            FURNACE, "on", "off", TS, TS_STR, TS_START, last_outdoor_temp_f=95.0
        )
        assert events[0]["data"]["outdoor_temp_f"] == 95.0

    def test_duration_unaffected_by_outdoor_temp(self):
        """Adding outdoor_temp_f does not change duration_s calculation."""
        events, _ = process_furnace_event(
            FURNACE, "on", "off", TS, TS_STR, TS_START, last_outdoor_temp_f=32.0
        )
        assert events[0]["data"]["duration_s"] == 3600

    def test_no_session_ended_event_when_off_to_on(self):
        """off→on transition emits started, not ended — no outdoor_temp_f issue."""
        events, _ = process_furnace_event(
            FURNACE, "off", "on", TS, TS_STR, None, last_outdoor_temp_f=40.0
        )
        schemas = [e["schema"] for e in events]
        assert "homeops.consumer.heating_session_ended.v1" not in schemas
