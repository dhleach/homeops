"""
Rule: Floor Daily Runtime Anomaly

Detects when a floor's daily heating runtime is significantly above its
historical rolling baseline. Compares today's runtime to the mean of the
last N days and fires if the runtime exceeds mean × threshold_multiplier.

Guards:
- Requires at least 3 historical data points (insufficient baseline → skip)
- Does not fire if baseline mean < 300 s (floor barely runs → avoid noise)
"""

from __future__ import annotations

import math

from rules.confidence import compute_confidence, severity_label

_DAILY_SUMMARY_SCHEMA = "homeops.consumer.furnace_daily_summary.v1"
_ANOMALY_SCHEMA = "homeops.consumer.floor_runtime_anomaly.v1"


def _utc_ts() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class FloorRuntimeAnomalyRule:
    """
    Detects floors whose daily runtime is well above their historical baseline.

    Args:
        history:              List of ``furnace_daily_summary.v1`` event dicts,
                              oldest first.
        lookback_days:        How many of the most-recent history days to include
                              in the rolling baseline.
        threshold_multiplier: runtime_s > mean_s × multiplier triggers anomaly.
    """

    def __init__(
        self,
        history: list[dict],
        lookback_days: int = 14,
        threshold_multiplier: float = 1.5,
    ) -> None:
        self._history = history or []
        self._lookback_days = lookback_days
        self._threshold_multiplier = threshold_multiplier

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_daily_runtime(
        self,
        floor: str,
        runtime_s: int,
        date_str: str,
    ) -> list[dict]:
        """
        Evaluate today's runtime for one floor against the historical baseline.

        Args:
            floor:      Floor identifier, e.g. ``"floor_2"``.
            runtime_s:  Today's total heating runtime in seconds.
            date_str:   Today's date string (``"YYYY-MM-DD"``), used in the
                        emitted event and to exclude today from the history window.

        Returns:
            A list containing 0 or 1 ``floor_runtime_anomaly.v1`` event dicts.
        """
        prior_runtimes = self._collect_prior_runtimes(floor, date_str)

        if len(prior_runtimes) < 3:
            return []

        mean_s = sum(prior_runtimes) / len(prior_runtimes)

        if mean_s < 300:
            return []

        threshold_s = mean_s * self._threshold_multiplier

        if runtime_s <= threshold_s:
            return []

        # --- Confidence scoring ---
        n = len(prior_runtimes)
        variance = sum((r - mean_s) ** 2 for r in prior_runtimes) / n
        stddev_s = math.sqrt(variance)

        if stddev_s == 0:
            confidence = 0.5
            sev = "medium"
        else:
            z = (runtime_s - mean_s) / stddev_s
            confidence = compute_confidence(abs(z))
            sev = severity_label(confidence)

        return [
            {
                "schema": _ANOMALY_SCHEMA,
                "source": "consumer.v1",
                "ts": _utc_ts(),
                "data": {
                    "floor": floor,
                    "runtime_s": runtime_s,
                    "baseline_mean_s": mean_s,
                    "baseline_stddev_s": stddev_s,
                    "threshold_multiplier": self._threshold_multiplier,
                    "threshold_s": threshold_s,
                    "lookback_days": self._lookback_days,
                    "history_count": len(prior_runtimes),
                    "date": date_str,
                    "confidence": confidence,
                    "severity": sev,
                },
            }
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_prior_runtimes(self, floor: str, exclude_date: str) -> list[float]:
        """Return the per-floor runtimes from the last ``lookback_days`` history entries."""
        # Filter to the summary schema and exclude today's date (avoid circular reference).
        summaries = [
            e
            for e in self._history
            if e.get("schema") == _DAILY_SUMMARY_SCHEMA
            and e.get("data", {}).get("date") != exclude_date
        ]

        # Take only the most recent lookback_days entries.
        window = summaries[-self._lookback_days :]

        runtimes: list[float] = []
        for evt in window:
            per_floor = evt.get("data", {}).get("per_floor_runtime_s", {})
            if floor in per_floor:
                runtimes.append(float(per_floor[floor]))

        return runtimes
