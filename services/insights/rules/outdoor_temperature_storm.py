"""Detect abrupt outdoor-temperature drops without a matching runtime change.

This is a read-only insight rule.  It uses the existing outdoor-temperature and
completed-furnace-session events, so it can be evaluated by the HVAC context
builder without adding a thermostat write path.

Revision history:
  2026-08-24  Added the storm insight so storm_count, storm_window_hours, and
              the outdoor-drop/runtime-change thresholds have a real consumer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

_OUTDOOR_SCHEMA = "homeops.consumer.outdoor_temp_updated.v1"
_SESSION_SCHEMA = "homeops.consumer.heating_session_ended.v1"
_STORM_SCHEMA = "homeops.insights.outdoor_temperature_storm.v1"


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp into a UTC-aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class OutdoorTemperatureStormRule:
    """Find rapid outdoor cooling while furnace runtime stays approximately flat.

    A candidate requires ``storm_count`` valid outdoor readings in the configured
    window and a drop at least ``outdoor_drop_f`` from the first to last reading.
    To avoid turning a normal heating response into a weather alert, the rule
    compares completed furnace runtime in the candidate window with the prior
    equal-sized window and only emits when that change is within
    ``runtime_change_ratio``.
    """

    def __init__(
        self,
        history: list[dict[str, Any]],
        storm_count: int = 3,
        storm_window_hours: float = 1.0,
        outdoor_drop_f: float = 10.0,
        runtime_change_ratio: float = 0.1,
        enabled: bool = True,
    ) -> None:
        self._history = history or []
        self._storm_count = storm_count
        self._window = timedelta(hours=storm_window_hours)
        self._outdoor_drop_f = outdoor_drop_f
        self._runtime_change_ratio = runtime_change_ratio
        self._enabled = enabled

    def check(self) -> list[dict[str, Any]]:
        """Return at most one current storm finding, or an empty list."""
        if not self._enabled:
            return []

        readings = self._outdoor_readings()
        if len(readings) < self._storm_count:
            return []

        for end_ts, _end_temp in reversed(readings):
            start_ts = end_ts - self._window
            window_readings = [
                (timestamp, temp) for timestamp, temp in readings if start_ts <= timestamp <= end_ts
            ]
            if len(window_readings) < self._storm_count:
                continue

            first_ts, first_temp = window_readings[0]
            _last_ts, last_temp = window_readings[-1]
            drop_f = first_temp - last_temp
            if drop_f < self._outdoor_drop_f:
                continue

            current_runtime_s, current_count = self._runtime_in_window(start_ts, end_ts)
            previous_runtime_s, previous_count = self._runtime_in_window(
                start_ts - self._window, start_ts
            )
            if not current_count or not previous_count:
                # Runtime stability cannot be established without both windows.
                continue

            runtime_change_ratio = abs(current_runtime_s - previous_runtime_s) / max(
                abs(previous_runtime_s), 1.0
            )
            if runtime_change_ratio > self._runtime_change_ratio:
                continue

            return [
                {
                    "schema": _STORM_SCHEMA,
                    "source": "insights.outdoor_temperature_storm.v1",
                    "ts": datetime.now(UTC).isoformat(),
                    "data": {
                        "window_start": first_ts.isoformat(),
                        "window_end": end_ts.isoformat(),
                        "outdoor_temp_start_f": round(first_temp, 2),
                        "outdoor_temp_end_f": round(last_temp, 2),
                        "outdoor_drop_f": round(drop_f, 2),
                        "threshold_drop_f": self._outdoor_drop_f,
                        "reading_count": len(window_readings),
                        "storm_count": self._storm_count,
                        "storm_window_hours": self._window.total_seconds() / 3600,
                        "runtime_current_s": round(current_runtime_s, 2),
                        "runtime_previous_s": round(previous_runtime_s, 2),
                        "runtime_change_ratio": round(runtime_change_ratio, 4),
                        "runtime_change_threshold": self._runtime_change_ratio,
                    },
                }
            ]

        return []

    def _outdoor_readings(self) -> list[tuple[datetime, float]]:
        """Return valid outdoor readings sorted oldest first."""
        readings: list[tuple[datetime, float]] = []
        for event in self._history:
            if event.get("schema") != _OUTDOOR_SCHEMA:
                continue
            data = event.get("data") or {}
            timestamp = _parse_timestamp(data.get("timestamp") or event.get("ts"))
            temperature = data.get("temperature_f")
            if timestamp is None or isinstance(temperature, bool):
                continue
            try:
                readings.append((timestamp, float(temperature)))
            except (TypeError, ValueError):
                continue
        return sorted(readings)

    def _runtime_in_window(self, start: datetime, end: datetime) -> tuple[float, int]:
        """Return completed furnace runtime and event count in ``[start, end]``."""
        total = 0.0
        count = 0
        for event in self._history:
            if event.get("schema") != _SESSION_SCHEMA:
                continue
            data = event.get("data") or {}
            timestamp = _parse_timestamp(data.get("ended_at") or event.get("ts"))
            duration = data.get("duration_s")
            if timestamp is None or isinstance(duration, bool):
                continue
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                continue
            if duration_s < 0 or not start <= timestamp <= end:
                continue
            total += duration_s
            count += 1
        return total, count


__all__ = ["OutdoorTemperatureStormRule"]
