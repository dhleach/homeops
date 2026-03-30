"""Shared constants and entity mappings for the HomeOps consumer service."""

import os
from pathlib import Path

# State persistence
STATE_FILE = Path("state/consumer/state.json")

# Floor heating-call binary sensors → floor keys
_FLOOR_ENTITIES = {
    "binary_sensor.floor_1_heating_call": "floor_1",
    "binary_sensor.floor_2_heating_call": "floor_2",
    "binary_sensor.floor_3_heating_call": "floor_3",
}

_ZONE_TO_FLOOR_ENTITY = {v: k for k, v in _FLOOR_ENTITIES.items()}

_ZONE_TO_CLIMATE_ENTITY = {
    "floor_1": "climate.floor_1_thermostat",
    "floor_2": "climate.floor_2_thermostat",
    "floor_3": "climate.floor_3_thermostat",
}

CLIMATE_ENTITIES = {
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
ZONE_TEMP_SNAPSHOT_INTERVAL_S = 300  # 5 minutes
ZONE_TEMP_SNAPSHOT_LOG = "state/consumer/zone_temps.jsonl"
