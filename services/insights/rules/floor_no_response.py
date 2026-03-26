"""
Rule: Floor Not Responding

Detects when a zone has been calling for heat for longer than a per-floor threshold
AND the internal temperature has not increased since the call started.

This replaces the earlier furnace-response-based approach, which was broken because
`binary_sensor.furnace_heating = OR(floor_1_call, floor_2_call, floor_3_call)` —
it fires immediately when any zone calls, not when the furnace actually heats.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Default thresholds derived from p95 call durations in consumer/events.jsonl (1,558 events).
# Set safely above p95 to avoid false positives from normal long calls.
_DEFAULT_THRESHOLDS_S: dict[str, float] = {
    "floor_1": 600.0,  # p95 = 9.3 min → threshold 10 min
    "floor_2": 900.0,  # p95 = 11.0 min → threshold 15 min
    "floor_3": 360.0,  # p95 = 6.0 min → threshold 6 min
}


@dataclass
class _ZoneState:
    start_ts: datetime
    start_temp: float | None
    max_temp_seen: float | None
    alert_sent: bool = False


class FloorNoResponseRule:
    """
    Tracks zone call start times and temperatures.

    A zone is "not responding" when:
      - It has been calling for >= threshold[zone] seconds, AND
      - The current temperature has not risen above the start temperature.

    If start_temp is unknown (None), no finding is emitted — we can't detect
    no-response without a baseline.

    Feed events in via:
      - on_floor_call_started(zone, started_at_ts, start_temp)
      - on_floor_call_ended(zone)
      - on_temp_updated(zone, current_temp)

    Then call check(now_ts) to get a list of fault findings.
    The alert fires at most once per call session (gated by alert_sent).
    """

    def __init__(self, thresholds_s: dict[str, float] | None = None) -> None:
        """
        Args:
            thresholds_s: Per-zone threshold in seconds. Defaults to
                          {"floor_1": 600, "floor_2": 900, "floor_3": 360}.
                          Missing zones fall back to the default value if present,
                          otherwise 600s.
        """
        self._thresholds: dict[str, float] = {**_DEFAULT_THRESHOLDS_S, **(thresholds_s or {})}
        self._zones: dict[str, _ZoneState] = {}

    def on_floor_call_started(
        self,
        zone: str,
        started_at_ts: datetime,
        start_temp: float | None,
    ) -> None:
        """Record that a zone has started calling for heat."""
        self._zones[zone] = _ZoneState(
            start_ts=started_at_ts,
            start_temp=start_temp,
            max_temp_seen=start_temp,
            alert_sent=False,
        )

    def on_floor_call_ended(self, zone: str) -> None:
        """Clear a zone when its heating call ends."""
        self._zones.pop(zone, None)

    def on_temp_updated(self, zone: str, current_temp: float) -> None:
        """
        Update the observed temperature for a zone that is currently calling.
        Call this whenever a thermostat_climate_updated event arrives for the zone.
        """
        state = self._zones.get(zone)
        if state is None:
            return  # zone not currently calling — ignore
        if state.max_temp_seen is None or current_temp > state.max_temp_seen:
            state.max_temp_seen = current_temp

    def check(self, now_ts: datetime) -> list[dict]:
        """
        Return findings for any zone that has been calling for >= threshold_s
        with no temperature increase.

        Args:
            now_ts: Current UTC datetime used to compute elapsed time.

        Returns:
            List of finding dicts (one per zone that crosses the threshold this call).
            Fields: zone, call_start_time, minutes_elapsed, start_temp, current_temp, severity.
        """
        findings = []
        for zone, state in self._zones.items():
            if state.alert_sent:
                continue
            if state.start_temp is None:
                # Can't determine non-response without a baseline temperature.
                continue
            threshold_s = self._thresholds.get(zone, 600.0)
            elapsed_s = (now_ts - state.start_ts).total_seconds()
            if elapsed_s < threshold_s:
                continue
            current_temp = state.max_temp_seen
            if current_temp is not None and current_temp > state.start_temp:
                # Temperature rose — furnace is working.
                continue
            state.alert_sent = True
            findings.append(
                {
                    "zone": zone,
                    "call_start_time": state.start_ts.astimezone(UTC).isoformat(),
                    "minutes_elapsed": round(elapsed_s / 60.0, 2),
                    "start_temp": state.start_temp,
                    "current_temp": current_temp,
                    "severity": "high",
                }
            )
        return findings
