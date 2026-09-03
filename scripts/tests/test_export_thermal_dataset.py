"""Tests for deterministic point-in-time thermal training-row export."""

from __future__ import annotations

import json
from pathlib import Path

from export_thermal_dataset import (
    build_dataset,
    load_jsonl_events,
    load_optional_jsonl_events,
)
from thermal_experiment_marker import MarkerStore, record_message
from validate_thermal_dataset import validate_row


def _observer(
    ts: str,
    entity_id: str,
    new_state: str,
    *,
    attributes: dict | None = None,
) -> dict:
    return {
        "schema": "homeops.observer.state_changed.v1",
        "ts": ts,
        "data": {
            "entity_id": entity_id,
            "old_state": "unknown",
            "new_state": new_state,
            "attributes": attributes or {},
        },
    }


def _derived(schema: str, ts: str, data: dict) -> dict:
    return {"schema": schema, "ts": ts, "data": data}


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def _fixture_events() -> tuple[list[dict], list[dict]]:
    observer = [
        _observer("2024-01-15T09:55:00+00:00", "sensor.outdoor_temperature", "30"),
        _observer(
            "2024-01-15T09:59:00+00:00",
            "binary_sensor.floor_1_heating_call",
            "off",
        ),
        _observer(
            "2024-01-15T09:59:01+00:00",
            "binary_sensor.floor_2_heating_call",
            "off",
        ),
        _observer(
            "2024-01-15T09:59:02+00:00",
            "binary_sensor.floor_3_heating_call",
            "off",
        ),
        _observer(
            "2024-01-15T09:59:30+00:00",
            "binary_sensor.floor_1_heating_call",
            "on",
        ),
        _observer(
            "2024-01-15T09:59:45+00:00",
            "binary_sensor.floor_2_heating_call",
            "on",
        ),
        _observer(
            "2024-01-15T10:00:00+00:00",
            "climate.floor_2_thermostat",
            "heat",
            attributes={
                "current_temperature": 69.0,
                "temperature": 70.0,
                "hvac_action": "idle",
            },
        ),
        _observer(
            "2024-01-15T10:00:05+00:00",
            "climate.floor_2_thermostat",
            "heat",
            attributes={
                "current_temperature": 69.0,
                "temperature": 70.0,
                "hvac_action": "heating",
            },
        ),
        _observer(
            "2024-01-15T10:05:05+00:00",
            "climate.floor_2_thermostat",
            "heat",
            attributes={
                "current_temperature": 70.0,
                "temperature": 70.0,
                "hvac_action": "heating",
            },
        ),
        _observer(
            "2024-01-15T10:07:05+00:00",
            "climate.floor_2_thermostat",
            "heat",
            attributes={
                "current_temperature": 70.2,
                "temperature": 70.0,
                "hvac_action": "idle",
            },
        ),
        _observer(
            "2024-01-15T10:09:00+00:00",
            "binary_sensor.floor_1_cooling_call",
            "off",
        ),
        _observer(
            "2024-01-15T10:09:01+00:00",
            "binary_sensor.floor_2_cooling_call",
            "off",
        ),
        _observer(
            "2024-01-15T10:09:02+00:00",
            "binary_sensor.floor_3_cooling_call",
            "off",
        ),
        _observer(
            "2024-01-15T10:09:30+00:00",
            "binary_sensor.floor_2_cooling_call",
            "on",
        ),
        _observer(
            "2024-01-15T10:10:00+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 75.0,
                "temperature": 73.0,
                "hvac_action": "idle",
            },
        ),
        _observer(
            "2024-01-15T10:10:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 75.0,
                "temperature": 73.0,
                "hvac_action": "cooling",
            },
        ),
        _observer(
            "2024-01-15T10:18:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 73.0,
                "temperature": 73.0,
                "hvac_action": "cooling",
            },
        ),
        _observer(
            "2024-01-15T10:20:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 72.0,
                "temperature": 73.0,
                "hvac_action": "idle",
            },
        ),
        _observer(
            "2024-01-15T11:00:00+00:00",
            "climate.floor_3_thermostat",
            "heat",
            attributes={
                "current_temperature": 65.0,
                "temperature": 68.0,
                "hvac_action": "idle",
            },
        ),
        _observer(
            "2024-01-15T11:00:05+00:00",
            "climate.floor_3_thermostat",
            "heat",
            attributes={
                "current_temperature": 65.0,
                "temperature": 68.0,
                "hvac_action": "heating",
            },
        ),
    ]
    derived = [
        _derived(
            "homeops.consumer.zone_time_to_temp.v1",
            "2024-01-15T10:05:05+00:00",
            {
                "entity_id": "climate.floor_2_thermostat",
                "zone": "floor_2",
                "start_temp": 69.0,
                "setpoint": 70.0,
                "setpoint_delta": 1.0,
                "duration_s": 300,
                "end_temp": 70.0,
                "degrees_gained": 1.0,
                "outdoor_temp_f": 30.0,
                "other_zones_calling": ["floor_1"],
            },
        ),
        _derived(
            "homeops.consumer.thermostat_cooling_session_started.v1",
            "2024-01-15T10:10:05+00:00",
            {
                "entity_id": "climate.floor_1_thermostat",
                "zone": "floor_1",
                "started_at": "2024-01-15T10:10:05+00:00",
                "mode": "cool",
                "hvac_mode": "cool",
                "hvac_action": "cooling",
                "setpoint": 73.0,
                "current_temp": 75.0,
                "other_zones_calling": ["binary_sensor.floor_2_cooling_call"],
            },
        ),
        _derived(
            "homeops.consumer.zone_time_to_cool.v1",
            "2024-01-15T10:18:05+00:00",
            {
                "entity_id": "climate.floor_1_thermostat",
                "zone": "floor_1",
                "mode": "cool",
                "start_temp": 75.0,
                "setpoint": 73.0,
                "setpoint_delta": 2.0,
                "duration_s": 480,
                "end_temp": 73.0,
                "degrees_cooled": 2.0,
                "outdoor_temp_f": 30.0,
                "other_zones_calling": ["floor_2"],
            },
        ),
        _derived(
            "homeops.consumer.thermostat_cooling_session_ended.v1",
            "2024-01-15T10:20:05+00:00",
            {
                "entity_id": "climate.floor_1_thermostat",
                "zone": "floor_1",
                "ended_at": "2024-01-15T10:20:05+00:00",
                "mode": "cool",
                "hvac_mode": "cool",
                "hvac_action": "idle",
                "start_temp": 75.0,
                "setpoint": 73.0,
                "current_temp": 72.0,
                "duration_s": 1200,
                "target_reached": True,
                "other_zones_calling": ["floor_2"],
            },
        ),
    ]
    return observer, derived


