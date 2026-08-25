"""Tests for observer state and custom-event schema validation.

Revision history:
  2026-08-25  Cover valid and invalid automatic mitigation rollback payloads.
  2026-08-25  Cover the observer envelope for applied/skipped mitigation
              decisions alongside the existing state-change contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from validate_schema import validate_line  # noqa: E402

EVENT_TYPE = "homeops.mitigation.zone_stagger_applied.v1"
ROLLBACK_EVENT_TYPE = "homeops.mitigation.rollback.v1"


def _record(**event_overrides):
    event_data = {
        "event_type": EVENT_TYPE,
        "zone": "floor_2",
        "reason": "resume_gate_failed",
        "delay_minutes": 5,
        "trigger_event_id": "0123456789abcdef",
        "outcome": "skipped",
    }
    event_data.update(event_overrides)
    return {
        "schema": "homeops.observer.event.v1",
        "source": "ha.websocket",
        "ts": "2026-08-25T13:00:00+00:00",
        "data": {"event_type": EVENT_TYPE, "event_data": event_data},
    }


def _rollback_record(**event_overrides):
    event_data = {
        "event_type": ROLLBACK_EVENT_TYPE,
        "incident_id": "0123456789abcdef",
        "failed_attempts": 3,
        "reason": "short_cycle_after_three_mitigation_attempts",
        "trigger_event_id": "fedcba9876543210",
        "storm_window_started_at": "2026-08-25T12:00:00+00:00",
        "mitigation_enabled": False,
        "rollback_state": "rolled_back",
        "source_event_type": "homeops.mitigation.short_cycle_detected.v1",
    }
    event_data.update(event_overrides)
    return {
        "schema": "homeops.observer.event.v1",
        "source": "ha.websocket",
        "ts": "2026-08-25T13:00:00+00:00",
        "data": {"event_type": ROLLBACK_EVENT_TYPE, "event_data": event_data},
    }


def test_valid_mitigation_event_passes() -> None:
    assert validate_line(json.dumps(_record())) == []


def test_template_rendered_delay_string_is_accepted() -> None:
    assert validate_line(json.dumps(_record(delay_minutes="5.0"))) == []


def test_mitigation_event_requires_all_decision_fields() -> None:
    record = _record()
    del record["data"]["event_data"]["trigger_event_id"]

    errors = validate_line(json.dumps(record))

    assert any("trigger_event_id" in error for error in errors)


def test_state_change_contract_still_passes() -> None:
    record = {
        "schema": "homeops.observer.state_changed.v1",
        "source": "ha.websocket",
        "ts": "2026-08-25T13:00:00+00:00",
        "data": {
            "entity_id": "binary_sensor.floor_2_heating_call",
            "old_state": "off",
            "new_state": "on",
        },
    }

    assert validate_line(json.dumps(record)) == []


def test_valid_rollback_event_passes() -> None:
    assert validate_line(json.dumps(_rollback_record())) == []


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"failed_attempts": 2}, "failed_attempts"),
        ({"mitigation_enabled": True}, "mitigation_enabled"),
        ({"rollback_state": "active"}, "rollback_state"),
        ({"source_event_type": "other.event.v1"}, "source_event_type"),
        ({"storm_window_started_at": "not-a-date"}, "storm_window_started_at"),
    ],
)
def test_invalid_rollback_event_is_rejected(overrides, expected) -> None:
    errors = validate_line(json.dumps(_rollback_record(**overrides)))

    assert any(expected in error for error in errors)
