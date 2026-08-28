"""Shared constants and entity mappings for the HomeOps consumer service.

Revision history:
  2026-08-27  Add the operator-created cooling-call entity map and aggregate AC
              entity constant so the consumer can add cooling state without
              changing the existing heating mappings.
  2026-08-24  Keep processor defaults available for direct callers while moving
              production warning thresholds to the validated insights rules.yaml file.
"""

from __future__ import annotations

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

# Floor cooling-call binary sensors → floor keys.  This is intentionally a
# separate map: the existing heating map and its consumers are a stable public
# contract and must not be made mode-generic as part of the cooling rollout.
_COOLING_FLOOR_ENTITIES: dict[str, str] = {
    "binary_sensor.floor_1_cooling_call": "floor_1",
    "binary_sensor.floor_2_cooling_call": "floor_2",
    "binary_sensor.floor_3_cooling_call": "floor_3",
}

AC_COOLING_ENTITY = "binary_sensor.ac_cooling"

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

# Per-floor fallback thresholds for direct processor callers (seconds). The
# consumer service overrides these from services/insights/rules.yaml at startup.
SLOW_TO_HEAT_THRESHOLDS_S: dict[str, int] = {
    "floor_1": 900,  # 15 min
    "floor_2": 1800,  # 30 min
    "floor_3": 600,  # 10 min
}

# Zone temperature snapshot settings
ZONE_TEMP_SNAPSHOT_INTERVAL_S: int = 300  # 5 minutes
ZONE_TEMP_SNAPSHOT_LOG: str = "state/consumer/zone_temps.jsonl"

# Outdoor temperature staleness threshold.
# A saved outdoor_temp_f reading is considered usable if it is no older than this.
# Outdoor temperature changes slowly; 3 hours is a reasonable window for seeding
# daily_state after a restart when a live reading has not yet arrived.
OUTDOOR_TEMP_STALE_S: int = 10800  # 3 hours
