"""
Unit tests for metrics.py — HvacMetrics gauge updates.

Tests cover update_from_event() dispatch for every supported schema,
plus the helper methods and reset_daily_runtimes().

The Prometheus HTTP server is NOT started in these tests — HvacMetrics.start()
is never called, so no port binding occurs.
"""

from __future__ import annotations

import pytest
from metrics import HvacMetrics

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def m() -> HvacMetrics:
    """Fresh HvacMetrics instance with a unique registry per test (via new instance)."""
    # Each instantiation creates new Gauge/Counter objects; prometheus_client reuses
    # names in the default registry so we use the same port-agnostic instance approach.
    return HvacMetrics(port=19999)  # port irrelevant — start() never called


def _gauge_value(gauge, **labels) -> float:
    """Read the current value of a labelled or unlabelled gauge."""
    if labels:
        return gauge.labels(**labels)._value.get()
    return gauge._value.get()


# ── furnace_heating_active ────────────────────────────────────────────────────


def test_furnace_active_on_session_started(m: HvacMetrics) -> None:
    m.update_from_event("homeops.consumer.heating_session_started.v1", {"started_at": "t"})
    assert _gauge_value(m.furnace_heating_active) == 1.0


def test_furnace_active_off_on_session_ended(m: HvacMetrics) -> None:
    m.update_from_event("homeops.consumer.heating_session_started.v1", {"started_at": "t"})
    m.update_from_event(
        "homeops.consumer.heating_session_ended.v1",
        {"ended_at": "t", "duration_s": 300, "outdoor_temp_f": 40.0},
    )
    assert _gauge_value(m.furnace_heating_active) == 0.0


# ── heating_session_duration_seconds ─────────────────────────────────────────


def test_session_duration_set_on_ended(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.heating_session_ended.v1",
        {"ended_at": "t", "duration_s": 450, "outdoor_temp_f": None},
    )
    assert _gauge_value(m.heating_session_duration_seconds) == 450.0


def test_session_duration_null_duration_no_update(m: HvacMetrics) -> None:
    """If duration_s is None (furnace was already on at startup), gauge stays 0."""
    m.update_from_event(
        "homeops.consumer.heating_session_ended.v1",
        {"ended_at": "t", "duration_s": None},
    )
    assert _gauge_value(m.heating_session_duration_seconds) == 0.0


# ── floor_temperature_fahrenheit ─────────────────────────────────────────────


def test_floor_temp_updated(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.thermostat_current_temp_updated.v1",
        {"floor": "floor_2", "temperature_f": 68.5},
    )
    assert _gauge_value(m.floor_temperature_fahrenheit, floor="floor_2") == 68.5


def test_floor_temp_all_three_floors(m: HvacMetrics) -> None:
    for floor, temp in [("floor_1", 70.0), ("floor_2", 65.0), ("floor_3", 72.5)]:
        m.update_from_event(
            "homeops.consumer.thermostat_current_temp_updated.v1",
            {"floor": floor, "temperature_f": temp},
        )
    assert _gauge_value(m.floor_temperature_fahrenheit, floor="floor_1") == 70.0
    assert _gauge_value(m.floor_temperature_fahrenheit, floor="floor_2") == 65.0
    assert _gauge_value(m.floor_temperature_fahrenheit, floor="floor_3") == 72.5


def test_floor_temp_missing_floor_no_error(m: HvacMetrics) -> None:
    """Events with missing floor key should not raise."""
    m.update_from_event(
        "homeops.consumer.thermostat_current_temp_updated.v1",
        {"temperature_f": 68.0},
    )


# ── outdoor_temperature_fahrenheit ───────────────────────────────────────────


def test_outdoor_temp_updated(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.outdoor_temp_updated.v1",
        {"temperature_f": 38.2, "entity_id": "sensor.outdoor_temperature"},
    )
    assert _gauge_value(m.outdoor_temperature_fahrenheit) == 38.2


def test_outdoor_temp_overwritten_by_newer_reading(m: HvacMetrics) -> None:
    m.update_from_event("homeops.consumer.outdoor_temp_updated.v1", {"temperature_f": 38.2})
    m.update_from_event("homeops.consumer.outdoor_temp_updated.v1", {"temperature_f": 41.0})
    assert _gauge_value(m.outdoor_temperature_fahrenheit) == 41.0


# ── floor_call_active ─────────────────────────────────────────────────────────


