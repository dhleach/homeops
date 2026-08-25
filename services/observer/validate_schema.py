#!/usr/bin/env python3
"""Validate JSONL output from the observer service.

Usage:
    python3 validate_schema.py observer.jsonl
    cat observer.jsonl | python3 validate_schema.py

Revision history:
  2026-08-25  Validate the automatic mitigation rollback envelope and its
              fail-safe payload fields alongside staged zone decisions.
  2026-08-25  Validate the generic observer envelope and staged mitigation
              decision payload alongside state_changed records.
"""

import json
import math
import sys
from datetime import datetime

KNOWN_ENTITIES = {
    "binary_sensor.furnace_heating",
    "binary_sensor.floor_1_heating_call",
    "binary_sensor.floor_2_heating_call",
    "binary_sensor.floor_3_heating_call",
    "sensor.outdoor_temperature",
    "climate.floor_1_thermostat",
    "climate.floor_2_thermostat",
    "climate.floor_3_thermostat",
}

CLIMATE_ENTITIES = {
    "climate.floor_1_thermostat",
    "climate.floor_2_thermostat",
    "climate.floor_3_thermostat",
}

CLIMATE_HVAC_MODES = {"heat", "off", "cool"}

BINARY_SENSOR_STATES = {"on", "off", "unavailable"}
OUTDOOR_TEMP_EXEMPT_STATES = {"unavailable", "unknown"}

REQUIRED_TOP_LEVEL = {"schema", "source", "ts", "data"}
REQUIRED_DATA_FIELDS = {"entity_id", "old_state", "new_state"}
EXPECTED_SCHEMA = "homeops.observer.state_changed.v1"
EVENT_SCHEMA = "homeops.observer.event.v1"
MITIGATION_EVENT_TYPE = "homeops.mitigation.zone_stagger_applied.v1"
MITIGATION_ROLLBACK_EVENT_TYPE = "homeops.mitigation.rollback.v1"
MITIGATION_SHORT_CYCLE_EVENT_TYPE = "homeops.mitigation.short_cycle_detected.v1"
EXPECTED_SOURCE = "ha.websocket"
MITIGATION_ZONES = {"floor_1", "floor_2", "floor_3"}
MITIGATION_OUTCOMES = {"applied", "skipped"}
MITIGATION_DATA_FIELDS = {
    "event_type",
    "zone",
    "reason",
    "delay_minutes",
    "trigger_event_id",
    "outcome",
}