def test_export_is_deterministic_and_keeps_future_out_of_features(tmp_path: Path):
    observer, derived = _fixture_events()
    observer_path = tmp_path / "observer.jsonl"
    derived_path = tmp_path / "derived.jsonl"
    _write_jsonl(observer_path, observer)
    _write_jsonl(derived_path, derived)

    observer_events, observer_stats = load_jsonl_events(observer_path, "observer")
    derived_events, derived_stats = load_jsonl_events(derived_path, "derived")
    first = build_dataset(observer_events, derived_events)
    second = build_dataset(observer_events, derived_events)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert observer_stats["lines"] == len(observer)
    assert derived_stats["lines"] == len(derived)

    heat = next(row for row in first if row["zone"] == "floor_2" and row["mode"] == "heat")
    assert heat["prediction_ts"] == "2024-01-15T10:00:05+00:00"
    assert heat["features"]["start_temp_f"] == 69.0
    assert heat["features"]["start_setpoint_f"] == 70.0
    assert heat["features"]["setpoint_delta_f"] == 1.0
    assert heat["features"]["other_zones_calling"] == ["floor_1"]
    assert heat["features"]["outdoor_temp_f"] == 30.0
    assert heat["labels"]["time_to_setpoint_s"] == 300.0
    assert heat["labels"]["zone_runtime_s"] == 420.0
    assert heat["label_status"] == {
        "time_to_setpoint": "eligible",
        "zone_runtime": "eligible",
    }
    assert "target_crossing_ts" not in heat["features"]
    assert "zone_runtime_s" not in heat["features"]

    cool = next(row for row in first if row["zone"] == "floor_1" and row["mode"] == "cool")
    assert cool["features"]["other_zones_calling"] == ["floor_2"]
    assert cool["labels"]["time_to_setpoint_s"] == 480.0
    assert cool["labels"]["zone_runtime_s"] == 600.0
    assert cool["label_status"]["time_to_setpoint"] == "eligible"
    assert any(
        reference["source"] == "derived"
        and reference["schema"] == "homeops.consumer.thermostat_cooling_session_started.v1"
        for reference in cool["provenance"]["source_events"]
    )
    assert any(
        reference["source"] == "observer" and reference.get("hvac_action") == "cooling"
        for reference in cool["provenance"]["source_events"]
    )


