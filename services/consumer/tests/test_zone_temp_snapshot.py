"""Tests for write_zone_temp_snapshot and the 5-minute interval logic."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from consumer import (
    ZONE_TEMP_SNAPSHOT_INTERVAL_S,
    _empty_daily_state,
    write_zone_temp_snapshot,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CLIMATE_STATE_FULL = {
    "climate.floor_1_thermostat": {
        "zone": "floor_1",
        "current_temp": 68.0,
        "setpoint": 68,
        "hvac_action": "idle",
    },
    "climate.floor_2_thermostat": {
        "zone": "floor_2",
        "current_temp": 67.0,
        "setpoint": 68,
        "hvac_action": "heating",
    },
    "climate.floor_3_thermostat": {
        "zone": "floor_3",
        "current_temp": 70.0,
        "setpoint": 68,
        "hvac_action": "idle",
    },
}


def _daily_state_with_outdoor(temp=48.0):
    ds = _empty_daily_state()
    ds["last_outdoor_temp_f"] = temp
    return ds


# ---------------------------------------------------------------------------
# 1. Happy path: snapshot written with correct schema
# ---------------------------------------------------------------------------


def test_snapshot_written_correct_schema(tmp_path):
    log_path = str(tmp_path / "zone_temps.jsonl")
    ds = _daily_state_with_outdoor(48.0)

    result = write_zone_temp_snapshot(CLIMATE_STATE_FULL, ds, snapshot_log=log_path)

    assert result is True, "should return True when snapshot is written"

    lines = Path(log_path).read_text().strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["schema"] == "homeops.consumer.zone_temp_snapshot.v1"
    assert record["source"] == "consumer.v1"
    assert "ts" in record

    zones = record["data"]["zones"]
    assert set(zones.keys()) == {"floor_1", "floor_2", "floor_3"}

    assert zones["floor_1"] == {"current_temp": 68.0, "setpoint": 68, "hvac_action": "idle"}
    assert zones["floor_2"] == {"current_temp": 67.0, "setpoint": 68, "hvac_action": "heating"}
    assert zones["floor_3"] == {"current_temp": 70.0, "setpoint": 68, "hvac_action": "idle"}

    assert record["data"]["outdoor_temp_f"] == 48.0


def test_snapshot_null_outdoor_when_missing(tmp_path):
    """outdoor_temp_f should be null when daily_state has no outdoor reading."""
    log_path = str(tmp_path / "zone_temps.jsonl")
    ds = _empty_daily_state()  # last_outdoor_temp_f is None

    write_zone_temp_snapshot(CLIMATE_STATE_FULL, ds, snapshot_log=log_path)

    record = json.loads(Path(log_path).read_text().strip())
    assert record["data"]["outdoor_temp_f"] is None


# ---------------------------------------------------------------------------
# 2. Skip: no snapshot written when climate_state is empty
# ---------------------------------------------------------------------------


def test_snapshot_skipped_when_empty(tmp_path):
    log_path = str(tmp_path / "zone_temps.jsonl")
    ds = _daily_state_with_outdoor()

    result = write_zone_temp_snapshot({}, ds, snapshot_log=log_path)

    assert result is False, "should return False when climate_state is empty"
    assert not Path(log_path).exists(), "log file should not be created when nothing to write"


def test_snapshot_skipped_when_no_current_temp(tmp_path):
    """Zones without current_temp should be excluded; if all excluded, skip."""
    log_path = str(tmp_path / "zone_temps.jsonl")
    climate = {
        "climate.floor_1_thermostat": {
            "zone": "floor_1",
            "current_temp": None,  # no reading yet
            "setpoint": 68,
            "hvac_action": "idle",
        },
    }
    ds = _empty_daily_state()

    result = write_zone_temp_snapshot(climate, ds, snapshot_log=log_path)

    assert result is False
    assert not Path(log_path).exists()


# ---------------------------------------------------------------------------
# 3. Interval: snapshot only written when 5 min have elapsed (mock time)
# ---------------------------------------------------------------------------


def test_snapshot_interval_respected(tmp_path):
    """Simulate the interval check that main() performs around write_zone_temp_snapshot."""
    log_path = str(tmp_path / "zone_temps.jsonl")
    ds = _daily_state_with_outdoor()

    base_time = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)

    def _should_write(last_ts, now):
        if last_ts is None:
            return True
        return (now - last_ts).total_seconds() >= ZONE_TEMP_SNAPSHOT_INTERVAL_S

    # First call — last_snapshot_ts is None, should write.
    now = base_time
    last_ts = None
    assert _should_write(last_ts, now) is True
    write_zone_temp_snapshot(CLIMATE_STATE_FULL, ds, snapshot_log=log_path)
    last_ts = now

    # 60 seconds later — NOT yet 5 minutes, should NOT write again.
    now = base_time + timedelta(seconds=60)
    assert _should_write(last_ts, now) is False

    # 299 seconds later — still NOT 5 minutes.
    now = base_time + timedelta(seconds=299)
    assert _should_write(last_ts, now) is False

    # Exactly 300 seconds later — should write.
    now = base_time + timedelta(seconds=300)
    assert _should_write(last_ts, now) is True
    write_zone_temp_snapshot(CLIMATE_STATE_FULL, ds, snapshot_log=log_path)
    last_ts = now

    # Verify exactly 2 records were written.
    lines = Path(log_path).read_text().strip().splitlines()
    assert len(lines) == 2


def test_snapshot_interval_constant():
    """Sanity-check that the interval constant is 300 seconds (5 minutes)."""
    assert ZONE_TEMP_SNAPSHOT_INTERVAL_S == 300


# ---------------------------------------------------------------------------
# 4. Zone name deduplication — last zone wins if two entities share zone name
# ---------------------------------------------------------------------------


def test_snapshot_zone_key_is_zone_not_entity_id(tmp_path):
    """Keys in zones dict must be zone names (floor_1), not entity IDs."""
    log_path = str(tmp_path / "zone_temps.jsonl")
    climate = {
        "climate.floor_1_thermostat": {
            "zone": "floor_1",
            "current_temp": 69.5,
            "setpoint": 70,
            "hvac_action": "heating",
        },
    }
    ds = _empty_daily_state()

    write_zone_temp_snapshot(climate, ds, snapshot_log=log_path)

    record = json.loads(Path(log_path).read_text().strip())
    zones = record["data"]["zones"]
    assert "floor_1" in zones
    assert "climate.floor_1_thermostat" not in zones
