"""Tests for Home Assistant event-to-observer JSONL conversion.

Revision history:
  2026-08-25  Cover automatic mitigation rollback-event wrapping alongside the
              existing staged decision event.
  2026-08-25  Cover state-change compatibility, mitigation-event wrapping, and
              filtering of unrelated Home Assistant event types.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from observer import _build_observer_record  # noqa: E402

TS = "2026-08-25T13:00:00+00:00"
MITIGATION_EVENT = "homeops.mitigation.zone_stagger_applied.v1"
ROLLBACK_EVENT = "homeops.mitigation.rollback.v1"


def test_state_changed_record_keeps_existing_shape_and_watch_filter() -> None:
    event = {
        "event_type": "state_changed",
        "data": {
            "entity_id": "binary_sensor.floor_2_heating_call",
            "old_state": {"state": "off"},
            "new_state": {"state": "on", "attributes": {"friendly_name": "Floor 2"}},
        },
    }

    record = _build_observer_record(event, {"binary_sensor.floor_2_heating_call"}, timestamp=TS)

    assert record == {
        "schema": "homeops.observer.state_changed.v1",
        "source": "ha.websocket",
        "ts": TS,
        "data": {
            "entity_id": "binary_sensor.floor_2_heating_call",
            "old_state": "off",
            "new_state": "on",
            "attributes": {"friendly_name": "Floor 2"},
        },
    }
    assert (
        _build_observer_record(event, {"binary_sensor.floor_1_heating_call"}, timestamp=TS) is None
    )


def test_mitigation_event_is_wrapped_without_entity_filtering() -> None:
    event_data = {
        "event_type": MITIGATION_EVENT,
        "zone": "floor_2",
        "reason": "secondary_zone_call_during_furnace_warmup",
        "delay_minutes": 5,
        "trigger_event_id": "0123456789abcdef",
        "outcome": "applied",
    }
    event = {
        "event_type": MITIGATION_EVENT,
        "data": event_data,
        "context": {"id": "observer-context-id"},
    }

    record = _build_observer_record(event, set(), timestamp=TS)

    assert record == {
        "schema": "homeops.observer.event.v1",
        "source": "ha.websocket",
        "ts": TS,
        "data": {
            "event_type": MITIGATION_EVENT,
            "event_data": event_data,
            "context_id": "observer-context-id",
        },
    }


def test_unrecognized_event_is_not_emitted() -> None:
    assert _build_observer_record({"event_type": "some_other_event", "data": {}}, set()) is None


def test_rollback_event_is_wrapped_without_entity_filtering() -> None:
    event_data = {
        "event_type": ROLLBACK_EVENT,
        "incident_id": "0123456789abcdef",
        "failed_attempts": 3,
        "reason": "short_cycle_after_three_mitigation_attempts",
        "trigger_event_id": "fedcba9876543210",
        "storm_window_started_at": "2026-08-25T12:00:00+00:00",
        "mitigation_enabled": False,
        "rollback_state": "rolled_back",
        "source_event_type": "homeops.mitigation.short_cycle_detected.v1",
    }
    event = {
        "event_type": ROLLBACK_EVENT,
        "data": event_data,
        "context": {"id": "rollback-context-id"},
    }

    record = _build_observer_record(event, {"climate.floor_2_thermostat"}, timestamp=TS)

    assert record == {
        "schema": "homeops.observer.event.v1",
        "source": "ha.websocket",
        "ts": TS,
        "data": {
            "event_type": ROLLBACK_EVENT,
            "event_data": event_data,
            "context_id": "rollback-context-id",
        },
    }
