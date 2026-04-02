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


# ---------------------------------------------------------------------------
# _load_last_outdoor_temp — stale-state seeding
# ---------------------------------------------------------------------------


class TestLoadLastOutdoorTemp:
    """Tests for _load_last_outdoor_temp() and startup seeding behavior."""

    def _write_state(self, tmp_path, temp_f, recorded_at_iso):
        """Helper: write a minimal state file with the given outdoor temp fields."""
        import json

        sf = tmp_path / "state.json"
        payload = {
            "saved_at": recorded_at_iso,
            "daily_state": {
                "last_outdoor_temp_f": temp_f,
                "last_outdoor_temp_recorded_at": recorded_at_iso,
            },
        }
        sf.write_text(json.dumps(payload), encoding="utf-8")
        return sf

    def test_returns_temp_when_fresh(self, tmp_path):
        """Returns temp when recorded_at is within the stale window."""
        from datetime import timedelta

        from state import _load_last_outdoor_temp

        recorded_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        sf = self._write_state(tmp_path, 42.5, recorded_at)
        result = _load_last_outdoor_temp(state_file=sf)
        assert result == 42.5

    def test_returns_none_when_too_old(self, tmp_path):
        """Returns None when recorded_at exceeds the 3-hour stale threshold."""
        from datetime import timedelta

        from state import _load_last_outdoor_temp

        recorded_at = (datetime.now(UTC) - timedelta(hours=4)).isoformat()
        sf = self._write_state(tmp_path, 42.5, recorded_at)
        result = _load_last_outdoor_temp(state_file=sf)
        assert result is None

    def test_returns_none_when_recorded_at_missing(self, tmp_path):
        """Returns None when last_outdoor_temp_recorded_at is absent."""
        import json

        from state import _load_last_outdoor_temp

        sf = tmp_path / "state.json"
        sf.write_text(
            json.dumps(
                {
                    "saved_at": "2024-01-15T10:00:00+00:00",
                    "daily_state": {"last_outdoor_temp_f": 38.0},
                }
            ),
            encoding="utf-8",
        )
        result = _load_last_outdoor_temp(state_file=sf)
        assert result is None

    def test_returns_none_when_temp_missing(self, tmp_path):
        """Returns None when last_outdoor_temp_f is absent."""
        import json
        from datetime import timedelta

        from state import _load_last_outdoor_temp

        recorded_at = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        sf = tmp_path / "state.json"
        sf.write_text(
            json.dumps(
                {
                    "saved_at": recorded_at,
                    "daily_state": {"last_outdoor_temp_recorded_at": recorded_at},
                }
            ),
            encoding="utf-8",
        )
        result = _load_last_outdoor_temp(state_file=sf)
        assert result is None

    def test_returns_none_when_file_missing(self, tmp_path):
        """Returns None when the state file does not exist."""
        from state import _load_last_outdoor_temp

        result = _load_last_outdoor_temp(state_file=tmp_path / "nonexistent.json")
        assert result is None

    def test_respects_custom_stale_s(self, tmp_path):
        """Custom stale_s parameter is honored."""
        from datetime import timedelta

        from state import _load_last_outdoor_temp

        recorded_at = (datetime.now(UTC) - timedelta(minutes=90)).isoformat()
        sf = self._write_state(tmp_path, 55.0, recorded_at)
        # 90 min old with 60-min window → stale
        assert _load_last_outdoor_temp(state_file=sf, stale_s=3600) is None
        # 90 min old with 2-hour window → fresh
        assert _load_last_outdoor_temp(state_file=sf, stale_s=7200) == 55.0

    def test_handles_exactly_at_boundary(self, tmp_path):
        """Reading just inside the window is returned; just outside is not."""
        from datetime import timedelta

        from state import _load_last_outdoor_temp

        just_inside = (datetime.now(UTC) - timedelta(seconds=10799)).isoformat()
        sf = self._write_state(tmp_path, 33.0, just_inside)
        assert _load_last_outdoor_temp(state_file=sf) == 33.0

    def test_zero_temp_returned(self, tmp_path):
        """Zero °F is a valid temperature and must not be treated as falsy."""
        from datetime import timedelta

        from state import _load_last_outdoor_temp

        recorded_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        sf = self._write_state(tmp_path, 0.0, recorded_at)
        assert _load_last_outdoor_temp(state_file=sf) == 0.0