def test_open_session_is_explicitly_right_censored():
    observer, derived = _fixture_events()
    rows = build_dataset(
        [event for event in (load_jsonl_events_from_dicts(observer, "observer"))],
        load_jsonl_events_from_dicts(derived, "derived"),
    )

    floor_3 = next(row for row in rows if row["zone"] == "floor_3")
    assert floor_3["labels"] == {
        "time_to_setpoint_s": None,
        "zone_runtime_s": None,
    }
    assert floor_3["label_status"] == {
        "time_to_setpoint": "right_censored",
        "zone_runtime": "right_censored",
    }
    assert "missing_end_boundary" in floor_3["quality_flags"]


def test_outcome_without_start_boundary_is_retained_but_not_eligible():
    derived = [
        _derived(
            "homeops.consumer.zone_time_to_temp.v1",
            "2024-01-15T12:00:00+00:00",
            {
                "zone": "floor_3",
                "start_temp": 66.0,
                "setpoint": 70.0,
                "duration_s": 600,
                "end_temp": 70.0,
            },
        )
    ]
    rows = build_dataset([], load_jsonl_events_from_dicts(derived, "derived"))

    assert len(rows) == 1
    row = rows[0]
    assert row["prediction_ts"] is None
    assert row["labels"]["time_to_setpoint_s"] is None
    assert row["label_status"]["time_to_setpoint"] == "missing_start_boundary"
    assert row["label_status"]["zone_runtime"] == "missing_start_boundary"
    assert row["provenance"]["start_boundary"] == "missing"


def test_setpoint_change_after_crossing_does_not_invalidate_target_label():
    observer = [
        _observer(
            "2024-01-15T08:00:00+00:00",
            "climate.floor_1_thermostat",
            "heat",
            attributes={
                "current_temperature": 69.0,
                "temperature": 70.0,
                "hvac_action": "idle",
            },
        ),
        _observer(
            "2024-01-15T08:00:05+00:00",
            "climate.floor_1_thermostat",
            "heat",
            attributes={
                "current_temperature": 69.0,
                "temperature": 70.0,
                "hvac_action": "heating",
            },
        ),
        _observer(
            "2024-01-15T08:05:05+00:00",
            "climate.floor_1_thermostat",
            "heat",
            attributes={
                "current_temperature": 70.0,
                "temperature": 70.0,
                "hvac_action": "heating",
            },
        ),
        _observer(
            "2024-01-15T08:06:05+00:00",
            "climate.floor_1_thermostat",
            "heat",
            attributes={
                "current_temperature": 70.1,
                "temperature": 71.0,
                "hvac_action": "heating",
            },
        ),
        _observer(
            "2024-01-15T08:07:05+00:00",
            "climate.floor_1_thermostat",
            "heat",
            attributes={
                "current_temperature": 70.2,
                "temperature": 71.0,
                "hvac_action": "idle",
            },
        ),
    ]
    row = build_dataset(
        load_jsonl_events_from_dicts(observer, "observer"),
        [],
    )[0]

    assert row["labels"]["time_to_setpoint_s"] == 300.0
    assert row["label_status"]["time_to_setpoint"] == "eligible"
    assert "setpoint_changed_during_session" not in row["quality_flags"]


def test_whole_home_cooling_events_do_not_create_floor_rows():
    derived = [
        _derived(
            "homeops.consumer.cooling_session_started.v1",
            "2024-01-15T13:00:00+00:00",
            {"started_at": "2024-01-15T13:00:00+00:00"},
        ),
        _derived(
            "homeops.consumer.cooling_session_ended.v1",
            "2024-01-15T13:30:00+00:00",
            {"ended_at": "2024-01-15T13:30:00+00:00"},
        ),
    ]

    assert build_dataset([], load_jsonl_events_from_dicts(derived, "derived")) == []


