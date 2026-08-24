"""Tests for outdoor-temperature storm insight detection."""

from __future__ import annotations

from rules.outdoor_temperature_storm import OutdoorTemperatureStormRule


def _outdoor(ts: str, temperature: float) -> dict:
    return {
        "schema": "homeops.consumer.outdoor_temp_updated.v1",
        "ts": f"2026-01-01T{ts}:00+00:00",
        "data": {"temperature_f": temperature},
    }


def _session(ts: str, duration_s: int = 100) -> dict:
    date = "2025-12-31" if ts.startswith("23:") else "2026-01-01"
    return {
        "schema": "homeops.consumer.heating_session_ended.v1",
        "ts": f"{date}T{ts}:00+00:00",
        "data": {"duration_s": duration_s},
    }


def _history(current_runtime: int = 200, previous_runtime: int = 200) -> list[dict]:
    return [
        _session("23:15", previous_runtime // 2),
        _session("23:45", previous_runtime // 2),
        _outdoor("00:00", 50),
        _session("00:15", current_runtime // 2),
        _outdoor("00:30", 45),
        _session("00:45", current_runtime // 2),
        _outdoor("01:00", 40),
    ]


class TestOutdoorTemperatureStormRule:
    def test_detects_drop_with_stable_runtime(self):
        findings = OutdoorTemperatureStormRule(_history()).check()

        assert len(findings) == 1
        data = findings[0]["data"]
        assert findings[0]["schema"] == "homeops.insights.outdoor_temperature_storm.v1"
        assert data["outdoor_drop_f"] == 10.0
        assert data["reading_count"] == 3
        assert data["runtime_current_s"] == 200
        assert data["runtime_previous_s"] == 200
        assert data["runtime_change_ratio"] == 0.0

    def test_disabled_rule_never_fires(self):
        assert OutdoorTemperatureStormRule(_history(), enabled=False).check() == []

    def test_requires_runtime_in_both_comparison_windows(self):
        history = [_outdoor("00:00", 50), _outdoor("00:30", 45), _outdoor("01:00", 40)]

        assert OutdoorTemperatureStormRule(history).check() == []

    def test_runtime_change_suppresses_finding(self):
        assert OutdoorTemperatureStormRule(_history(current_runtime=500)).check() == []

    def test_requires_configured_reading_count(self):
        assert OutdoorTemperatureStormRule(_history(), storm_count=4).check() == []

    def test_malformed_events_are_ignored(self):
        history = _history() + [
            {
                "schema": "homeops.consumer.outdoor_temp_updated.v1",
                "data": {"temperature_f": "bad"},
            },
            {
                "schema": "homeops.consumer.heating_session_ended.v1",
                "data": {"duration_s": "bad"},
            },
        ]

        assert len(OutdoorTemperatureStormRule(history).check()) == 1
