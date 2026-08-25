"""Tests for staged Home Assistant mitigation event validation and translation.

Revision history:
  2026-08-25  Add applied/skipped payload coverage and rejection tests for the
              durable zone-stagger event.
"""

from __future__ import annotations

import pytest

from processors import MITIGATION_EVENT_TYPE, process_mitigation_event

TS = "2026-08-25T13:00:00+00:00"


def _event_data(**overrides):
    data = {
        "event_type": MITIGATION_EVENT_TYPE,
        "zone": "floor_2",
        "reason": "secondary_zone_call_during_furnace_warmup",
        "delay_minutes": 5,
        "trigger_event_id": "0123456789abcdef",
        "outcome": "applied",
    }
    data.update(overrides)
    return data


def test_applied_event_is_translated_to_common_derived_envelope() -> None:
    result = process_mitigation_event(MITIGATION_EVENT_TYPE, _event_data(), processing_ts=TS)

    assert result == {
        "schema": MITIGATION_EVENT_TYPE,
        "event_type": MITIGATION_EVENT_TYPE,
        "source": "consumer.v1",
        "ts": TS,
        "data": _event_data(),
    }


def test_skipped_event_is_preserved_with_reason_and_delay() -> None:
    result = process_mitigation_event(
        MITIGATION_EVENT_TYPE,
        _event_data(outcome="skipped", reason="resume_gate_failed", delay_minutes=0),
        processing_ts=TS,
    )

    assert result is not None
    assert result["data"]["outcome"] == "skipped"
    assert result["data"]["reason"] == "resume_gate_failed"
    assert result["data"]["delay_minutes"] == 0


def test_numeric_string_delay_is_normalized_for_template_output() -> None:
    result = process_mitigation_event(
        MITIGATION_EVENT_TYPE,
        _event_data(delay_minutes="5.5"),
        processing_ts=TS,
    )

    assert result is not None
    assert result["data"]["delay_minutes"] == 5.5


@pytest.mark.parametrize(
    "event_type,overrides",
    [
        ("other.event.v1", {}),
        (MITIGATION_EVENT_TYPE, {"zone": "attic"}),
        (MITIGATION_EVENT_TYPE, {"reason": ""}),
        (MITIGATION_EVENT_TYPE, {"delay_minutes": -1}),
        (MITIGATION_EVENT_TYPE, {"delay_minutes": "not-a-number"}),
        (MITIGATION_EVENT_TYPE, {"trigger_event_id": ""}),
        (MITIGATION_EVENT_TYPE, {"outcome": "aborted"}),
        (MITIGATION_EVENT_TYPE, {"event_type": "other.event.v1"}),
    ],
)
def test_invalid_event_payload_is_rejected(event_type, overrides) -> None:
    assert process_mitigation_event(event_type, _event_data(**overrides), processing_ts=TS) is None


def test_non_object_payload_is_rejected() -> None:
    assert process_mitigation_event(MITIGATION_EVENT_TYPE, None, processing_ts=TS) is None
