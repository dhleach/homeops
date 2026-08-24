"""Ensure every insight/alert rule honors its shared enabled setting."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[2] / "consumer"))

from rules.efficiency_degradation import EfficiencyDegradationRule  # noqa: E402
from rules.floor_no_response import FloorNoResponseRule  # noqa: E402
from rules.floor_runtime_anomaly import FloorRuntimeAnomalyRule  # noqa: E402
from rules.furnace_session_anomaly import FurnaceSessionAnomalyRule  # noqa: E402
from rules.heating_efficiency import HeatingEfficiencyRule  # noqa: E402
from rules.time_of_day_pattern import TimeOfDayPatternRule  # noqa: E402

from alerts import (  # noqa: E402
    check_floor_2_escalation,
    check_floor_2_warning,
    check_observer_silence,
)
from processors import process_climate_event  # noqa: E402

BASE = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)


def test_floor_runtime_anomaly_disabled():
    assert (
        FloorRuntimeAnomalyRule([], enabled=False).check_daily_runtime(
            "floor_1", 9999, "2026-01-01"
        )
        == []
    )


def test_floor_no_response_disabled():
    rule = FloorNoResponseRule(thresholds_s={"floor_1": 1}, enabled=False)
    rule.on_floor_call_started("floor_1", BASE, 68.0)

    assert rule.check(BASE + timedelta(minutes=5)) == []


def test_furnace_session_anomaly_disabled():
    assert (
        FurnaceSessionAnomalyRule(enabled=False).check_session("floor_1", 1, BASE.isoformat()) == []
    )


def test_insight_rules_disabled():
    assert HeatingEfficiencyRule([], enabled=False).check() == []
    assert EfficiencyDegradationRule([], enabled=False).check() == []
    assert TimeOfDayPatternRule([], enabled=False).check([]) == []


def test_alert_helpers_disabled():
    floor_on_since = {"binary_sensor.floor_2_heating_call": BASE}

    warning, sent = check_floor_2_warning(
        floor_on_since,
        False,
        60,
        BASE + timedelta(minutes=5),
        enabled=False,
    )

    assert warning is None
    assert sent is False
    assert check_floor_2_escalation(10, 60, enabled=False) is None
    silence, silence_sent = check_observer_silence(
        BASE,
        False,
        60,
        BASE + timedelta(minutes=5),
        enabled=False,
    )
    assert silence is None
    assert silence_sent is False


def test_slow_to_heat_processor_disabled():
    state = {
        "climate.floor_1_thermostat": {
            "setpoint": 72.0,
            "current_temp": 68.0,
            "hvac_action": "heating",
            "hvac_mode": "heat",
            "heating_start_temp": 68.0,
            "heating_start_ts": BASE,
            "setpoint_reached_ts": None,
            "session_temps": [],
        }
    }
    attrs = {"temperature": 72.0, "current_temperature": 68.0, "hvac_action": "heating"}

    events, _ = process_climate_event(
        "climate.floor_1_thermostat",
        attrs,
        (BASE + timedelta(minutes=5)).isoformat(),
        state,
        "heat",
        slow_to_heat_thresholds_s={"floor_1": 60},
        slow_to_heat_enabled=False,
    )

    assert not any(event["schema"].endswith("zone_slow_to_heat_warning.v1") for event in events)