def test_experiment_marker_sidecar_joins_to_overlapping_active_floor_session(tmp_path: Path):
    observer, derived = _fixture_events()
    store = MarkerStore(tmp_path / "markers.jsonl")
    start = record_message(
        "Starting a 30-minute cooling test on Floor 1.",
        store=store,
        source_message_id="marker-start",
        received_at="2024-01-15T10:09:30+00:00",
    )
    record_message(
        "Floor 1 test ended.",
        store=store,
        source_message_id="marker-end",
        received_at="2024-01-15T10:20:30+00:00",
    )
    marker_events = load_jsonl_events_from_dicts(
        store.read(),
        "experiment",
    )

    rows = build_dataset(
        load_jsonl_events_from_dicts(observer, "observer"),
        load_jsonl_events_from_dicts(derived, "derived"),
        experiment_events=marker_events,
    )
    cool = next(row for row in rows if row["zone"] == "floor_1" and row["mode"] == "cool")
    experiment = cool["provenance"]["experiment"]

    assert experiment["experiment_id"] == start["record"]["data"]["experiment_id"]
    assert experiment["configuration_id"] == "cool-s1-f1"
    assert experiment["status"] == "completed"
    assert experiment["active_zones"] == ["floor_1"]
    assert experiment["boundary"]["status"] == "clean"
    assert experiment["boundary"]["type"] == "session_start"
    assert experiment["boundary"]["role"] == "primary"
    assert len(cool["provenance"]["experiment_marker_events"]) == 2
    assert "experiment_id" not in cool["features"]


def test_experiment_marker_rebases_active_session_to_setpoint_change(tmp_path: Path):
    observer = [
        _observer("2024-01-15T09:55:00+00:00", "sensor.outdoor_temperature", "80"),
        _observer("2024-01-15T09:59:00+00:00", "binary_sensor.floor_1_cooling_call", "off"),
        _observer("2024-01-15T09:59:01+00:00", "binary_sensor.floor_2_cooling_call", "off"),
        _observer("2024-01-15T09:59:02+00:00", "binary_sensor.floor_3_cooling_call", "off"),
        _observer(
            "2024-01-15T10:00:00+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 74.0,
                "temperature": 74.0,
                "hvac_action": "idle",
            },
        ),
        _observer(
            "2024-01-15T10:00:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 74.0,
                "temperature": 74.0,
                "hvac_action": "cooling",
            },
        ),
        _observer(
            "2024-01-15T10:00:35+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 74.0,
                "temperature": 70.0,
                "hvac_action": "cooling",
            },
        ),
        _observer(
            "2024-01-15T10:05:35+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "temperature": 70.0,
                "hvac_action": "cooling",
            },
        ),
        _observer(
            "2024-01-15T10:06:35+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={
                "current_temperature": 70.0,
                "temperature": 70.0,
                "hvac_action": "idle",
            },
        ),
    ]
    derived = [
        _derived(
            "homeops.consumer.zone_time_to_cool.v1",
            "2024-01-15T10:05:35+00:00",
            {
                "zone": "floor_1",
                "mode": "cool",
                "start_temp": 74.0,
                "setpoint": 70.0,
                "duration_s": 300.0,
                "end_temp": 70.0,
            },
        )
    ]
    store = MarkerStore(tmp_path / "markers.jsonl")
    start = record_message(
        "Starting a 30-minute cooling test on Floor 1.",
        store=store,
        source_message_id="active-start",
        received_at="2024-01-15T10:00:30+00:00",
    )
    record_message(
        "Floor 1 test ended.",
        store=store,
        source_message_id="active-end",
        received_at="2024-01-15T10:10:30+00:00",
    )

    rows = build_dataset(
        load_jsonl_events_from_dicts(observer, "observer"),
        load_jsonl_events_from_dicts(derived, "derived"),
        experiment_events=load_jsonl_events_from_dicts(store.read(), "experiment"),
    )
    row = next(row for row in rows if row["zone"] == "floor_1" and row["mode"] == "cool")
    boundary = row["provenance"]["experiment"]["boundary"]

    assert (
        start["record"]["data"]["experiment_id"] == row["provenance"]["experiment"]["experiment_id"]
    )
    assert boundary["status"] == "clean"
    assert boundary["type"] == "setpoint_change"
    assert boundary["role"] == "primary"
    assert boundary["session_start_ts"] == "2024-01-15T10:00:05+00:00"
    assert boundary["intervention_start_ts"] == "2024-01-15T10:00:35+00:00"
    assert row["prediction_ts"] == "2024-01-15T10:00:35+00:00"
    assert row["active_end_ts"] == "2024-01-15T10:06:35+00:00"
    assert row["features"]["start_temp_f"] == 74.0
    assert row["features"]["start_setpoint_f"] == 70.0
    assert row["features"]["setpoint_delta_f"] == 4.0
    assert row["labels"]["time_to_setpoint_s"] == 300.0
    assert row["labels"]["zone_runtime_s"] == 360.0
    assert row["observations"]["observed_duration_s"] == 360.0
    assert "setpoint_changed_during_session" not in row["quality_flags"]
    assert "experiment_boundary_unverified" not in row["quality_flags"]
    assert validate_row(row) == []
    assert any(
        reference.get("source_message_id") == "active-start"
        for reference in row["provenance"]["experiment_marker_events"]
    )
    assert any(
        reference["timestamp"] == "2024-01-15T10:00:35+00:00"
        for reference in row["provenance"]["source_events"]
    )


