"""Tests for additive cooling metrics, daily accounting, and reporting.

Revision history:
  2026-08-28  Add coverage for cooling metric dispatch, persisted gauge restore,
              mixed heat/cool summaries, and raw-event report reconstruction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from consumer import (
    _empty_daily_state,
    _playback_phase,
    _restore_cooling_metrics,
)
from metrics import HvacMetrics
from summary import compute_day_from_raw

from reporting import emit_daily_summary, emit_floor_daily_summaries, format_daily_summary_message
from state import ensure_daily_state

COOLING_FLOOR_1 = "binary_sensor.floor_1_cooling_call"
COOLING_FLOOR_2 = "binary_sensor.floor_2_cooling_call"
COOLING_FLOOR_3 = "binary_sensor.floor_3_cooling_call"
AC_COOLING = "binary_sensor.ac_cooling"
START = datetime(2024, 7, 15, 14, 0, tzinfo=UTC)


@pytest.fixture()
def metrics() -> HvacMetrics:
    return HvacMetrics(port=19999)


def _value(metric, **labels) -> float:
    if labels:
        return metric.labels(**labels)._value.get()
    return metric._value.get()


def _observer_line(entity_id: str, old_state: str, new_state: str, ts: datetime) -> str:
    return json.dumps(
        {
            "schema": "homeops.observer.state_changed.v1",
            "ts": ts.isoformat(),
            "data": {
                "entity_id": entity_id,
                "old_state": old_state,
                "new_state": new_state,
                "attributes": {},
            },
        }
    )


def test_cooling_metrics_track_active_runtime_and_counts(metrics: HvacMetrics) -> None:
    metrics.update_from_event(
        "homeops.consumer.cooling_call_started.v1",
        {"floor": "floor_1", "entity_id": COOLING_FLOOR_1},
    )
    metrics.update_from_event(
        "homeops.consumer.cooling_session_started.v1",
        {"entity_id": AC_COOLING},
    )

    assert _value(metrics.cooling_floor_call_active, floor="floor_1") == 1.0
    assert _value(metrics.ac_cooling_active) == 1.0

    metrics.update_from_event(
        "homeops.consumer.cooling_call_ended.v1",
        {"floor": "floor_1", "entity_id": COOLING_FLOOR_1, "duration_s": 900},
    )
    metrics.update_from_event(
        "homeops.consumer.cooling_session_ended.v1",
        {"entity_id": AC_COOLING, "duration_s": 1200},
    )

    assert _value(metrics.cooling_floor_call_active, floor="floor_1") == 0.0
    assert _value(metrics.cooling_zone_runtime_today_seconds, floor="floor_1") == 900.0
    assert _value(metrics.cooling_zone_call_count_today, floor="floor_1") == 1.0
    assert _value(metrics.ac_cooling_active) == 0.0
    assert _value(metrics.cooling_session_duration_seconds) == 1200.0
    assert _value(metrics.cooling_runtime_today_seconds) == 1200.0
    assert _value(metrics.cooling_session_count_today) == 1.0


def test_concurrent_cooling_floor_calls_are_independent(metrics: HvacMetrics) -> None:
    for floor, entity_id in (("floor_1", COOLING_FLOOR_1), ("floor_2", COOLING_FLOOR_2)):
        metrics.update_from_event(
            "homeops.consumer.cooling_call_started.v1",
            {"floor": floor, "entity_id": entity_id},
        )

    metrics.update_from_event(
        "homeops.consumer.cooling_call_ended.v1",
        {"floor": "floor_1", "entity_id": COOLING_FLOOR_1, "duration_s": 600},
    )

    assert _value(metrics.cooling_floor_call_active, floor="floor_1") == 0.0
    assert _value(metrics.cooling_floor_call_active, floor="floor_2") == 1.0
    assert _value(metrics.cooling_zone_runtime_today_seconds, floor="floor_1") == 600.0
    assert _value(metrics.cooling_zone_runtime_today_seconds, floor="floor_2") == 0.0


def test_null_cooling_duration_counts_session_without_fabricating_runtime(
    metrics: HvacMetrics,
) -> None:
    metrics.update_from_event(
        "homeops.consumer.cooling_call_ended.v1",
        {"floor": "floor_3", "entity_id": COOLING_FLOOR_3, "duration_s": None},
    )
    metrics.update_from_event(
        "homeops.consumer.cooling_session_ended.v1",
        {"entity_id": AC_COOLING, "duration_s": None},
    )

    assert _value(metrics.cooling_zone_call_count_today, floor="floor_3") == 1.0
    assert _value(metrics.cooling_zone_runtime_today_seconds, floor="floor_3") == 0.0
    assert _value(metrics.cooling_session_count_today) == 1.0
    assert _value(metrics.cooling_runtime_today_seconds) == 0.0
    assert _value(metrics.cooling_session_duration_seconds) == 0.0


def test_cooling_daily_gauges_restore_and_reset(metrics: HvacMetrics) -> None:
    metrics.restore_daily_cooling_state(
        runtime_s=3600,
        session_count=3,
        per_floor_runtime_s={"floor_1": 900, "floor_2": 1200},
        per_floor_call_count={"floor_1": 2, "floor_2": 1},
    )
    assert _value(metrics.cooling_runtime_today_seconds) == 3600.0
    assert _value(metrics.cooling_session_count_today) == 3.0
    assert _value(metrics.cooling_zone_runtime_today_seconds, floor="floor_1") == 900.0
    assert _value(metrics.cooling_zone_call_count_today, floor="floor_2") == 1.0

    metrics.reset_daily_runtimes()
    assert _value(metrics.cooling_runtime_today_seconds) == 0.0
    assert _value(metrics.cooling_session_count_today) == 0.0
    for floor in ("floor_1", "floor_2", "floor_3"):
        assert _value(metrics.cooling_zone_runtime_today_seconds, floor=floor) == 0.0
        assert _value(metrics.cooling_zone_call_count_today, floor=floor) == 0.0


def test_old_daily_state_is_backfilled_without_losing_heating_values() -> None:
    state = ensure_daily_state({"furnace_runtime_s": 1800, "session_count": 2})

    assert state["furnace_runtime_s"] == 1800
    assert state["session_count"] == 2
    assert state["cooling_runtime_s"] == 0
    assert state["cooling_session_count"] == 0
    assert state["per_floor_cooling_session_count"][COOLING_FLOOR_1] == 0


def test_restore_cooling_metrics_uses_persisted_active_boundaries(metrics: HvacMetrics) -> None:
    daily = _empty_daily_state()
    daily["cooling_runtime_s"] = 2400
    daily["cooling_session_count"] = 2
    daily["cooling_floor_runtime_s"] = {COOLING_FLOOR_1: 1200}
    daily["per_floor_cooling_session_count"] = {COOLING_FLOOR_1: 1}

    _restore_cooling_metrics(
        metrics,
        daily,
        {COOLING_FLOOR_1: START, COOLING_FLOOR_2: None, COOLING_FLOOR_3: None},
        START,
    )

    assert _value(metrics.ac_cooling_active) == 1.0
    assert _value(metrics.cooling_floor_call_active, floor="floor_1") == 1.0
    assert _value(metrics.cooling_floor_call_active, floor="floor_2") == 0.0
    assert _value(metrics.cooling_runtime_today_seconds) == 2400.0
    assert _value(metrics.cooling_session_count_today) == 2.0
    assert _value(metrics.cooling_zone_runtime_today_seconds, floor="floor_1") == 1200.0


def test_daily_summaries_include_cooling_without_changing_heating_fields() -> None:
    state = _empty_daily_state()
    state["furnace_runtime_s"] = 1800
    state["session_count"] = 2
    state["floor_runtime_s"] = {
        "binary_sensor.floor_1_heating_call": 600,
        "binary_sensor.floor_2_heating_call": 1200,
    }
    state["cooling_runtime_s"] = 2400
    state["cooling_session_count"] = 2
    state["cooling_floor_runtime_s"] = {
        COOLING_FLOOR_1: 900,
        COOLING_FLOOR_2: 1500,
    }
    state["per_floor_cooling_session_count"] = {
        COOLING_FLOOR_1: 1,
        COOLING_FLOOR_2: 2,
        COOLING_FLOOR_3: 0,
    }
    state["per_floor_max_cooling_call_s"] = {
        COOLING_FLOOR_1: 900,
        COOLING_FLOOR_2: 900,
        COOLING_FLOOR_3: None,
    }

    summary = emit_daily_summary(state, "2024-07-15")["data"]
    assert summary["total_furnace_runtime_s"] == 1800
    assert summary["session_count"] == 2
    assert summary["total_cooling_runtime_s"] == 2400
    assert summary["cooling_session_count"] == 2
    assert summary["per_floor_cooling_runtime_s"] == {
        "floor_1": 900,
        "floor_2": 1500,
        "floor_3": 0,
    }
    assert summary["per_floor_cooling_session_count"] == {
        "floor_1": 1,
        "floor_2": 2,
        "floor_3": 0,
    }

    floor_summaries = emit_floor_daily_summaries(state, "2024-07-15")
    by_floor = {event["data"]["floor"]: event["data"] for event in floor_summaries}
    assert by_floor["floor_1"]["cooling_total_calls"] == 1
    assert by_floor["floor_1"]["cooling_total_runtime_s"] == 900
    assert by_floor["floor_1"]["cooling_avg_duration_s"] == 900.0
    assert by_floor["floor_2"]["cooling_total_calls"] == 2
    assert by_floor["floor_2"]["cooling_max_duration_s"] == 900


def test_daily_message_reports_inferred_cooling_demand() -> None:
    state = _empty_daily_state()
    state["cooling_runtime_s"] = 1800
    state["cooling_session_count"] = 2
    state["cooling_floor_runtime_s"] = {COOLING_FLOOR_1: 900}
    state["per_floor_cooling_session_count"] = {COOLING_FLOOR_1: 1}

    message = format_daily_summary_message(emit_daily_summary(state, "2024-07-15")["data"])

    assert "❄️ Cooling: 2 inferred AC sessions, 0h 30m total" in message
    assert "Floor 1 cooling: 1 calls, 15m avg" in message
    assert "inferred thermostat demand; not compressor telemetry" in message


def test_playback_accumulates_cooling_daily_state(tmp_path: Path) -> None:
    end_floor = START + timedelta(minutes=15)
    end_ac = START + timedelta(minutes=20)
    observer = tmp_path / "observer.jsonl"
    derived = tmp_path / "derived.jsonl"
    observer.write_text(
        "\n".join(
            [
                _observer_line(COOLING_FLOOR_1, "off", "on", START),
                _observer_line(AC_COOLING, "off", "on", START),
                _observer_line(COOLING_FLOOR_1, "on", "off", end_floor),
                _observer_line(AC_COOLING, "on", "off", end_ac),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    from rules.floor_no_response import FloorNoResponseRule
    from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule

    result = _playback_phase(
        str(observer),
        START.isoformat(),
        derived_log=str(derived),
        floor_on_since={},
        furnace_on_since=None,
        climate_state={},
        daily_state=_empty_daily_state(),
        floor_2_warn_sent=False,
        fresh_restart=False,
        current_date="2024-07-15",
        floor_entities={},
        floor_no_response_rule=FloorNoResponseRule(),
        furnace_session_anomaly_rule=FurnaceSessionAnomalyRule({}),
        telegram_bot_token="",
        telegram_chat_id="",
        cooling_floor_on_since={
            COOLING_FLOOR_1: None,
            COOLING_FLOOR_2: None,
            COOLING_FLOOR_3: None,
        },
        ac_cooling_on_since=None,
    )

    daily = result["daily_state"]
    assert daily["cooling_floor_runtime_s"][COOLING_FLOOR_1] == 900
    assert daily["per_floor_cooling_session_count"][COOLING_FLOOR_1] == 1
    assert daily["cooling_runtime_s"] == 1200
    assert daily["cooling_session_count"] == 1


def test_raw_day_reconstruction_includes_cooling_events(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    ts = START.isoformat()
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema": "homeops.consumer.cooling_call_ended.v1",
                        "ts": ts,
                        "data": {
                            "entity_id": COOLING_FLOOR_2,
                            "duration_s": 600,
                        },
                    }
                ),
                json.dumps(
                    {
                        "schema": "homeops.consumer.cooling_session_ended.v1",
                        "ts": ts,
                        "data": {"entity_id": AC_COOLING, "duration_s": 1200},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    data = compute_day_from_raw(str(events), "2024-07-15")

    assert data["total_cooling_runtime_s"] == 1200
    assert data["cooling_session_count"] == 1
    assert data["per_floor_cooling_runtime_s"]["floor_2"] == 600
    assert data["per_floor_cooling_session_count"]["floor_2"] == 1
