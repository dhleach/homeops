"""Tests for pi_reader.py — event parsing and caching logic."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from pi_reader import PiReader, TempsData, ThermostatReading, _parse_events

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    {
        "schema": "homeops.consumer.thermostat_current_temp_updated.v1",
        "source": "consumer",
        "ts": "2026-04-02T19:56:58Z",
        "data": {
            "entity_id": "climate.floor_1_thermostat",
            "zone": "floor_1",
            "ts": "2026-04-02T19:56:58Z",
            "hvac_mode": "heat",
            "hvac_action": "idle",
            "setpoint": 68,
            "current_temp": 70,
        },
    },
    {
        "schema": "homeops.consumer.thermostat_current_temp_updated.v1",
        "source": "consumer",
        "ts": "2026-04-02T20:00:00Z",
        "data": {
            "entity_id": "climate.floor_2_thermostat",
            "zone": "floor_2",
            "ts": "2026-04-02T20:00:00Z",
            "hvac_mode": "heat",
            "hvac_action": "heating",
            "setpoint": 70,
            "current_temp": 68,
        },
    },
    {
        "schema": "homeops.consumer.outdoor_temp_updated.v1",
        "source": "consumer",
        "ts": "2026-04-02T20:01:00Z",
        "data": {
            "entity_id": "sensor.outdoor_temperature",
            "temperature_f": 52.3,
            "timestamp": "2026-04-02T20:01:00Z",
        },
    },
]

SAMPLE_JSONL = "\n".join(json.dumps(e) for e in SAMPLE_EVENTS)


# ---------------------------------------------------------------------------
# _parse_events
# ---------------------------------------------------------------------------


def test_parse_events_zones():
    data = _parse_events(SAMPLE_JSONL)
    assert set(data.zones.keys()) == {"floor_1", "floor_2"}


def test_parse_events_floor1_temps():
    data = _parse_events(SAMPLE_JSONL)
    f1 = data.zones["floor_1"]
    assert f1.current_temp_f == 70
    assert f1.setpoint_f == 68
    assert f1.hvac_mode == "heat"
    assert f1.hvac_action == "idle"


def test_parse_events_floor2_temps():
    data = _parse_events(SAMPLE_JSONL)
    f2 = data.zones["floor_2"]
    assert f2.current_temp_f == 68
    assert f2.hvac_action == "heating"


def test_parse_events_outdoor_temp():
    data = _parse_events(SAMPLE_JSONL)
    assert data.outdoor_temp_f == pytest.approx(52.3)
    assert data.outdoor_last_updated == "2026-04-02T20:01:00Z"


def test_parse_events_empty_string():
    data = _parse_events("")
    assert data.zones == {}
    assert data.outdoor_temp_f is None


def test_parse_events_skips_invalid_json():
    raw = "not json\n" + SAMPLE_JSONL
    data = _parse_events(raw)
    assert len(data.zones) == 2


def test_parse_events_last_value_wins():
    """If the same zone appears twice, the later reading should win."""
    updated = {
        "schema": "homeops.consumer.thermostat_current_temp_updated.v1",
        "source": "consumer",
        "ts": "2026-04-02T21:00:00Z",
        "data": {
            "entity_id": "climate.floor_1_thermostat",
            "zone": "floor_1",
            "ts": "2026-04-02T21:00:00Z",
            "hvac_mode": "heat",
            "hvac_action": "heating",
            "setpoint": 68,
            "current_temp": 72,
        },
    }
    raw = SAMPLE_JSONL + "\n" + json.dumps(updated)
    data = _parse_events(raw)
    assert data.zones["floor_1"].current_temp_f == 72
    assert data.zones["floor_1"].hvac_action == "heating"


def test_parse_events_thermostat_mode_changed():
    """thermostat_mode_changed events also contain temp data — should be parsed."""
    event = {
        "schema": "homeops.consumer.thermostat_mode_changed.v1",
        "source": "consumer",
        "ts": "2026-04-02T20:30:00Z",
        "data": {
            "entity_id": "climate.floor_3_thermostat",
            "zone": "floor_3",
            "ts": "2026-04-02T20:30:00Z",
            "hvac_mode": "cool",
            "hvac_action": "idle",
            "setpoint": 72,
            "current_temp": 75,
        },
    }
    data = _parse_events(json.dumps(event))
    assert "floor_3" in data.zones
    assert data.zones["floor_3"].hvac_mode == "cool"


def test_parse_events_no_outdoor():
    """No outdoor event → outdoor_temp_f is None."""
    raw = "\n".join(json.dumps(e) for e in SAMPLE_EVENTS if "outdoor" not in e["schema"])
    data = _parse_events(raw)
    assert data.outdoor_temp_f is None
    assert data.outdoor_last_updated is None


def test_parse_events_skips_blank_lines():
    raw = "\n\n" + SAMPLE_JSONL + "\n\n"
    data = _parse_events(raw)
    assert len(data.zones) == 2


# ---------------------------------------------------------------------------
# PiReader — caching behaviour (no real SSH)
# ---------------------------------------------------------------------------


def _make_reader() -> PiReader:
    return PiReader(
        host="10.0.0.1",
        user="bob",
        key_path="/fake/key",
        events_path="/fake/events.jsonl",
        cache_ttl=30,
    )


def _make_fake_data(temp: float = 70.0) -> TempsData:
    return TempsData(
        zones={
            "floor_1": ThermostatReading(
                zone="floor_1",
                entity_id="climate.floor_1_thermostat",
                current_temp_f=temp,
                setpoint_f=68,
                hvac_mode="heat",
                hvac_action="idle",
                last_updated="2026-04-02T20:00:00Z",
            )
        },
        outdoor_temp_f=52.0,
        outdoor_last_updated="2026-04-02T20:00:00Z",
    )


def test_reader_calls_fetch_on_first_call():
    reader = _make_reader()
    fake_data = _make_fake_data()
    with patch.object(reader, "_fetch_from_pi", return_value=fake_data) as mock_fetch:
        result = reader.get_temps()
    mock_fetch.assert_called_once()
    assert result.zones["floor_1"].current_temp_f == 70.0


def test_reader_uses_cache_within_ttl():
    reader = _make_reader()
    call_count = 0

    def fake_fetch():
        nonlocal call_count
        call_count += 1
        return _make_fake_data(temp=float(call_count * 10))

    with patch.object(reader, "_fetch_from_pi", side_effect=fake_fetch):
        reader.get_temps()
        reader.get_temps()
        reader.get_temps()

    assert call_count == 1  # cache hit — only one real fetch


def test_reader_refreshes_after_ttl_expires():
    reader = _make_reader()
    reader.cache_ttl = 0  # expire immediately

    call_count = 0

    def fake_fetch():
        nonlocal call_count
        call_count += 1
        return _make_fake_data()

    with patch.object(reader, "_fetch_from_pi", side_effect=fake_fetch):
        reader.get_temps()
        time.sleep(0.01)
        reader.get_temps()

    assert call_count == 2


def test_reader_propagates_ssh_exception():
    reader = _make_reader()
    with patch.object(reader, "_fetch_from_pi", side_effect=ConnectionError("SSH failed")):
        with pytest.raises(ConnectionError, match="SSH failed"):
            reader.get_temps()