def test_experiment_marker_groups_follow_on_sessions_without_rebasing_them(tmp_path: Path):
    observer = [
        _observer("2024-01-15T09:55:00+00:00", "sensor.outdoor_temperature", "80"),
        _observer("2024-01-15T09:59:00+00:00", "binary_sensor.floor_1_cooling_call", "off"),
        _observer("2024-01-15T09:59:01+00:00", "binary_sensor.floor_2_cooling_call", "off"),
        _observer("2024-01-15T09:59:02+00:00", "binary_sensor.floor_3_cooling_call", "off"),
        _observer(
            "2024-01-15T10:00:00+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 74.0, "temperature": 74.0, "hvac_action": "idle"},
        ),
        _observer(
            "2024-01-15T10:00:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 74.0, "temperature": 74.0, "hvac_action": "cooling"},
        ),
        _observer(
            "2024-01-15T10:00:35+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 74.0, "temperature": 70.0, "hvac_action": "cooling"},
        ),
        _observer(
            "2024-01-15T10:06:35+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 70.0, "temperature": 70.0, "hvac_action": "idle"},
        ),
        _observer(
            "2024-01-15T10:07:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 70.0, "temperature": 70.0, "hvac_action": "cooling"},
        ),
        _observer(
            "2024-01-15T10:08:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 70.0, "temperature": 70.0, "hvac_action": "idle"},
        ),
    ]
    store = MarkerStore(tmp_path / "markers.jsonl")
    record_message(
        "Starting a 30-minute cooling test on Floor 1.",
        store=store,
        source_message_id="group-start",
        received_at="2024-01-15T10:00:30+00:00",
    )
    record_message(
        "Floor 1 test ended.",
        store=store,
        source_message_id="group-end",
        received_at="2024-01-15T10:10:30+00:00",
    )

    rows = build_dataset(
        load_jsonl_events_from_dicts(observer, "observer"),
        [],
        experiment_events=load_jsonl_events_from_dicts(store.read(), "experiment"),
    )
    cool_rows = [row for row in rows if row["zone"] == "floor_1" and row["mode"] == "cool"]
    assert len(cool_rows) == 2
    primary = next(
        row for row in cool_rows if row["provenance"]["experiment"]["boundary"]["role"] == "primary"
    )
    follow_on = next(
        row
        for row in cool_rows
        if row["provenance"]["experiment"]["boundary"]["role"] == "continuation"
    )

    assert primary["prediction_ts"] == "2024-01-15T10:00:35+00:00"
    assert follow_on["prediction_ts"] == "2024-01-15T10:07:05+00:00"
    assert follow_on["provenance"]["experiment"]["boundary"]["status"] == "continuation"
    assert follow_on["provenance"]["experiment"]["boundary"]["primary_status"] == "ambiguous"
    assert follow_on["provenance"]["experiment"]["boundary"]["type"] == "follow_on_session"
    assert "experiment_follow_on_cycle" in follow_on["quality_flags"]
    assert (
        primary["provenance"]["experiment"]["experiment_id"]
        == follow_on["provenance"]["experiment"]["experiment_id"]
    )