def validate_line(line: str) -> list[str]:
    """Validate a single JSONL line. Returns a list of error strings (empty = valid)."""
    errors = []

    # Must be valid JSON
    try:
        record = json.loads(line)
    except json.JSONDecodeError as e:
        return [f"Invalid JSON: {e}"]

    if not isinstance(record, dict):
        return ["Top-level value must be a JSON object"]

    # Required top-level fields
    missing = REQUIRED_TOP_LEVEL - record.keys()
    if missing:
        errors.append(f"Missing top-level fields: {sorted(missing)}")

    # schema value
    schema = record.get("schema")
    if schema not in {EXPECTED_SCHEMA, EVENT_SCHEMA}:
        errors.append(
            f"Unexpected schema value: {schema!r}"
            f" (expected {EXPECTED_SCHEMA!r} or {EVENT_SCHEMA!r})"
        )

    # source value
    if record.get("source") != EXPECTED_SOURCE:
        errors.append(
            f"Unexpected source value: {record.get('source')!r} (expected {EXPECTED_SOURCE!r})"
        )

    # ts must be a non-empty string
    ts = record.get("ts")
    if not isinstance(ts, str) or not ts:
        errors.append(f"Field 'ts' must be a non-empty string, got: {ts!r}")

    # data must be a dict
    data = record.get("data")
    if not isinstance(data, dict):
        errors.append(f"Field 'data' must be an object, got: {type(data).__name__}")
        return errors  # can't validate data sub-fields

    if schema == EVENT_SCHEMA:
        event_type = data.get("event_type")
        event_data = data.get("event_data")
        if not isinstance(event_data, dict):
            errors.append(
                f"Field 'data.event_data' must be an object, got: {type(event_data).__name__}"
            )
            return errors

        if event_type == MITIGATION_ROLLBACK_EVENT_TYPE:
            rollback_fields = {
                "event_type",
                "incident_id",
                "failed_attempts",
                "reason",
                "trigger_event_id",
                "storm_window_started_at",
                "mitigation_enabled",
                "rollback_state",
                "source_event_type",
            }
            missing_rollback_fields = rollback_fields - event_data.keys()
            if missing_rollback_fields:
                errors.append(f"Missing rollback event fields: {sorted(missing_rollback_fields)}")
            if event_data.get("event_type") != event_type:
                errors.append(
                    "Field 'data.event_data.event_type' must match the observer event type"
                )
            for field in ("incident_id", "reason", "trigger_event_id"):
                if (
                    not isinstance(event_data.get(field), str)
                    or not event_data.get(field, "").strip()
                ):
                    errors.append(f"Rollback field '{field}' must be a non-empty string")
            storm_started = event_data.get("storm_window_started_at")
            if not isinstance(storm_started, str) or not storm_started.strip():
                errors.append("Rollback field 'storm_window_started_at' must be a non-empty string")
            else:
                try:
                    datetime.fromisoformat(storm_started.replace("Z", "+00:00"))
                except ValueError:
                    errors.append("Rollback field 'storm_window_started_at' must be ISO 8601")
            failed_attempts = event_data.get("failed_attempts")
            if isinstance(failed_attempts, bool) or failed_attempts is None:
                errors.append("Rollback field 'failed_attempts' must be an integer >= 3")
            else:
                try:
                    attempts_value = float(failed_attempts)
                except (TypeError, ValueError):
                    errors.append("Rollback field 'failed_attempts' must be an integer >= 3")
                else:
                    if (
                        not math.isfinite(attempts_value)
                        or not attempts_value.is_integer()
                        or attempts_value < 3
                    ):
                        errors.append("Rollback field 'failed_attempts' must be an integer >= 3")
            if event_data.get("mitigation_enabled") is not False:
                errors.append("Rollback field 'mitigation_enabled' must be false")
            if event_data.get("rollback_state") != "rolled_back":
                errors.append("Rollback field 'rollback_state' must be 'rolled_back'")
            if event_data.get("source_event_type") != MITIGATION_SHORT_CYCLE_EVENT_TYPE:
                errors.append(
                    "Rollback field 'source_event_type' must be the short-cycle event type"
                )
            for field in ("short_cycle_duration_s", "short_cycle_threshold_s"):
                value = event_data.get(field)
                if value in (None, ""):
                    continue
                if isinstance(value, bool):
                    errors.append(f"Rollback field '{field}' must be a non-negative number")
                    continue
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    errors.append(f"Rollback field '{field}' must be a non-negative number")
                else:
                    if not math.isfinite(numeric_value) or numeric_value < 0:
                        errors.append(f"Rollback field '{field}' must be a non-negative number")
            return errors

        if event_type != MITIGATION_EVENT_TYPE:
            errors.append(
                f"Unexpected event type: {event_type!r} (expected one of "
                f"{MITIGATION_EVENT_TYPE!r}, {MITIGATION_ROLLBACK_EVENT_TYPE!r})"
            )
        missing_event_data = MITIGATION_DATA_FIELDS - event_data.keys()
        if missing_event_data:
            errors.append(f"Missing mitigation event fields: {sorted(missing_event_data)}")
        if event_data.get("event_type") != MITIGATION_EVENT_TYPE:
            errors.append("Field 'data.event_data.event_type' must match the observer event type")
        if event_data.get("zone") not in MITIGATION_ZONES:
            errors.append(
                f"Mitigation zone {event_data.get('zone')!r} is not one of"
                f" {sorted(MITIGATION_ZONES)}"
            )
        if (
            not isinstance(event_data.get("reason"), str)
            or not event_data.get("reason", "").strip()
        ):
            errors.append("Mitigation field 'reason' must be a non-empty string")
        if (
            not isinstance(event_data.get("trigger_event_id"), str)
            or not event_data.get("trigger_event_id", "").strip()
        ):
            errors.append("Mitigation field 'trigger_event_id' must be a non-empty string")
        delay_minutes = event_data.get("delay_minutes")
        if isinstance(delay_minutes, bool) or delay_minutes is None:
            errors.append("Mitigation field 'delay_minutes' must be numeric")
        else:
            try:
                delay_value = float(delay_minutes)
            except (TypeError, ValueError):
                errors.append("Mitigation field 'delay_minutes' must be numeric")
            else:
                if not math.isfinite(delay_value):
                    errors.append("Mitigation field 'delay_minutes' must be finite")
                elif delay_value < 0:
                    errors.append("Mitigation field 'delay_minutes' must be non-negative")
        if event_data.get("outcome") not in MITIGATION_OUTCOMES:
            errors.append(
                f"Mitigation outcome {event_data.get('outcome')!r} is not one of"
                f" {sorted(MITIGATION_OUTCOMES)}"
            )
        return errors

    # Required data fields
    missing_data = REQUIRED_DATA_FIELDS - data.keys()
    if missing_data:
        errors.append(f"Missing data fields: {sorted(missing_data)}")

    entity_id = data.get("entity_id")
    new_state = data.get("new_state")

    # entity_id check (warn only — does not count as error)
    if entity_id not in KNOWN_ENTITIES:
        print(
            f"  WARN  unknown entity_id: {entity_id!r} (not in known entity list)",
            file=sys.stderr,
        )

    # new_state must be non-null and non-empty
    if new_state is None or new_state == "":
        errors.append(f"Field 'data.new_state' must be non-null and non-empty, got: {new_state!r}")

    # Entity-specific state validation
    if isinstance(entity_id, str) and isinstance(new_state, str):
        if entity_id.startswith("binary_sensor."):
            if new_state not in BINARY_SENSOR_STATES:
                errors.append(
                    f"Binary sensor state {new_state!r} is not one of"
                    f" {sorted(BINARY_SENSOR_STATES)}"
                )
        elif entity_id == "sensor.outdoor_temperature":
            if new_state not in OUTDOOR_TEMP_EXEMPT_STATES:
                try:
                    float(new_state)
                except ValueError:
                    errors.append(
                        f"outdoor_temperature state {new_state!r} is not a float or exempt value"
                        f" {sorted(OUTDOOR_TEMP_EXEMPT_STATES)}"
                    )
        elif entity_id in CLIMATE_ENTITIES:
            if new_state not in CLIMATE_HVAC_MODES:
                errors.append(
                    f"Climate entity state {new_state!r} is not one of {sorted(CLIMATE_HVAC_MODES)}"
                )
            attributes = data.get("attributes") or {}
            if not isinstance(attributes.get("temperature"), (int, float)):
                errors.append(
                    f"Climate attributes missing numeric 'temperature',"
                    f" got: {attributes.get('temperature')!r}"
                )
            if not isinstance(attributes.get("current_temperature"), (int, float)):
                errors.append(
                    f"Climate attributes missing numeric 'current_temperature',"
                    f" got: {attributes.get('current_temperature')!r}"
                )

    return errors


def main() -> int:
    if len(sys.argv) > 1:
        path = sys.argv[1]
        try:
            fh = open(path, encoding="utf-8")
        except OSError as e:
            print(f"ERROR: Cannot open file {path!r}: {e}", file=sys.stderr)
            return 1
    else:
        fh = sys.stdin

    total = 0
    invalid = 0

    try:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            if not line:
                continue  # skip blank lines

            total += 1
            errors = validate_line(line)
            if errors:
                invalid += 1
                print(f"  FAIL  line {lineno}: {len(errors)} error(s)")
                for err in errors:
                    print(f"        - {err}")
    finally:
        if fh is not sys.stdin:
            fh.close()

    valid = total - invalid
    print(f"\nSummary: {total} lines | {valid} valid | {invalid} invalid")

    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
