"""
Rule: Floor Not Responding

Detects when a zone has been calling for heat but the furnace has not turned on
within the configured threshold. Emits a high-severity finding per affected zone.
"""

from __future__ import annotations

from datetime import UTC, datetime


class FloorNoResponseRule:
    """
    Tracks zone call start times and detects when no furnace response has occurred.

    Feed events in via:
      - on_floor_call_started(zone, started_at_ts)
      - on_floor_call_ended(zone)
      - on_heating_session_started()

    Then call check(now_ts) to get a list of fault findings.
    """

    def __init__(self, threshold_s: float = 600.0):
        """
        Args:
            threshold_s: Seconds a zone must be calling without a furnace response
                         before a finding is emitted. Default is 600s (10 minutes).
        """
        self.threshold_s = threshold_s
        # Maps zone name -> datetime (UTC) when the call started
        self._calling: dict[str, datetime] = {}

    def on_floor_call_started(self, zone: str, started_at_ts: datetime) -> None:
        """Record that a zone has started calling for heat."""
        self._calling[zone] = started_at_ts

    def on_floor_call_ended(self, zone: str) -> None:
        """Clear a zone when its heating call ends."""
        self._calling.pop(zone, None)

    def on_heating_session_started(self) -> None:
        """Clear all zones when the furnace turns on — it has responded."""
        self._calling.clear()

    def check(self, now_ts: datetime) -> list[dict]:
        """
        Return findings for any zone that has been calling for >= threshold_s
        with no furnace response.

        Args:
            now_ts: Current UTC datetime used to compute elapsed time.

        Returns:
            List of finding dicts with keys: zone, call_start_time, minutes_elapsed, severity.
        """
        findings = []
        for zone, start_ts in self._calling.items():
            elapsed_s = (now_ts - start_ts).total_seconds()
            if elapsed_s >= self.threshold_s:
                findings.append(
                    {
                        "zone": zone,
                        "call_start_time": start_ts.astimezone(UTC).isoformat(),
                        "minutes_elapsed": round(elapsed_s / 60.0, 2),
                        "severity": "high",
                    }
                )
        return findings