def test_marker_inside_existing_session_without_transition_is_unverified(tmp_path: Path):
    observer = [
        _observer("2024-01-15T09:55:00+00:00", "sensor.outdoor_temperature", "80"),
        _observer("2024-01-15T09:59:00+00:00", "binary_sensor.floor_1_cooling_call", "off"),
        _observer("2024-01-15T09:59:01+00:00", "binary_sensor.floor_2_cooling_call", "off"),
        _observer("2024-01-15T09:59:02+00:00", "binary_sensor.floor_3_cooling_call", "off"),
        _observer(
            "2024-01-15T10:00:00+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 75.0, "temperature": 74.0, "hvac_action": "idle"},
        ),
        _observer(
            "2024-01-15T10:00:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 75.0, "temperature": 74.0, "hvac_action": "cooling"},
        ),
        _observer(
            "2024-01-15T10:10:05+00:00",
            "climate.floor_1_thermostat",
            "cool",
            attributes={"current_temperature": 74.5, "temperature": 74.0, "hvac_action": "idle"},
        ),
    ]
    store = MarkerStore(tmp_path / "markers.jsonl")
    record_message(
        "Starting a 30-minute cooling test on Floor 1.",
        store=store,
        source_message_id="unverified-start",
        received_at="2024-01-15T10:05:00+00:00",
    )
    record_message(
        "Floor 1 test ended.",
        store=store,
        source_message_id="unverified-end",
        received_at="2024-01-15T10:08:00+00:00",
    )

    row = build_dataset(
        load_jsonl_events_from_dicts(observer, "observer"),
        [],
        experiment_events=load_jsonl_events_from_dicts(store.read(), "experiment"),
    )[0]
    boundary = row["provenance"]["experiment"]["boundary"]

    assert boundary["status"] == "unverified"
    assert boundary["type"] == "marker_inside_session"
    assert boundary["role"] == "primary"
    assert "experiment_boundary_unverified" in row["quality_flags"]
    assert row["prediction_ts"] == "2024-01-15T10:00:05+00:00"


def test_experiment_markers_do_not_create_rows_for_passive_floors():
    observer, derived = _fixture_events()
    marker = {
        "schema": "homeops.thermal.experiment_marker.v1",
        "source": "telegram.experiment_marker",
        "ts": "2024-01-15T10:10:00+00:00",
        "event_id": "marker-only",
        "data": {
            "action": "start",
            "status": "active",
            "experiment_id": "cool-s1-f1-example",
            "configuration_id": "cool-s1-f1",
            "experiment_name": "Cooling — Floor 1 only",
            "test_id": "cool-s1-f1",
            "operation_type": "controlled_thermal_experiment",
            "mode": "cool",
            "active_zones": ["floor_1"],
            "suppressed_zones": ["floor_2", "floor_3"],
            "planned_duration_s": 1800,
            "target_f": None,
            "start_ts": "2024-01-15T10:10:00+00:00",
            "end_ts": None,
            "received_at": "2024-01-15T10:10:00+00:00",
            "confidence": "exact",
            "source_type": "live",
            "duration_defaulted": True,
            "abort_reason": None,
            "intervention": {
                "source": "operator",
                "mode": "cool",
                "active_zones": ["floor_1"],
                "suppressed_zones": ["floor_2", "floor_3"],
                "planned_duration_s": 1800,
            },
            "raw_text": "Starting a 30-minute cooling test on Floor 1.",
            "source_message_id": "marker-only",
        },
    }
    rows = build_dataset(
        load_jsonl_events_from_dicts(observer, "observer"),
        load_jsonl_events_from_dicts(derived, "derived"),
        experiment_events=load_jsonl_events_from_dicts([marker], "experiment"),
    )

    cool = next(row for row in rows if row["zone"] == "floor_1" and row["mode"] == "cool")
    assert cool["provenance"]["experiment"]["configuration_id"] == "cool-s1-f1"
    assert all(
        row["provenance"].get("experiment", {}).get("experiment_id") != "cool-s1-f1-example"
        for row in rows
        if row["zone"] != "floor_1"
    )


def test_missing_experiment_sidecar_is_optional_for_pre_marker_histories(tmp_path: Path):
    events, stats = load_optional_jsonl_events(tmp_path / "not-created.jsonl", "experiment")

    assert events == []
    assert stats["missing"] == 1
    assert stats["lines"] == 0


def load_jsonl_events_from_dicts(events: list[dict], source: str):
    """Create SourceEvents for unit fixtures without requiring a filesystem."""

    from export_thermal_dataset import (
        SourceEvent,
        _event_data,
        _event_timestamp,
    )

    return [
        SourceEvent(
            source=source,
            line=index,
            event=event,
            schema=event["schema"],
            data=_event_data(event),
            timestamp=_event_timestamp(event, event["schema"]),
        )
        for index, event in enumerate(events, start=1)
    ]