def test_floor_call_active_set_on_started(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.floor_call_started.v1",
        {"floor": "floor_2", "started_at": "t"},
    )
    assert _gauge_value(m.floor_call_active, floor="floor_2") == 1.0


def test_floor_call_active_cleared_on_ended(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.floor_call_started.v1",
        {"floor": "floor_2", "started_at": "t"},
    )
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_2", "ended_at": "t", "duration_s": 900},
    )
    assert _gauge_value(m.floor_call_active, floor="floor_2") == 0.0


def test_other_floors_unaffected_by_floor_call(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.floor_call_started.v1",
        {"floor": "floor_2", "started_at": "t"},
    )
    assert _gauge_value(m.floor_call_active, floor="floor_1") == 0.0
    assert _gauge_value(m.floor_call_active, floor="floor_3") == 0.0


# ── zone_runtime_today_seconds ────────────────────────────────────────────────


def test_runtime_accumulates_across_calls(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_2", "ended_at": "t", "duration_s": 600},
    )
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_2", "ended_at": "t", "duration_s": 300},
    )
    assert _gauge_value(m.zone_runtime_today_seconds, floor="floor_2") == 900.0


def test_runtime_independent_per_floor(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_1", "ended_at": "t", "duration_s": 400},
    )
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_3", "ended_at": "t", "duration_s": 200},
    )
    assert _gauge_value(m.zone_runtime_today_seconds, floor="floor_1") == 400.0
    assert _gauge_value(m.zone_runtime_today_seconds, floor="floor_2") == 0.0
    assert _gauge_value(m.zone_runtime_today_seconds, floor="floor_3") == 200.0


def test_runtime_synced_from_daily_summary(m: HvacMetrics) -> None:
    """floor_daily_summary.v1 is the authoritative source — overrides accumulated value."""
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_1", "ended_at": "t", "duration_s": 400},
    )
    m.update_from_event(
        "homeops.consumer.floor_daily_summary.v1",
        {"floor": "floor_1", "total_runtime_s": 3600, "date": "2026-01-01"},
    )
    assert _gauge_value(m.zone_runtime_today_seconds, floor="floor_1") == 3600.0


def test_reset_daily_runtimes(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_2", "ended_at": "t", "duration_s": 500},
    )
    m.reset_daily_runtimes()
    for floor in ["floor_1", "floor_2", "floor_3"]:
        assert _gauge_value(m.zone_runtime_today_seconds, floor=floor) == 0.0


def test_runtime_null_duration_no_accumulation(m: HvacMetrics) -> None:
    """floor_call_ended with duration_s=None should not change runtime."""
    m.update_from_event(
        "homeops.consumer.floor_call_ended.v1",
        {"floor": "floor_2", "ended_at": "t", "duration_s": None},
    )
    assert _gauge_value(m.zone_runtime_today_seconds, floor="floor_2") == 0.0


# ── floor_runtime_anomaly_total ───────────────────────────────────────────────


def test_anomaly_counter_increments(m: HvacMetrics) -> None:
    m.update_from_event(
        "homeops.consumer.floor_runtime_anomaly.v1",
        {"floor": "floor_2", "runtime_s": 5000},
    )
    assert m.floor_runtime_anomaly_total.labels(floor="floor_2")._value.get() == 1.0


def test_anomaly_counter_accumulates(m: HvacMetrics) -> None:
    for _ in range(3):
        m.update_from_event(
            "homeops.consumer.floor_runtime_anomaly.v1",
            {"floor": "floor_2", "runtime_s": 5000},
        )
    assert m.floor_runtime_anomaly_total.labels(floor="floor_2")._value.get() == 3.0


def test_anomaly_counter_independent_per_floor(m: HvacMetrics) -> None:
    m.update_from_event("homeops.consumer.floor_runtime_anomaly.v1", {"floor": "floor_2"})
    m.update_from_event("homeops.consumer.floor_runtime_anomaly.v1", {"floor": "floor_3"})
    assert m.floor_runtime_anomaly_total.labels(floor="floor_2")._value.get() == 1.0
    assert m.floor_runtime_anomaly_total.labels(floor="floor_3")._value.get() == 1.0
    assert m.floor_runtime_anomaly_total.labels(floor="floor_1")._value.get() == 0.0


# ── unknown schema is a no-op ─────────────────────────────────────────────────


def test_unknown_schema_no_error(m: HvacMetrics) -> None:
    """Unrecognised schemas should be silently ignored."""
    m.update_from_event("homeops.consumer.some_future_event.v1", {"data": "value"})
