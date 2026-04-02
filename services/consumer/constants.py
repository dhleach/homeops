"""Shared constants and entity mappings for the HomeOps consumer service."""

from __future__ import annotations

import os
from pathlib import Path

# State persistence
STATE_FILE: Path = Path("state/consumer/state.json")

# Floor heating-call binary sensors → floor keys
_FLOOR_ENTITIES: dict[str, str] = {
    "binary_sensor.floor_1_heating_call": "floor_1",
    "binary_sensor.floor_2_heating_call": "floor_2",
    "binary_sensor.floor_3_heating_call": "floor_3",
}

_ZONE_TO_FLOOR_ENTITY: dict[str, str] = {v: k for k, v in _FLOOR_ENTITIES.items()}

_ZONE_TO_CLIMATE_ENTITY: dict[str, str] = {
    "floor_1": "climate.floor_1_thermostat",
    "floor_2": "climate.floor_2_thermostat",
    "floor_3": "climate.floor_3_thermostat",
}

CLIMATE_ENTITIES: dict[str, str] = {
    "climate.floor_1_thermostat": "floor_1",
    "climate.floor_2_thermostat": "floor_2",
    "climate.floor_3_thermostat": "floor_3",
}

# Per-floor thresholds for the slow-to-heat warning (seconds).
# Overridable via env vars: SLOW_TO_HEAT_THRESHOLD_FLOOR1_S / FLOOR2_S / FLOOR3_S.
SLOW_TO_HEAT_THRESHOLDS_S: dict[str, int] = {
    "floor_1": int(os.environ.get("SLOW_TO_HEAT_THRESHOLD_FLOOR1_S", "900")),  # 15 min
    "floor_2": int(os.environ.get("SLOW_TO_HEAT_THRESHOLD_FLOOR2_S", "1800")),  # 30 min
    "floor_3": int(os.environ.get("SLOW_TO_HEAT_THRESHOLD_FLOOR3_S", "600")),  # 10 min
}

# Zone temperature snapshot settings
ZONE_TEMP_SNAPSHOT_INTERVAL_S: int = 300  # 5 minutes
ZONE_TEMP_SNAPSHOT_LOG: str = "state/consumer/zone_temps.jsonl"

# Outdoor temperature staleness threshold.
# A saved outdoor_temp_f reading is considered usable if it is no older than this.
# Outdoor temperature changes slowly; 3 hours is a reasonable window for seeding
# daily_state after a restart when a live reading has not yet arrived.
OUTDOOR_TEMP_STALE_S: int = 10800  # 3 hours
