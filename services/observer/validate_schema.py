#!/usr/bin/env python3
"""Validate JSONL output from the observer service.

Usage:
    python3 validate_schema.py observer.jsonl
    cat observer.jsonl | python3 validate_schema.py
"""

import json
import sys

KNOWN_ENTITIES = {
    "binary_sensor.furnace_heating",
    "binary_sensor.floor_1_heating_call",
    "binary_sensor.floor_2_heating_call",
    "binary_sensor.floor_3_heating_call",
    "sensor.outdoor_temperature",
}

BINARY_SENSOR_STATES = {"on", "off", "unavailable"}
OUTDOOR_TEMP_EXEMPT_STATES = {"unavailable", "unknown"}

REQUIRED_TOP_LEVEL = {"schema", "source", "ts", "data"}
REQUIRED_DATA_FIELDS = {"entity_id", "old_state", "new_state"}
EXPECTED_SCHEMA = "homeops.observer.state_changed.v1"
EXPECTED_SOURCE = "ha.websocket"


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
    if record.get("schema") != EXPECTED_SCHEMA:
        errors.append(
            f"Unexpected schema value: {record.get('schema')!r} (expected {EXPECTED_SCHEMA!r})"
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
